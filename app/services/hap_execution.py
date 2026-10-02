"""全量 HAP upsert 的持久化提交与恢复；sync/async 共用同一批量执行器。"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4
from sqlalchemy import select, update
from app.database import async_session as session_factory
from app.models.sync import ApiConnection, FieldMapping, SyncTask, DataBuffer, HapWritePlan
from app.services.hap_bulk import is_upsert, execute, summary, fail
from app.services.incremental import seal, unseal
from app.services.field_policy import freeze
from app.services.transformer import transform_payload
from app.services.validator import validate_batch
from app.services.adapter_config import build_adapter_config
from app.adapters.factory import ProtocolAdapterFactory
from app.services.logger import log_execution
from app.adapters.errors import AdapterError
from app.services.failure_logs import error_detail, validation_detail, safe_message


async def submit(session, task, mapping, source, target):
    from app.services.incremental_rounds import lock_task, snapshot
    task = await lock_task(session, task.id)
    if task.status != 'active':
        fail('hap_task_paused', '任务已暂停', 'validate')
    mapping = await session.get(FieldMapping, task.mapping_id, populate_existing=True)
    from app.services.hap_target import validate_policy
    validate_policy(mapping, target, task)
    pending = (await session.scalars(select(DataBuffer).where(DataBuffer.task_id == task.id,
        DataBuffer.round_id.is_(None), DataBuffer.status.in_(['pending', 'processing', 'failed', 'validation_failed'])))).all()
    if pending:
        candidates = [b for b in pending if b.config_snapshot and is_upsert(SimpleNamespace(**unseal(b.config_snapshot)))]
        if len(candidates) != 1 or len(pending) != 1:
            fail('hap_legacy_pending', '存在其他未完成批次，请先处理历史批次', 'validate')
        buffer = candidates[0]
    else:
        mapping = await freeze(session, mapping, source, target)
        saved = snapshot(mapping)
        saved.update(source_connection_id=str(source.id), target_connection_id=str(target.id))
        # 先占用任务批次；网络读取在事务外进行；重复点击只恢复该批次。
        buffer = DataBuffer(id=uuid4(), task_id=task.id, config_snapshot=seal(saved),
                            source_data={'hap_preparing': True}, status='pending')
        session.add(buffer)
    await session.commit()
    if task.sync_mode == 'async':
        from app.celery_app import process_buffer
        process_buffer.delay(str(buffer.id))
        return {'status': 'async_submitted', 'buffer_id': str(buffer.id)}
    return await process_full(buffer.id)


async def process_full(buffer_id):
    buffer_id = UUID(str(buffer_id))
    from app.cache import lock
    token = await lock.acquire_lock(f'hap_buffer:{buffer_id}', ttl=300)
    if not token:
        return {'status': 'processing', 'buffer_id': str(buffer_id)}
    from contextlib import suppress
    lost = asyncio.Event()
    async def renew():
        while True:
            await asyncio.sleep(60)
            try:
                if not await lock.refresh_lock(f'hap_buffer:{buffer_id}', token, ttl=300):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return
    heartbeat = asyncio.create_task(renew())
    stage = 'source_read'
    try:
        async with session_factory() as db:
            buffer = await db.get(DataBuffer, buffer_id)
            if buffer is None or not buffer.config_snapshot:
                fail('hap_buffer_missing', '不存在可恢复的 HAP 批次', 'validate')
            if buffer.status in ('completed', 'cancelled'):
                return {'status': buffer.status, 'buffer_id': str(buffer_id)}
            task = await db.get(SyncTask, buffer.task_id)
            if task is None or task.status != 'active':
                fail('hap_task_paused', '任务已暂停', 'validate')
            mapping = SimpleNamespace(**unseal(buffer.config_snapshot))
            if not is_upsert(mapping) or mapping.sync_mode != 'full':
                fail('hap_mode_invalid', '该批次不是 HAP 全量新增更新', 'validate')
            source = await db.get(ApiConnection, UUID(mapping.source_connection_id))
            target = await db.get(ApiConnection, UUID(mapping.target_connection_id))
            task_id = task.id
            if (buffer.source_data or {}).get('hap_preparing'):
                from app.services.sync_handler import _fetch_source
                await db.commit()
                records = await asyncio.wait_for(_fetch_source(source, mapping), timeout=settings_timeout())
                from app.services.incremental import serialize
                from app.config import settings
                if len(serialize(records).encode()) > settings.incremental_max_bytes:
                    fail('hap_source_too_large', '源数据超过本次处理上限', 'hap_lookup')
                buffer.source_data = {'encrypted': seal(records)}
                await db.commit()
            records = unseal(buffer.source_data['encrypted'])
            stage = 'transform'
            transformed = transform_payload(records, mapping.mapping_config)
            from app.config import settings
            if len(transformed) > settings.incremental_max_rows:
                fail('hap_source_too_large', '源记录数超过本次处理上限', 'hap_lookup')
            validation = validate_batch(transformed, mapping.mapping_config.get('validations') or {})
            if not validation['valid']:
                fail('hap_validation_failed', '转换记录未通过字段校验', 'validate',
                     details=validation_detail(validation))
            buffer.status = 'processing'
            await db.commit()
        async def before_call():
            if lost.is_set():
                fail('hap_lease_lost', '批次执行锁失效')
        adapter = ProtocolAdapterFactory.create(target.protocol, build_adapter_config(target, mapping, 'target'))
        stage = 'push'
        result = await execute(session_factory, buffer_id, adapter, mapping, transformed, task_id, before_call)
        async with session_factory() as db:
            buffer = await db.get(DataBuffer, buffer_id)
            buffer.status, buffer.processed_at = 'completed', datetime.now(timezone.utc).replace(tzinfo=None)
            buffer.transformed_data = {'encrypted': seal(transformed)}
            buffer.error_message = None
            task = await db.get(SyncTask, task_id)
            task.last_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await db.commit()
            await log_execution(db, task_id, 'hap_verify', 'success', buffer_id, response_data=result['body'])
        return {'status': 'success', 'buffer_id': str(buffer_id), **result['body']}
    except Exception as exc:
        message = safe_message(exc)
        async with session_factory() as db:
            buffer = await db.get(DataBuffer, buffer_id)
            if buffer and buffer.status != 'completed':
                buffer.status, buffer.error_message = 'failed', message
                await db.commit()
                await log_execution(db, buffer.task_id, exc.stage if isinstance(exc, AdapterError) else stage, 'failed', buffer_id, error_message=message,
                                    error=exc)
        return {'status': 'failed', 'buffer_id': str(buffer_id), 'error': message}
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        await lock.release_lock(f'hap_buffer:{buffer_id}', token)


def settings_timeout():
    from app.config import settings
    return settings.incremental_prepare_timeout


async def progress(session, task_id):
    plans = (await session.scalars(select(HapWritePlan).where(HapWritePlan.task_id == task_id)
        .order_by(HapWritePlan.created_at.desc()).limit(20))).all()
    return {'plans': [{'id': str(p.id), 'buffer_id': str(p.buffer_id), 'status': p.status,
        **summary(unseal(p.payload)), 'rows': [{'index': op['index'] + 1, 'operation': op['kind'],
            'status': op['status']} for op in unseal(p.payload).get('ops', [])][:100]}
        for p in plans]}


async def cancel_unsent(session, task_id):
    from app.cache import lock
    from app.services.incremental_rounds import lock_task
    from fastapi import HTTPException
    task = await lock_task(session, task_id)
    if task is None:
        raise HTTPException(404, '任务不存在')
    if task.status != 'paused' or task.active_round_id:
        raise HTTPException(409, '先暂停全量任务；增量轮次请使用原轮次恢复')
    buffers = (await session.scalars(select(DataBuffer).where(DataBuffer.task_id == task_id,
        DataBuffer.status.in_(['pending','processing','failed','validation_failed']), DataBuffer.round_id.is_(None)))).all()
    acquired = []
    count = 0
    try:
        for batch in buffers:
            if not batch.config_snapshot or not is_upsert(SimpleNamespace(**unseal(batch.config_snapshot))):
                continue
            name = f'hap_buffer:{batch.id}'
            token = await lock.acquire_lock(name, ttl=300)
            if not token:
                raise HTTPException(409, '批次仍在执行，请等待后重试取消')
            acquired.append((name,token))
            plans = (await session.scalars(select(HapWritePlan).where(HapWritePlan.buffer_id == batch.id).with_for_update())).all()
            for plan in plans:
                if (plan.lease_until and plan.lease_until > datetime.now(timezone.utc).replace(tzinfo=None)) or any(r['attempts'] for r in unseal(plan.payload)['requests']):
                    raise HTTPException(409, '存在在途或已发送请求，不能取消；请恢复原计划回查')
                plan.status, plan.active_target = 'cancelled', None
            batch.status = 'cancelled'
            count += 1
        await session.commit()
        return {'cancelled': count}
    except Exception:
        await session.rollback()
        raise
    finally:
        for name,token in acquired:
            await lock.release_lock(name,token)


async def guard_pending(session, task_ids):
    if not task_ids:
        return
    buffers = (await session.scalars(select(DataBuffer).where(DataBuffer.task_id.in_(task_ids),
        DataBuffer.round_id.is_(None), DataBuffer.status.in_(['pending', 'processing', 'failed', 'validation_failed']),
        DataBuffer.config_snapshot.isnot(None)))).all()
    for buffer in buffers:
        if is_upsert(SimpleNamespace(**unseal(buffer.config_snapshot))):
            from fastapi import HTTPException
            raise HTTPException(409, '存在未完成 HAP 写入批次，请先恢复，不能修改执行配置或删除任务')
