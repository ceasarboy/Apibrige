"""单活动轮次协调：先封存，串行处理，整轮成功后提交水位。"""
import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select, update, func
from app.config import settings
from app.database import async_session as session_factory
from app.models.sync import SyncTask, SyncRound, DataBuffer, ApiConnection, FieldMapping, ExecutionLog
from app.connectors.incremental_snapshot import read_snapshot
from app.services.incremental import validate, reject, seal, unseal, typed, target_retry_limit
from app.services.transformer import transform_payload
from app.services.validator import validate_batch
from app.services.sync_handler import _push_target
from app.security.desensitize import desensitize_record
from app.services.failure_logs import error_detail, validation_detail


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def dispatch(buffer_id):
    from app.celery_app import process_incremental_batch
    process_incremental_batch.apply_async(args=[str(buffer_id)], retry=False)


def safe_error(error):
    if isinstance(error, HTTPException) and isinstance(error.detail, dict):
        return error.detail.get('message', '增量配置校验失败')
    from app.adapters.errors import AdapterError
    if isinstance(error, AdapterError) and error.code.startswith('hap_'):
        return str(error)
    if isinstance(error, AdapterError) and error.code in {
            'incremental_source_changed', 'incremental_round_too_large', 'source_records_invalid',
            'source_http_failed', 'source_response_invalid', 'source_business_failed', 'source_response_contract_failed', 'source_pagination_invalid', 'source_filter_invalid', 'worksheet_metadata_unavailable'}:
        return str(error)
    return '批次处理失败或结果未确认，请检查目标连接和日志后重试（原始异常已隐藏）'


def checkpoint_value(run, value):
    # 新准备的 API 轮次冻结兼容版本；旧已封存批次继续使用原比较规则。
    saved = unseal(run.config_snapshot)
    if saved.get('api_time_compat') == 1:
        from app.services.incremental import api_typed
        return api_typed(value, run.watermark_type)
    return typed(value, run.watermark_type)


def snapshot(mapping):
    return {key: getattr(mapping, key) for key in (
        'source_table', 'target_table', 'source_endpoint', 'target_endpoint', 'mapping_config',
        'source_headers', 'target_headers', 'incremental_field', 'batch_size', 'sync_mode')}


NO_RETRY_MESSAGE = '本轮重试次数为 0，已尝试批次不可重发；请人工核对目标写入结果。水位保持原值。'


async def round_retry_limit(session, run):
    try:
        saved = unseal(run.config_snapshot)
        limit = saved.get('retry_limit')
        if type(limit) is int and limit >= 0:
            return limit
        # 旧轮次没有冻结策略时，根据原映射与目标判断；无法读取时禁止重发。
        target = await session.get(ApiConnection, UUID(saved['target_connection_id']))
        return target_retry_limit(SimpleNamespace(**saved), target)
    except Exception:
        return 0


def retry_block_reason(limit, batches):
    if limit == 0 and any(b.retry_count > 0 and b.status != 'completed' for b in batches):
        return NO_RETRY_MESSAGE
    return None


async def lock_task(session, task_id):
    # 无值变更的 UPDATE 在 SQLite/PG 都获得事务写锁，串行化轮次/暂停/配置变更。
    await session.execute(update(SyncTask).where(SyncTask.id == task_id).values(checkpoint_version=SyncTask.checkpoint_version))
    return await session.get(SyncTask, task_id, populate_existing=True)


async def start_round(session, task, mapping, source, target):
    validate(mapping, source, task, target)
    task_id = task.id
    task = await lock_task(session, task_id)
    mapping = await session.get(FieldMapping, task.mapping_id, populate_existing=True)
    source = await session.get(ApiConnection, mapping.source_connection_id, populate_existing=True)
    target = await session.get(ApiConnection, mapping.target_connection_id, populate_existing=True)
    validate(mapping, source, task, target)
    if task.active_round_id:
        result = await round_detail(session, task)
        await session.commit()
        return {**result, 'reused': True}
    if task.status != 'active':
        await session.rollback()
        reject('incremental_task_paused', '任务已暂停', 409)
    # 防止旧版本未完成增量批次被当成新轮次跳过。
    legacy = await session.scalar(select(func.count()).select_from(DataBuffer).where(
        DataBuffer.task_id == task.id, DataBuffer.round_id.is_(None),
        DataBuffer.status.in_(['pending', 'processing', 'failed', 'validation_failed'])))
    if legacy:
        await session.rollback()
        reject('incremental_legacy_pending', '存在旧版未完成批次，请处理后再执行增量轮次', 409)
    from app.services.field_policy import freeze
    mapping = await freeze(session, mapping, source, target)
    saved = snapshot(mapping)
    saved.update(source_connection_id=str(source.id), target_connection_id=str(target.id))
    saved['retry_limit'] = target_retry_limit(mapping, target)
    run = SyncRound(id=uuid4(), task_id=task.id, from_watermark=task.last_sync_value,
                    checkpoint_version=task.checkpoint_version, config_snapshot=seal(saved),
                    attempt_token=str(uuid4()), lease_until=now() + timedelta(seconds=settings.incremental_prepare_timeout + 30))
    session.add(run)
    task.active_round_id = run.id
    await session.commit()
    await prepare_round(run.id)
    await session.refresh(task)
    result = await round_detail(session, task, run.id)
    await session.commit()
    return {**result, 'reused': False}


async def prepare_round(round_id):
    async with session_factory() as session:
        run = await session.get(SyncRound, round_id)
        if run is None or run.status != 'preparing':
            return
        token = run.attempt_token
        task_id = run.task_id
        saved = unseal(run.config_snapshot)
        source = await session.get(ApiConnection, UUID(saved['source_connection_id']))
        mapping = SimpleNamespace(**saved)
        try:
            rows, candidate, kind = await asyncio.wait_for(
                read_snapshot(source, mapping, run.from_watermark), settings.incremental_prepare_timeout)
            if mapping.mapping_config.get('hap_write_mode') == 'upsert':
                # 整轮跨批次检查目标业务键，避免不同源行被分批后覆盖同一目标。
                from app.services.hap_bulk import prepare_records
                from app.services.sync_config import side_config, HapBulkConfig
                from app.services.adapter_config import build_adapter_config
                from app.adapters.factory import ProtocolAdapterFactory
                target = await session.get(ApiConnection, UUID(saved['target_connection_id']))
                adapter = ProtocolAdapterFactory.create('hap', build_adapter_config(target, mapping, 'target'))
                cfg = side_config(mapping.mapping_config, 'target')
                controls = await adapter.get_worksheet_controls(cfg.get('worksheet_id') or mapping.target_endpoint)
                prepare_records(transform_payload(rows, mapping.mapping_config), controls,
                    mapping.mapping_config['key_field'], HapBulkConfig.model_validate(cfg.get('hap_bulk') or {}).model_dump())
            task = await lock_task(session, run.task_id)
            await session.refresh(run)
            if task.active_round_id != run.id or run.attempt_token != token or run.status != 'preparing':
                await session.rollback()
                return
            for index, offset in enumerate(range(0, len(rows), mapping.batch_size)):
                session.add(DataBuffer(id=uuid4(), task_id=task.id, round_id=run.id, batch_no=index,
                                       source_data={'encrypted': seal(rows[offset:offset + mapping.batch_size])}, status='pending'))
            if not source.db_type:
                saved['api_time_compat'] = 1
                run.config_snapshot = seal(saved)
            run.candidate_watermark = candidate
            run.watermark_type = kind
            run.batch_count = (len(rows) + mapping.batch_size - 1) // mapping.batch_size
            run.sealed_at = now()
            run.status = 'running'
            run.lease_until = None
            await session.commit()
        except Exception as error:
            await session.rollback()
            await session.execute(update(SyncRound).where(SyncRound.id == round_id,
                SyncRound.attempt_token == token, SyncRound.status == 'preparing').values(
                    status='blocked', error_message=safe_error(error), lease_until=None))
            detail = error_detail(error, 'source_read')
            session.add(ExecutionLog(task_id=task_id, action='source_read', status='failed',
                error_message=safe_error(error), response_data={'round_id':str(round_id), 'error_detail':detail}))
            await session.commit()
            return
    await advance(round_id)


async def advance(round_id):
    """事务内核验完整清单并收口；事务外投递下一批，失败由扫描补偿。"""
    next_id = None
    async with session_factory() as session:
        run = await session.get(SyncRound, round_id)
        if not run or not run.sealed_at or run.status == 'completed':
            return
        task = await lock_task(session, run.task_id)
        await session.refresh(run)
        if task.active_round_id != run.id:
            await session.rollback()
            return
        batches = (await session.scalars(select(DataBuffer).where(DataBuffer.round_id == run.id).order_by(DataBuffer.batch_no))).all()
        if len(batches) != run.batch_count or [b.batch_no for b in batches] != list(range(run.batch_count)):
            run.status, run.error_message = 'blocked', '轮次批次清单不完整，水位未提交'
        elif all(b.status == 'completed' and b.processed_at and b.attempt_token for b in batches):
            if task.checkpoint_version != run.checkpoint_version or task.last_sync_value != run.from_watermark:
                run.status, run.error_message = 'blocked', '水位版本冲突，未提交本轮水位'
            elif run.from_watermark is not None and checkpoint_value(run, run.candidate_watermark) < checkpoint_value(run, run.from_watermark):
                run.status, run.error_message = 'blocked', '候选水位回退，已拒绝提交'
            else:
                task.last_sync_value = run.candidate_watermark
                task.checkpoint_version += 1
                task.last_run_at = now()
                task.active_round_id = None
                run.status, run.completed_at = 'completed', now()
                run.error_message = None
                session.add(ExecutionLog(task_id=task.id, action='checkpoint', status='success',
                    response_data={'round_id': str(run.id), 'from_watermark': run.from_watermark,
                                   'watermark': run.candidate_watermark, 'batches': run.batch_count}))
        elif task.status == 'active' and run.status == 'running':
            first = next(b for b in batches if b.status != 'completed')
            if first.status == 'pending' and task.sync_mode == 'async':
                next_id = first.id
        await session.commit()
    if next_id:
        try:
            dispatch(next_id)
        except Exception:
            # 不改变持久化 pending，消息丢失/重复投递都由消费领取和扫描兜底。
            pass


async def push_batch(target, mapping, records, batch_id):
    from app.services.hap_bulk import is_upsert, execute
    if is_upsert(mapping) and target.protocol == 'hap':
        from app.adapters.factory import ProtocolAdapterFactory
        from app.services.adapter_config import build_adapter_config
        adapter = ProtocolAdapterFactory.create('hap', build_adapter_config(target, mapping, 'target'))
        async with session_factory() as db:
            batch = await db.get(DataBuffer, UUID(str(batch_id)))
            task_id = batch.task_id
        return await execute(session_factory, batch_id, adapter, mapping, records, task_id)
    if target.db_type:
        from app.connectors.database import get_database_connector
        connector = get_database_connector(target.db_type, target.db_config or {})
        schema = await connector.get_table_schema(mapping.target_table)
        key = mapping.mapping_config['key_field']
        matches = [c for c in schema if c.get('primary_key')]
        if len(matches) != 1 or matches[0]['name'].lower() != key.lower():
            reject('incremental_idempotency_required', '目标配置主键必须对应数据库的单列主键')
        for row in records:
            if row.get(key) is None:
                reject('incremental_idempotency_required', '转换结果缺少目标主键，不能安全重试')
    else:
        from app.services.sync_config import side_config
        config = side_config(mapping.mapping_config, 'target')
        if config.get('idempotency_header'):
            mapping.target_headers = {**(mapping.target_headers or {}), config['idempotency_header']: str(batch_id)}
    result = await _push_target(target, mapping, records)
    if result.get('skipped'):
        reject('incremental_write_unconfirmed', '本批未确认写入，不能推进水位')
    return result


async def renew(batch_id, token):
    while True:
        await asyncio.sleep(max(1, settings.incremental_lease_seconds // 3))
        async with session_factory() as session:
            result = await session.execute(update(DataBuffer).where(DataBuffer.id == batch_id,
                DataBuffer.status == 'processing', DataBuffer.attempt_token == token).values(
                lease_until=now() + timedelta(seconds=settings.incremental_lease_seconds)))
            await session.commit()
            if not result.rowcount:
                return


async def process_batch(batch_id):
    batch_id = UUID(str(batch_id))
    async with session_factory() as session:
        batch = await session.get(DataBuffer, batch_id)
        if not batch or not batch.round_id:
            return
        run_id = batch.round_id
        task = await lock_task(session, batch.task_id)
        run = await session.get(SyncRound, run_id, populate_existing=True)
        await session.refresh(batch)
        if batch.status == 'completed':
            await session.commit()
            await advance(run_id)
            return
        previous = await session.scalar(select(func.count()).select_from(DataBuffer).where(
            DataBuffer.round_id == run_id, DataBuffer.batch_no < batch.batch_no, DataBuffer.status != 'completed'))
        if task.status != 'active' or task.active_round_id != run_id or run.status != 'running' or not run.sealed_at or previous or batch.status != 'pending':
            await session.rollback()
            return
        try:
            saved = unseal(run.config_snapshot)
            records = unseal(batch.source_data['encrypted'])
        except Exception:
            run.status, run.error_message = 'blocked', '轮次数据或密钥不可用，请恢复原密钥后重试'
            batch.status, batch.error_message = 'validation_failed', run.error_message
            await session.commit()
            return
        retry_limit = await round_retry_limit(session, run)
        if retry_limit == 0 and batch.retry_count > 0:
            run.status, run.error_message = 'blocked', NO_RETRY_MESSAGE
            batch.status, batch.error_message, batch.next_retry_at = 'failed', NO_RETRY_MESSAGE, None
            await session.commit()
            return
        token = str(uuid4())
        batch.status, batch.attempt_token = 'processing', token
        batch.lease_until = now() + timedelta(seconds=settings.incremental_lease_seconds)
        batch.retry_count += 1
        target = await session.get(ApiConnection, UUID(saved['target_connection_id']))
        await session.commit()
    heartbeat = asyncio.create_task(renew(batch_id, token))
    error = None
    transformed = None
    try:
        mapping = SimpleNamespace(**saved)
        transformed = transform_payload(records, mapping.mapping_config)
        if len(transformed) != len(records):
            reject('incremental_write_unconfirmed', '转换后记录数量变化，不能确认整批成功')
        rules = mapping.mapping_config.get('validations')
        if rules:
            validation = validate_batch(transformed, rules)
            if not validation['valid']:
                raise HTTPException(422, {**validation_detail(validation), 'code':'incremental_validation_failed',
                                          'message':'批次字段校验失败，请检查映射与源数据'})
        from app.cache.rate_limiter import rate_limit
        if not await rate_limit(f'target:{target.id}'):
            raise ConnectionError('rate limit')
        await push_batch(target, mapping, transformed, batch_id)
    except Exception as exc:
        error = exc
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await heartbeat
    async with session_factory() as session:
        batch = await session.get(DataBuffer, batch_id)
        task = await lock_task(session, batch.task_id)
        await session.refresh(batch)
        run = await session.get(SyncRound, run_id)
        if batch.attempt_token != token or batch.status != 'processing' or task.active_round_id != run_id:
            await session.rollback()
            return
        batch.lease_until = None
        if error:
            batch.status = 'validation_failed' if isinstance(error, HTTPException) else 'failed'
            batch.error_message = NO_RETRY_MESSAGE if retry_limit == 0 else safe_error(error)
            blocked = isinstance(error, HTTPException) or batch.retry_count >= retry_limit + 1
            batch.next_retry_at = None if blocked else now() + timedelta(seconds=30 * 2 ** min(batch.retry_count - 1, 5))
            run.status = 'blocked' if blocked else 'retry_wait'
            run.error_message = batch.error_message
        else:
            batch.status, batch.processed_at = 'completed', now()
            batch.error_message = None
            batch.transformed_data = {'encrypted': seal(transformed)}
        session.add(ExecutionLog(task_id=task.id, buffer_id=batch.id, action='incremental_batch',
            status='failed' if error else 'success', error_message=batch.error_message,
            response_data={'round_id': str(run_id), 'batch_no': batch.batch_no,
                           'source_revision':(batch.source_data or {}).get('revision',0),
                           **({'error_detail': error_detail(error, 'push')} if error else {})}))
        await session.commit()
    await advance(run_id)


async def retry_round(session, task):
    task = await lock_task(session, task.id)
    if not task.active_round_id:
        reject('incremental_round_missing', '没有活动轮次', 409)
    run = await session.get(SyncRound, task.active_round_id)
    if run.status not in ('blocked', 'retry_wait'):
        reject('incremental_round_busy', '轮次正在处理，不能重复重试', 409)
    if task.status != 'active':
        reject('incremental_task_paused', '请先恢复任务', 409)
    if await session.scalar(select(func.count()).select_from(DataBuffer).where(DataBuffer.round_id == run.id, DataBuffer.status == 'processing')):
        reject('incremental_round_busy', '存在在途批次，请等待租约恢复后重试', 409)
    batches = (await session.scalars(select(DataBuffer).where(DataBuffer.round_id == run.id).execution_options(populate_existing=True))).all()
    reason = retry_block_reason(await round_retry_limit(session, run), batches)
    if reason:
        reject('incremental_retry_disabled', reason, 409)
    run.error_message = None
    if run.sealed_at:
        await session.execute(update(DataBuffer).where(DataBuffer.round_id == run.id,
            DataBuffer.status.in_(['failed', 'validation_failed'])).values(status='pending', next_retry_at=None))
        run.status = 'running'
    else:
        run.status, run.attempt_token = 'preparing', str(uuid4())
        run.lease_until = now() + timedelta(seconds=settings.incremental_prepare_timeout + 30)
    session.add(ExecutionLog(task_id=task.id, action='incremental_retry', status='success', response_data={'round_id': str(run.id)}))
    run_id, sealed = run.id, run.sealed_at
    await session.commit()
    if sealed:
        await advance(run_id)
    else:
        await prepare_round(run_id)
    await session.refresh(task)
    return await round_detail(session, task, run_id)


async def delete_unsealed_round(session, task):
    """删除准备失败且未封存的空轮次；已写入/封存轮次不能删除。"""
    task = await lock_task(session, task.id)
    if not task.active_round_id:
        reject('incremental_round_missing', '没有活动轮次', 409)
    run = await session.get(SyncRound, task.active_round_id, with_for_update=True)
    if run is None:
        reject('incremental_round_missing', '活动轮次不存在', 409)
    if run.status not in ('blocked', 'retry_wait') or run.sealed_at or run.batch_count:
        reject('incremental_round_delete_forbidden', '只有未封存且没有批次的失败轮次可以删除', 409)
    batches = (await session.scalars(select(DataBuffer).where(DataBuffer.round_id == run.id))).all()
    if batches:
        reject('incremental_round_delete_forbidden', '轮次已生成批次，不能删除', 409)
    from app.models.sync import HapWritePlan
    plans = (await session.scalars(select(HapWritePlan).where(HapWritePlan.task_id == task.id))).all()
    if plans:
        reject('incremental_round_delete_forbidden', '任务存在 HAP 写入计划，不能删除', 409)
    session.add(ExecutionLog(task_id=task.id, action='incremental_round_delete', status='success', response_data={'round_id': str(run.id), 'reason': 'manual_management_action'}))
    task.active_round_id = None
    await session.delete(run)
    await session.commit()
    return {'task_id': str(task.id), 'round_id': str(run.id), 'deleted': True, 'last_sync_value': task.last_sync_value}


async def recover():
    async with session_factory() as session:
        ids = (await session.scalars(select(SyncRound.id).where(SyncRound.status != 'completed'))).all()
    for run_id in ids:
        async with session_factory() as session:
            run = await session.get(SyncRound, run_id)
            task = await lock_task(session, run.task_id)
            await session.refresh(run)
            if task.active_round_id != run.id:
                await session.rollback()
                continue
            if run.status == 'preparing' and run.lease_until and run.lease_until < now():
                run.status, run.error_message = 'blocked', '轮次准备中断，未写目标；可重试准备'
                run.attempt_token = None
            batches = (await session.scalars(select(DataBuffer).where(DataBuffer.round_id == run.id))).all()
            retry_limit = await round_retry_limit(session, run)
            for batch in batches:
                if batch.status == 'processing' and batch.lease_until and batch.lease_until < now():
                    batch.status, batch.attempt_token = 'failed', None
                    batch.error_message = NO_RETRY_MESSAGE if retry_limit == 0 else '消费租约过期，写入结果未知，使用原幂等标识重试'
                    blocked = retry_limit == 0 or batch.retry_count >= retry_limit + 1
                    batch.next_retry_at = None if blocked else now() + timedelta(seconds=30)
                    run.status = 'blocked' if blocked else 'retry_wait'
                    run.error_message = batch.error_message
            reason = retry_block_reason(retry_limit, [b for b in batches if b.status in ('failed', 'validation_failed', 'pending')])
            if reason:
                run.status, run.error_message = 'blocked', reason
                for batch in batches:
                    batch.next_retry_at = None
            if task.status == 'active' and run.status == 'retry_wait':
                for batch in batches:
                    if batch.status == 'failed' and batch.next_retry_at and batch.next_retry_at <= now():
                        batch.status = 'pending'
                        run.status = 'running'
            await session.commit()
        await advance(run_id)


async def round_detail(session, task, round_id=None):
    run = await session.get(SyncRound, round_id or task.active_round_id) if (round_id or task.active_round_id) else None
    if run is None:
        run = await session.scalar(select(SyncRound).where(SyncRound.task_id == task.id).order_by(SyncRound.created_at.desc()).limit(1))
    if run is None:
        return {'round_id': None, 'status': 'none', 'watermark': task.last_sync_value, 'batches': []}
    await session.refresh(run)
    batches = (await session.scalars(select(DataBuffer).where(DataBuffer.round_id == run.id).order_by(DataBuffer.batch_no))).all()
    retry_limit = await round_retry_limit(session, run)
    reason = retry_block_reason(retry_limit, batches) if run.status in ('blocked', 'retry_wait') else None
    return desensitize_record({'round_id': str(run.id), 'status': run.status, 'watermark': task.last_sync_value,
        'from_watermark': run.from_watermark, 'candidate_watermark': run.candidate_watermark,
        'batch_count': run.batch_count, 'completed_batches': sum(b.status == 'completed' for b in batches),
        'retry_limit': retry_limit, 'retry_block_reason': reason,
        'retry_allowed': not reason and run.status in ('blocked', 'retry_wait') and task.status == 'active' and not any(b.status == 'processing' for b in batches),
        'error': run.error_message, 'batches': [{'id': str(b.id), 'batch_no': b.batch_no,
            'status': b.status, 'attempts': b.retry_count, 'error': b.error_message, 'source_revision': (b.source_data or {}).get('revision', 0), 'source_refreshable': b.status in ('failed','validation_failed') and task.status == 'paused'} for b in batches]})
