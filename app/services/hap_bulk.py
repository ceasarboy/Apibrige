"""HAP V2 upsert 计划与执行；外部非事务写入逐项确认，未知结果不重发。"""
import asyncio
import hashlib
import json
from collections import Counter, defaultdict
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from sqlalchemy import select, update, or_
from sqlalchemy.exc import IntegrityError
from app.adapters.errors import AdapterError
from app.models.sync import HapWritePlan, DataBuffer, SyncTask, ExecutionLog
from app.services.incremental import seal, unseal
from app.services.sync_config import HapBulkConfig, side_config
from app.services.failure_logs import normalize_detail
from app.config import settings
from app.services.hap_options import SINGLE_OPTION_TYPES, normalize_option, control_snapshot, same_options, write_controls


def is_upsert(mapping):
    return (mapping.mapping_config or {}).get('hap_write_mode') == 'upsert'


def fail(code, message, stage='hap_lookup', *, details=None):
    raise AdapterError(code, message, stage, details=details)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def scalar(value, control):
    """仅对已支持的标量控件规范化，复杂控件显式拒绝，不能猜测写入表示。"""
    kind = control.get('type')
    if kind in SINGLE_OPTION_TYPES:
        return normalize_option(value, control)
    if value is None or value == '':
        return ''
    if kind in (6, 8):
        try:
            number = Decimal(str(value))
            if not number.is_finite():
                raise ValueError()
            return format(number.normalize(), 'f')
        except (InvalidOperation, ValueError):
            fail('hap_value_invalid', '数值字段格式无效', 'field_resolve')
    if kind in (15, 16):
        try:
            dt = datetime.fromisoformat(str(value))
            return dt.date().isoformat() if kind == 15 else dt.isoformat(sep=' ')
        except ValueError:
            fail('hap_value_invalid', '日期字段需要 ISO 日期或日期时间', 'field_resolve')
    if kind in (2, 3, 4, 5, 7, 32) and isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    fail('hap_control_unsupported', '批量新增更新暂不支持该字段类型，请使用文本、数值、日期或单选字段', 'field_resolve')


def readback_equal(actual, expected, control):
    """仅在回读比较时容忍文本首尾空白，兼容旧计划保留的原始预期值。"""
    if control.get('type') in (2, 3, 4, 5, 7, 32) and isinstance(actual, str) and isinstance(expected, str):
        return actual.strip() == expected.strip()
    return actual == expected


def catalog(controls):
    by_name = {}
    for control in controls:
        for name in set(filter(None, (control.get('controlId'), control.get('alias')))):
            if name in by_name and by_name[name].get('controlId') != control.get('controlId'):
                fail('hap_field_ambiguous', '目标控件 ID 或别名存在歧义', 'field_resolve')
            by_name[name] = control
    return by_name


def prepare_records(records, controls, key, options):
    names = catalog(controls)
    if key not in names or names[key].get('readOnly'):
        fail('hap_key_invalid', '请选择可写的目标业务键', 'field_resolve')
    key_id = names[key]['controlId']
    types = {}
    desired = []
    seen = set()
    for index, record in enumerate(records):
        values = {}
        if not isinstance(record, dict):
            fail('hap_record_invalid', '源记录必须为对象', 'field_resolve')
        for name, value in record.items():
            control = names.get(name)
            if not control or control.get('readOnly') or control.get('type') in (22, 33):
                fail('hap_field_invalid', '目标字段不存在、只读或不可写', 'field_resolve')
            cid = control['controlId']
            if cid in values:
                fail('hap_field_ambiguous', '同一控件被重复映射', 'field_resolve')
            types[cid] = control_snapshot(control)
            if value is None and options['null_policy'] == 'omit' and cid != key_id:
                continue
            try:
                values[cid] = scalar(value, control)
            except AdapterError as exc:
                exc.details.update(row_index=index, target_field=cid, target_field_alias=control.get('alias'),
                                   target_field_name=control.get('controlName') or control.get('alias') or cid)
                raise
            if values[cid] == '' and control.get('required'):
                fail('hap_required_missing', '目标必填字段不能清空', 'field_resolve',
                     details={'row_index': index, 'target_field': cid, 'target_field_alias': control.get('alias'),
                              'target_field_name': control.get('controlName') or control.get('alias') or cid})
        business_key = values.get(key_id, '')
        if not business_key or business_key in seen:
            fail('hap_key_duplicate', '源业务键为空或重复，未执行写入', 'field_resolve')
        seen.add(business_key)
        desired.append({'index': index, 'key': business_key, 'values': values})
    types.setdefault(key_id, control_snapshot(names[key]))
    # 新增需覆盖所有无默认值声明的必填业务字段；缺失时不让部分记录先写入。
    for control in controls:
        if control.get('required') and not control.get('readOnly') and control.get('type') not in (22,33):
            types.setdefault(control['controlId'], control_snapshot(control))
    return key_id, types, desired


def read_value(row, control):
    keys = set(filter(None, (control['controlId'], control.get('alias'))))
    values = [scalar(row[key], control) for key in keys if key in row]
    if len(set(values)) > 1:
        fail('hap_field_ambiguous', '回读的字段 ID 和别名值不一致', 'hap_verify')
    return values[0] if values else None


async def read_index(adapter, worksheet, key_id, types, before_call):
    adapter.config.update(_stable_scan=True, _scan_max_rows=settings.incremental_max_rows,
                          _scan_max_bytes=settings.incremental_max_bytes)
    await before_call()
    rows = await adapter.get_worksheet_rows(worksheet)
    index = {}
    rowids = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get('rowid') or row['rowid'] in rowids:
            fail('hap_target_invalid', '目标记录 ID 缺失或重复，查询结果不完整')
        rowids.add(row['rowid'])
        if any(read_value(row, control) is None for control in types.values()):
            await before_call()
            details = await adapter.get_worksheet_row(worksheet, row['rowid'])
            row = {**row, **details, 'rowid': row['rowid']}
        key = read_value(row, types[key_id])
        if key is None or key == '' or key in index:
            fail('hap_target_key_conflict', '目标业务键缺失、为空或重复，停止自动写入')
        values = {cid: read_value(row, control) for cid, control in types.items()}
        index[key] = {'rowid': str(row['rowid']), 'values': values}
    return index


def build_operations(desired, index, key_id, types, options, delete=False):
    ops = []
    requests = []
    edits = defaultdict(list)
    adds = []
    for item in desired:
        found = index.get(item['key'])
        rowid = found['rowid'] if found else None
        if found and any(found['values'].get(cid) is None for cid in item['values']):
            fail('hap_readback_unavailable', '目标字段值不可读取，不能判断修改', 'hap_verify')
        patch = {cid: v for cid, v in item['values'].items()
                 if not found or (cid != key_id and found['values'].get(cid) != v)}
        if not found:
            for cid, control in types.items():
                if control.get('required') and not item['values'].get(cid):
                    fail('hap_required_missing', '新增记录缺少目标必填字段', 'field_resolve',
                         details={'row_index': item['index'], 'target_field': cid, 'target_field_alias': control.get('alias'),
                                  'target_field_name': control.get('controlName') or control.get('alias') or cid})
        kind = 'add' if not found else 'edit' if patch else 'unchanged'
        pos = len(ops)
        ops.append({**item, 'kind': kind, 'rowid': rowid, 'patch': patch,
                    'status': 'confirmed' if kind == 'unchanged' else 'pending'})
        if kind == 'add':
            adds.append(pos)
        elif kind == 'edit':
            edits[canonical(patch)].append(pos)
    groups = [('add', adds, options['create_batch_size'])]
    groups.extend(('edit', values, options['update_batch_size']) for values in edits.values())
    for kind, members, size in groups:
        for start in range(0, len(members), size):
            requests.append({'kind': kind, 'members': members[start:start + size], 'status': 'pending', 'attempts': 0})
    if delete and desired:
        source_keys = {item['key'] for item in desired}
        for key, found in index.items():
            if key in source_keys:
                continue
            pos = len(ops)
            ops.append({'index': pos, 'key': key, 'values': {}, 'patch': {}, 'rowid': found['rowid'], 'kind': 'delete', 'status': 'pending'})
            requests.append({'kind': 'delete', 'members': [pos], 'status': 'pending', 'attempts': 0})
    return ops, requests


def summary(payload):
    counts = Counter(op['kind'] for op in payload.get('ops', []) if op['status'] == 'confirmed')
    states = Counter(op['status'] for op in payload.get('ops', []))
    return {'added': counts['add'], 'updated': counts['edit'], 'unchanged': counts['unchanged'],
            'deleted': counts['delete'], 'pending': states['pending'], 'unknown': states['unknown'],
            'total': len(payload.get('desired', []))}


def target_identity(adapter, worksheet):
    # 仅将身份散列持久化；不存认证明文。相同应用的不同连接仍共享目标互斥。
    return hashlib.sha256(canonical([adapter.base_url.rstrip('/'), adapter._appkey, worksheet]).encode()).hexdigest()


async def execute(factory, buffer_id, adapter, mapping, records, task_id=None, before_call=None, probe_id=None):
    """共用批量执行入口；所有网络写入在持久化 sending 之后进行。"""
    cfg = mapping.mapping_config
    target_cfg = side_config(cfg, 'target')
    options = HapBulkConfig.model_validate(target_cfg.get('hap_bulk') or {}).model_dump()
    worksheet = target_cfg.get('worksheet_id') or mapping.target_endpoint
    if not worksheet or target_cfg.get('view_id') or target_cfg.get('filters'):
        fail('hap_scope_invalid', '新增更新必须查询完整目标工作表，不能配置目标视图或过滤范围')
    buffer_id = UUID(str(buffer_id)) if buffer_id else None
    if buffer_id is None and probe_id is None:
        fail('hap_plan_identity_required', '写入需要批次或已授权探针身份', 'validate')
    target_key = target_identity(adapter, worksheet)
    token = str(uuid4())
    now = lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    ttl = max(120, int(adapter.timeout) * 2 + 30)

    async def check_call():
        if before_call:
            await before_call()
        from app.cache.rate_limiter import rate_limit
        if not await rate_limit(f'hap:{target_key}'):
            fail('hap_rate_limited', 'HAP 请求限流，稍后恢复原计划')

    async with factory() as db:
        condition = HapWritePlan.buffer_id == buffer_id if buffer_id else HapWritePlan.probe_id == probe_id
        plan = await db.scalar(select(HapWritePlan).where(condition))
        if plan is None:
            # 目标互斥在读取元数据之前占用，准备失败也能恢复原计划。
            payload = {'desired': [], 'ops': [], 'requests': [], 'prepared': False,
                       'records': records, 'worksheet': worksheet, 'config': cfg, 'options': options}
            plan = HapWritePlan(id=uuid4(), buffer_id=buffer_id, probe_id=probe_id, task_id=task_id,
                target_key=target_key, active_target=target_key, payload=seal(payload), status='pending')
            db.add(plan)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                fail('hap_target_busy', '同一目标有未完成写入计划，请先恢复该计划')
        if plan.target_key != target_key:
            fail('hap_target_changed', '目标身份变化，不能重放旧写入计划')
        if plan.status == 'cancelled':
            fail('hap_plan_cancelled', '该计划已取消，不可继续发送')
        if probe_id and unseal(plan.payload)['config'] != cfg:
            fail('hap_probe_changed', '已有写入计划与当前草稿不同，请核对原探针结果', 'validate')
        if plan.status == 'completed':
            return {'status_code': 200, 'body': summary(unseal(plan.payload)), 'plan_id': str(plan.id)}
        plan_id = plan.id
        buffer = await db.get(DataBuffer, buffer_id) if buffer_id else None
        source_revision = (buffer.source_data or {}).get('revision', 0) if buffer else 0
        claimed = await db.execute(update(HapWritePlan).where(HapWritePlan.id == plan_id,
            HapWritePlan.status.not_in(['completed','cancelled']),
            or_(HapWritePlan.lease_until.is_(None), HapWritePlan.lease_until < now())).values(
                attempt_token=token, lease_until=now() + timedelta(seconds=ttl)))
        await db.commit()
        if not claimed.rowcount:
            fail('hap_plan_busy', '该写入计划仍在执行，请稍后查看')
        await db.refresh(plan)
        payload = unseal(plan.payload)

    async def persist(status='running'):
        async with factory() as db:
            result = await db.execute(update(HapWritePlan).where(HapWritePlan.id == plan_id,
                HapWritePlan.attempt_token == token, HapWritePlan.lease_until > now()).values(
                    payload=seal(payload), status=status, updated_at=now()))
            await db.commit()
            if not result.rowcount:
                fail('hap_lease_lost', '写入计划租约已失效，停止后续请求')

    async def allowed():
        async with factory() as db:
            p = await db.get(HapWritePlan, plan_id)
            if p.attempt_token != token or not p.lease_until or p.lease_until <= now():
                fail('hap_lease_lost', '写入计划执行权已失效')
            if task_id:
                task = await db.get(SyncTask, task_id)
                if task is None or task.status != 'active':
                    fail('hap_task_paused', '任务已暂停，未发送请求保持等待')
        await check_call()

    async def heartbeat():
        while True:
            await asyncio.sleep(ttl / 3)
            async with factory() as db:
                result = await db.execute(update(HapWritePlan).where(HapWritePlan.id == plan_id,
                    HapWritePlan.attempt_token == token, HapWritePlan.lease_until > now()).values(
                        lease_until=now() + timedelta(seconds=ttl)))
                await db.commit()
                if not result.rowcount:
                    return

    heart = asyncio.create_task(heartbeat())
    old_guard = getattr(adapter, 'before_request', None)
    adapter.before_request = allowed
    try:
        options = payload['options']
        await allowed()
        controls = await adapter.get_worksheet_controls(worksheet)
        if not payload['prepared']:
            key_id, types, desired = prepare_records(payload['records'], controls, payload['config']['key_field'], options)
            index = await read_index(adapter, worksheet, key_id, types, allowed)
            # 删除差额的开关双键同口径：hap_delete_sync（发行包键）与 delete_sync（既有映射键）
            # 都要触发 delete=True——只认新键会让既有映射的删除同步静默失效（合并回归）
            cfg_delete = bool(payload['config'].get('hap_delete_sync')) or bool(payload['config'].get('delete_sync'))
            ops, requests = build_operations(desired, index, key_id, types, options, cfg_delete and not probe_id)
            payload.update(key_id=key_id, types=types, desired=desired, ops=ops, requests=requests, prepared=True)
            payload.pop('records')
            await persist()
        else:
            fresh = catalog(controls)
            if any(cid not in fresh or fresh[cid].get('type') != info['type'] for cid, info in payload['types'].items()):
                fail('hap_schema_changed', '目标控件结构已变化，不能继续旧计划')
            for cid, info in payload['types'].items():
                if info['type'] in SINGLE_OPTION_TYPES and not same_options(info, fresh[cid]):
                    fail('hap_schema_changed', '目标选项定义已变化，停止原计划；请核对名称、key 和删除状态')
            # 固定 ID，读取时兼容最新别名；不改变已固定的写入字段和值。
            for cid, info in payload['types'].items():
                info['alias'] = fresh[cid].get('alias')
        for req in payload['requests']:
            if req['status'] == 'confirmed':
                continue
            members = [payload['ops'][i] for i in req['members']]
            if req['status'] == 'pending':
                # 更新始终使用计划中固定的 rowId，不能重新绑定到同键的新行。
                await allowed()
                req['status'] = 'sending'
                req['attempts'] += 1
                for op in members:
                    op['status'] = 'unknown'
                await persist()
                try:
                    if req['kind'] == 'add':
                        req['returned_ids'] = await adapter.add_worksheet_rows(worksheet,
                            [write_controls(op['values'], payload['types']) for op in members], options['trigger_workflow'])
                    elif req['kind'] == 'edit':
                        await adapter.edit_worksheet_rows(worksheet, [op['rowid'] for op in members],
                            write_controls(members[0]['patch'], payload['types']), options['trigger_workflow'])
                    else:
                        await adapter.delete_worksheet_row(worksheet, members[0]['rowid'])
                    req['acknowledged'] = True
                except Exception as exc:
                    req['acknowledged'] = False
                    req['error_code'] = exc.code if isinstance(exc, AdapterError) else type(exc).__name__
                req['status'] = 'unknown'
                await persist()
            # 业务任务仅确认业务键存在；L3 探针保留字段与记录身份比对。
            verify_types = payload['types'] if probe_id else {payload['key_id']:payload['types'][payload['key_id']]}
            index = await read_index(adapter, worksheet, payload['key_id'], verify_types, allowed)
            for op in members:
                if op['status'] == 'confirmed':
                    continue
                found = index.get(op['key'])
                if op['kind'] == 'delete':
                    matched = not any(row['rowid'] == op['rowid'] for row in index.values())
                elif not probe_id:
                    matched = found is not None
                else:
                    matched = found is not None and all(
                        found['values'].get(k) == v if k == payload['key_id'] else
                        readback_equal(found['values'].get(k), v, payload['types'][k])
                        for k, v in op['values'].items())
                    if matched and op['kind'] == 'edit':
                        matched = found['rowid'] == op['rowid']
                    if matched and req.get('returned_ids'):
                        matched = found['rowid'] in req['returned_ids']
                # L3 仍需确认工作流应答；业务任务仅以业务键存在确认。
                if matched and (not probe_id or req.get('acknowledged') or not options['trigger_workflow']):
                    op['status'] = 'confirmed'
                    if found:
                        op['rowid'] = found['rowid']
            req['status'] = 'confirmed' if all(op['status'] == 'confirmed' for op in members) else 'unknown'
            verification_issues = []
            for op in members:
                if op['status'] == 'confirmed':
                    continue
                found = index.get(op['key'])
                location = {'row_index':op['index']} if op['kind'] != 'delete' else {}
                differences = [cid for cid, value in op['values'].items()
                               if probe_id and found and not readback_equal(found['values'].get(cid), value, payload['types'][cid])]
                if differences:
                    for cid in differences:
                        info = payload['types'][cid]
                        verification_issues.append({**location, 'target_field':cid,
                            'target_field_alias':info.get('alias'),
                            'target_field_name':info.get('controlName') or info.get('alias') or cid,
                            'rule':'readback_mismatch'})
                else:
                    verification_issues.append({**location, 'rule':'target_record_missing' if found is None else 'identity_or_ack_unconfirmed'})
            await persist()
            if task_id:
                async with factory() as db:
                    db.add(ExecutionLog(task_id=task_id, buffer_id=buffer_id, action=f'hap_{req["kind"]}',
                        status='success' if req['status']=='confirmed' else 'failed',
                        response_data={'plan_id':str(plan_id), 'record_count':len(members),
                            'source_revision':source_revision,
                            'verification_mode':'field_values' if probe_id else 'business_key_exists',
                            'confirmed':sum(op['status']=='confirmed' for op in members),
                            'error_code':req.get('error_code'),
                            **({'error_detail': normalize_detail(
                                {'code':'hap_outcome_unknown', 'stage':'hap_verify', 'issues':verification_issues})}
                               if req['status'] != 'confirmed' else {})}))
                    await db.commit()
            if req['status'] != 'confirmed':
                raise AdapterError('hap_outcome_unknown', '部分记录结果待核对；恢复只回查，不重发已发送请求', 'hap_verify',
                    details={'cause':req.get('error_code'), 'confirmed':sum(op['status']=='confirmed' for op in members),
                             'issues':verification_issues})
        async with factory() as db:
            result = await db.execute(update(HapWritePlan).where(HapWritePlan.id == plan_id,
                HapWritePlan.attempt_token == token, HapWritePlan.lease_until > now()).values(
                    payload=seal(payload), status='completed', active_target=None, lease_until=None))
            await db.commit()
            if not result.rowcount:
                fail('hap_lease_lost', '完成确认时执行权失效，请恢复原计划')
        return {'status_code': 200, 'body': summary(payload), 'plan_id': str(plan_id)}
    except Exception as exc:
        with suppress(Exception):
            await persist('blocked')
        if isinstance(exc, AdapterError):
            raise
        fail('hap_operation_failed', 'HAP 查询或处理失败；请检查目标连接并恢复原计划', 'hap_verify')
    finally:
        adapter.before_request = old_guard
        heart.cancel()
        with suppress(asyncio.CancelledError):
            await heart
        async with factory() as db:
            await db.execute(update(HapWritePlan).where(HapWritePlan.id == plan_id,
                HapWritePlan.attempt_token == token).values(lease_until=None))
            await db.commit()


async def preview(adapter, mapping, records, before_call=None):
    """只查询目标并分类；不创建计划，不写目标。"""
    async def allowed():
        if before_call:
            await before_call()
    cfg = mapping.mapping_config
    target = side_config(cfg, 'target')
    if target.get('view_id') or target.get('filters'):
        fail('hap_scope_invalid', '新增更新必须查询完整目标工作表')
    worksheet = target.get('worksheet_id') or mapping.target_endpoint
    options = HapBulkConfig.model_validate(target.get('hap_bulk') or {}).model_dump()
    old_guard = getattr(adapter, 'before_request', None)
    adapter.before_request = allowed
    try:
        await allowed()
        controls = await adapter.get_worksheet_controls(worksheet)
        key_id, types, desired = prepare_records(records, controls, cfg['key_field'], options)
        index = await read_index(adapter, worksheet, key_id, types, allowed)
        ops, requests = build_operations(desired, index, key_id, types, options)
    finally:
        adapter.before_request = old_guard
    return {'zero_write': True, 'counts': dict(Counter(op['kind'] for op in ops)),
            'request_count': len(requests), 'rows': [{'index':op['index'] + 1,'operation':op['kind']} for op in ops]}
