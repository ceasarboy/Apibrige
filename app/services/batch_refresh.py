"""显式刷新未发送的 HAP 增量失败批次；外部只读，快照/计划/审计原子提交。"""
import asyncio
from copy import deepcopy
import hashlib
from types import SimpleNamespace
from uuid import UUID
from sqlalchemy import select
from fastapi import HTTPException
from app.models.sync import SyncRound, DataBuffer, ApiConnection, HapWritePlan, ExecutionLog
from app.config import settings
from app.connectors.incremental_snapshot import read_snapshot
from app.services.incremental import unseal, seal, serialize, get_value
from app.services.incremental_rounds import lock_task, now
from app.services.transformer import transform_payload
from app.services.validator import validate_batch
from app.services.sync_config import side_config
from app.services.adapter_config import build_adapter_config
from app.adapters.factory import ProtocolAdapterFactory
from app.services.hap_bulk import prepare_records, read_index, build_operations, target_identity
from app.services.failure_logs import error_detail, safe_message, validation_detail


def refuse(message, code='batch_refresh_forbidden'):
    raise HTTPException(409, {'code':code, 'message':message})


def revision(batch):
    return (batch.source_data or {}).get('revision', 0)


def unsent(plan, payload):
    if plan.status not in ('pending','blocked') or (plan.lease_until and plan.lease_until > now()):
        return False
    requests = payload.get('requests')
    ops = payload.get('ops')
    if not isinstance(requests, list) or not isinstance(ops, list):
        return False
    if any(r.get('status') != 'pending' or r.get('attempts') != 0 or r.get('acknowledged') or r.get('returned_ids') for r in requests):
        return False
    # 已确认 unchanged 可来自写前查询；其他确认结果不能覆盖。
    return all(op.get('status') == 'pending' or
               (op.get('kind') == 'unchanged' and op.get('status') == 'confirmed') for op in ops)


async def eligible(session, task_id, run_id, batch_id, expected_revision):
    task = await lock_task(session, task_id)
    if task is None or task.active_round_id != run_id:
        refuse('活动轮次已变化，请刷新进度')
    if task.status != 'paused':
        refuse('请先暂停任务，再重新获取失败批次源数据')
    run = await session.get(SyncRound, run_id, populate_existing=True)
    if run is None or not run.sealed_at or run.status not in ('blocked','retry_wait'):
        refuse('只允许刷新已封存且停止执行的失败轮次')
    batches = (await session.scalars(select(DataBuffer).where(DataBuffer.round_id==run_id)
                                     .order_by(DataBuffer.batch_no).execution_options(populate_existing=True))).all()
    if len(batches) != run.batch_count or [b.batch_no for b in batches] != list(range(run.batch_count)):
        refuse('轮次批次清单不完整')
    if any(b.status == 'processing' or (b.lease_until and b.lease_until > now()) for b in batches):
        refuse('轮次仍有在途批次，请等待执行退出')
    batch = next((b for b in batches if b.id==batch_id and b.task_id==task_id), None)
    if batch is None or batch.status not in ('failed','validation_failed'):
        refuse('请选择当前轮次失败的批次')
    if any(b.status != 'completed' for b in batches if b.batch_no < batch.batch_no):
        refuse('请先处理前面的未完成批次')
    if revision(batch) != expected_revision:
        refuse('源数据版本已变化，请刷新进度后重试', 'batch_refresh_conflict')
    if task.last_sync_value != run.from_watermark or task.checkpoint_version != run.checkpoint_version:
        refuse('水位或版本已变化，不能刷新旧轮次')
    saved = unseal(run.config_snapshot)
    if saved['mapping_config'].get('hap_write_mode') != 'upsert':
        refuse('当前仅支持有持久化发送记录的 HAP 新增更新批次')
    plan = await session.scalar(select(HapWritePlan).where(HapWritePlan.buffer_id==batch_id)
                                .with_for_update().execution_options(populate_existing=True))
    if plan is None or plan.task_id != task_id:
        refuse('缺少可证明从未发送的写入计划，不能覆盖源数据')
    payload = unseal(plan.payload)
    if not unsent(plan, payload):
        refuse('计划存在在途、已发送或结果未知的请求，只能恢复原计划回查')
    return task, run, batch, batches, plan, payload, saved


async def source_key_fields(source, mapping):
    if source.db_type:
        from app.connectors.database import get_database_connector
        connector = get_database_connector(source.db_type, source.db_config or {})
        try:
            schema = await connector.get_table_schema(mapping.source_table)
            keys = [c['name'] for c in schema if c.get('primary_key')]
        finally:
            await connector.disconnect()
    else:
        keys = ['rowid'] if source.protocol == 'hap' else [side_config(mapping.mapping_config, 'source')['incremental']['key_field']]
    if not keys:
        refuse('源未提供稳定主键，不能重新绑定失败记录')
    return keys


def identity(row, keys, database=False):
    values = [get_value(row, key) if database else row.get(key) for key in keys]
    if any(v is None or v == '' or isinstance(v, (dict,list,bool)) for v in values):
        refuse('源主键缺失或无效')
    return serialize(values)


async def prepare_refresh(source, target, saved, old_rows, from_watermark, old_payload, other_rows):
    mapping = SimpleNamespace(**saved)
    keys = await source_key_fields(source, mapping)
    old_ids = [identity(row,keys,bool(source.db_type)) for row in old_rows]
    if len(set(old_ids)) != len(old_ids):
        refuse('旧批次源主键重复，无法重新匹配')
    # 复用运行时读取路径及预算；没有按 ID 查询契约时完整分页，再选原主键。
    fresh, _, _ = await read_snapshot(source, mapping, from_watermark)
    by_key = {}
    for row in fresh:
        key = identity(row, keys, bool(source.db_type))
        if key in by_key:
            refuse('重新读取的源主键重复')
        by_key[key] = row
    if any(key not in by_key for key in old_ids):
        refuse('部分原主键在源读取范围内不存在，请核对删除、水位或接口过滤条件')
    selected = [by_key[key] for key in old_ids]
    transformed = transform_payload(selected, mapping.mapping_config)
    validation = validate_batch(transformed, mapping.mapping_config.get('validations') or {})
    if len(transformed) != len(selected) or not validation['valid']:
        raise HTTPException(422, {**validation_detail(validation), 'message':'重新读取的数据仍未通过字段校验'})
    adapter = ProtocolAdapterFactory.create(target.protocol, build_adapter_config(target, mapping, 'target'))
    worksheet = old_payload['worksheet']
    controls = await adapter.get_worksheet_controls(worksheet)
    options = old_payload['options']
    key_field = mapping.mapping_config['key_field']
    key_id, types, desired = prepare_records(transformed, controls, key_field, options)
    # 目标业务键也固定；只修复内容，不把源记录重新绑定到另一条目标。
    key_config = {**mapping.mapping_config, 'fields':[f for f in mapping.mapping_config['fields'] if f['target']==key_field]}
    _, _, old_desired = prepare_records(transform_payload(old_rows,key_config), controls,key_field,options)
    if [d['key'] for d in old_desired] != [d['key'] for d in desired]:
        refuse('目标业务键发生变化，不能替换原计划')
    _, _, others = prepare_records(transform_payload(other_rows,key_config), controls,key_field,options) if other_rows else (None,None,[])
    if set(d['key'] for d in desired) & set(d['key'] for d in others):
        refuse('目标业务键与其他批次冲突')
    async def allowed():
        from app.cache.rate_limiter import rate_limit
        if not await rate_limit(f'hap:{target_identity(adapter,worksheet)}'):
            refuse('目标查询限流，请稍后重试')
    adapter.before_request = allowed
    index = await read_index(adapter,worksheet,key_id,types,allowed)
    ops, requests = build_operations(desired,index,key_id,types,options)
    payload = {'worksheet':worksheet,'config':old_payload['config'],'options':options,'prepared':True,
               'key_id':key_id,'types':types,'desired':desired,'ops':ops,'requests':requests}
    return selected, payload, target_identity(adapter,worksheet)


async def refresh_batch(session, task_id, run_id, batch_id, expected_revision):
    task_id,run_id,batch_id = UUID(str(task_id)),UUID(str(run_id)),UUID(str(batch_id))
    task,run,batch,batches,plan,old_payload,saved = await eligible(session,task_id,run_id,batch_id,expected_revision)
    before = deepcopy(batch.source_data)
    old_rows = unseal(before['encrypted'])
    old_plan = plan.payload
    run_config = run.config_snapshot
    candidate = run.candidate_watermark
    old_attempts = batch.retry_count
    source = await session.get(ApiConnection,UUID(saved['source_connection_id']))
    target = await session.get(ApiConnection,UUID(saved['target_connection_id']))
    if source is None or target is None or target.protocol != 'hap' or target.db_type:
        refuse('原源连接或 HAP 目标不可用')
    target_key = plan.target_key
    from_watermark = run.from_watermark
    other_rows = [row for b in batches if b.id!=batch_id for row in unseal(b.source_data['encrypted'])]
    await session.commit()  # 外部只读期间不持有平台数据库事务锁。
    try:
        selected, payload, fresh_target_key = await asyncio.wait_for(
            prepare_refresh(source,target,saved,old_rows,from_watermark,old_payload,other_rows),
            settings.incremental_prepare_timeout)
        task,run,batch,batches,plan,_,_ = await eligible(session,task_id,run_id,batch_id,expected_revision)
        if batch.source_data != before or plan.payload != old_plan or run.config_snapshot != run_config or run.candidate_watermark != candidate:
            refuse('刷新期间批次或计划已变化，未保存新数据', 'batch_refresh_conflict')
        if target_key != plan.target_key or fresh_target_key != target_key or plan.active_target != target_key:
            refuse('目标身份或写入计划占用已变化')
        new_revision = expected_revision + 1
        history = list(before.get('history') or [])
        history.append({'revision':expected_revision,'encrypted':before['encrypted'],'plan':old_plan,
                        'retry_count':old_attempts,'saved_at':now().isoformat()})
        batch.source_data = {'encrypted':seal(selected),'revision':new_revision,'history':history}
        batch.transformed_data = None
        batch.data_hash = None
        batch.status,batch.retry_count,batch.error_message = 'pending',0,None
        batch.attempt_token,batch.lease_until,batch.next_retry_at = None,None,None
        plan.payload,plan.status,plan.attempt_token,plan.lease_until = seal(payload),'pending',None,None
        plan.updated_at = now()
        run.status,run.error_message = 'blocked',None  # 显式恢复后才执行；不自动派发。
        digest = lambda rows:hashlib.sha256(serialize(rows).encode()).hexdigest()
        result={'round_id':str(run_id),'batch_no':batch.batch_no,'source_revision':new_revision,
                'record_count':len(selected),'changed_records':sum(a!=b for a,b in zip(old_rows,selected)),
                'status':'refreshed','requires_resume':True}
        session.add(ExecutionLog(task_id=task_id,buffer_id=batch_id,action='batch_source_refresh',status='success',
            response_data={**result,'old_revision':expected_revision,'new_revision':new_revision,
                           'before_hash':digest(old_rows),'after_hash':digest(selected),'previous_attempts':old_attempts}))
        await session.commit()
        return result
    except Exception as error:
        await session.rollback()
        session.add(ExecutionLog(task_id=task_id,buffer_id=batch_id,action='batch_source_refresh',status='failed',
            error_message=safe_message(error),response_data={'round_id':str(run_id),'source_revision':expected_revision,
                'error_detail':error_detail(error,'source_read')}))
        await session.commit()
        raise
