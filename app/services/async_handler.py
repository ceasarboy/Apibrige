"""异步传输处理器（先持久化再投递 + Worker消费逻辑）"""
import time
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.sync import DataBuffer, SyncTask, ApiConnection, FieldMapping
from app.services.transformer import transform_payload
from app.services.api_client import resolve_variables
from app.services.adapter_config import build_adapter_config
from app.services.db_client import push_to_db
from app.services.validator import validate_batch
from app.services.failure_logs import validation_detail, safe_message
from app.services.logger import log_execution
from app.adapters.factory import ProtocolAdapterFactory
from app.cache.idempotency import is_duplicate, mark_processed
from app.cache.rate_limiter import rate_limit
from app.adapters.errors import AdapterError
from app.metrics_collector import record_api_call


def _is_db(conn: ApiConnection) -> bool:
    """判断连接是否为数据库类型"""
    return bool(conn.db_type)


async def _push_target(target_conn: ApiConnection, mapping: FieldMapping, data) -> dict:
    """推送数据到目标：数据库走 DatabaseConnector，API 走协议适配器（同源）"""
    if _is_db(target_conn):
        # 与同步通道（sync_handler._push_target）同口径：配置了 key_field 即 upsert，
        # 否则 insert——异步/降级重跑才不会重复插入或撞唯一约束（审查 P1）
        mapping_cfg = mapping.mapping_config or {}
        key_field = mapping_cfg.get("key_field")
        return await push_to_db(
            db_type=target_conn.db_type,
            db_config=target_conn.db_config or {},
            table=mapping.target_table or "",
            data=data if isinstance(data, list) else [data],
            key_field=key_field,
            mode="upsert" if key_field else "insert",
        )
    config = build_adapter_config(target_conn, mapping, side="target")
    config["base_url"] = resolve_variables(config["base_url"])
    config["headers"] = resolve_variables(config["headers"])
    adapter = ProtocolAdapterFactory.create(target_conn.protocol, config)
    endpoint = resolve_variables(mapping.target_endpoint or "")
    start = time.time()
    try:
        result = await adapter.push_data(endpoint, data)
    except Exception:
        record_api_call(config['base_url'], 'failed', time.time() - start)
        raise
    if not result.get('skipped'):
        record_api_call(config['base_url'], 'ok', time.time() - start)
    return result


async def submit_async(session: AsyncSession, task_id, source_data: dict) -> str:
    """异步提交入口：写PostgreSQL(pending) → 投递RabbitMQ → 返回任务ID

    Returns:
        buffer_id（供后续查询状态）
    """
    task = await session.get(SyncTask, task_id)
    if task is not None:
        mapping = await session.get(FieldMapping, task.mapping_id)
        if mapping is not None:
            from app.services.hap_target import enabled
            if enabled(mapping):
                raise AdapterError('hap_policy_mode', 'HAP 目标策略不支持异步', 'validate')
        if mapping is not None and mapping.sync_mode == 'incremental':
            from app.services.incremental import reject
            reject('incremental_round_required', '增量数据必须通过轮次入口派发')
    config_snapshot = None
    if task is not None and mapping is not None and (mapping.mapping_config or {}).get('field_policy') == 'inherit':
        from app.services.field_policy import freeze
        from app.services.incremental_rounds import snapshot
        from app.services.incremental import seal
        frozen = await freeze(session, mapping)
        saved = snapshot(frozen)
        saved.update(source_connection_id=str(mapping.source_connection_id), target_connection_id=str(mapping.target_connection_id))
        config_snapshot = seal(saved)
    # 1. 持久化到 data_buffer（真相源）
    buffer = DataBuffer(
        task_id=task_id,
        source_data=source_data,
        config_snapshot=config_snapshot,
        status="pending",
    )
    session.add(buffer)
    await session.commit()

    # 2. 投递到 Celery（通过 RabbitMQ broker）
    from app.celery_app import process_buffer
    process_buffer.delay(str(buffer.id))

    return str(buffer.id)


async def process_async(buffer_id: str):
    """Worker 消费逻辑：幂等检查→转换→限流→推送→更新状态"""
    from app.database import async_session
    from uuid import UUID
    buffer_id = UUID(str(buffer_id))

    async with async_session() as session:
        # 1. 读取 buffer
        result = await session.execute(select(DataBuffer).where(DataBuffer.id == buffer_id))
        buffer = result.scalar_one_or_none()
        if buffer is None or buffer.status == "completed":
            return  # 已处理或不存在
        if isinstance(buffer.round_id, __import__('uuid').UUID):
            from app.services.incremental_rounds import process_batch
            await process_batch(buffer_id)
            return

        # 2. 读取任务和连接配置
        task_result = await session.execute(select(SyncTask).where(SyncTask.id == buffer.task_id))
        task = task_result.scalar_one()
        mapping_result = await session.execute(select(FieldMapping).where(FieldMapping.id == task.mapping_id))
        mapping = mapping_result.scalar_one()
        if isinstance(buffer.config_snapshot, str) and buffer.config_snapshot:
            from app.services.incremental import unseal
            from types import SimpleNamespace
            saved = unseal(buffer.config_snapshot)
            from uuid import UUID
            saved['source_connection_id'] = UUID(saved['source_connection_id'])
            saved['target_connection_id'] = UUID(saved['target_connection_id'])
            mapping = SimpleNamespace(**saved)
        from app.services.hap_bulk import is_upsert
        if is_upsert(mapping):
            await session.commit()
            from app.services.hap_execution import process_full
            return await process_full(buffer_id)
        if mapping.sync_mode == 'incremental':
            buffer.status = 'failed'
            buffer.error_message = '旧版增量批次缺少轮次边界，不能继续执行'
            await session.commit()
            return
        target_result = await session.execute(
            select(ApiConnection).where(ApiConnection.id == mapping.target_connection_id))
        target_conn = target_result.scalar_one()
        from app.services.field_policy import execution_key
        idempotency_key = execution_key(task.id, mapping.mapping_config) if isinstance(buffer.config_snapshot, str) and buffer.config_snapshot else str(task.id)

        start = time.time()
        buffer.status = "processing"
        await session.commit()

        stage = 'transform'
        try:
            # 3. 幂等检查
            if await is_duplicate(idempotency_key, buffer.source_data):
                buffer.status = "completed"
                buffer.processed_at = datetime.now(timezone.utc)
                await session.commit()
                return

            # 4. 转换（列表源逐条转换）
            transformed = transform_payload(buffer.source_data, mapping.mapping_config)
            buffer.transformed_data = transformed

            # 4.5 数据校验
            validations = mapping.mapping_config.get("validations") if mapping.mapping_config else None
            if validations:
                records = transformed if isinstance(transformed, list) else [transformed]
                vr = validate_batch(records, validations)
                if not vr["valid"]:
                    buffer.status = "validation_failed"
                    buffer.error_message = f"数据校验失败: {vr['failed']}/{vr['total']} 条不通过"
                    await session.commit()
                    await log_execution(session, task.id, "validate", "failed", buffer.id,
                                        error_message=buffer.error_message,
                                    response_data={'error_detail': validation_detail(vr)}, mapping_config=mapping.mapping_config)
                    return

            # 5. 限流检查
            if not await rate_limit(f"target:{target_conn.id}"):
                buffer.status = "failed"
                buffer.error_message = "目标API限流"
                await session.commit()
                await log_execution(session, task.id, "push", "failed", buffer.id, error_message="限流")
                return

            stage = 'push'
            # 6. 推送目标
            result = await _push_target(target_conn, mapping, transformed)
            if result.get('skipped'):
                buffer.status = 'completed'
                await session.commit()
                await log_execution(session, task.id, 'push', 'skipped', buffer.id, response_data=result)
                return

            # 幂等标记只在推送成功后写入；同时维护 last_run_at
            await mark_processed(idempotency_key, buffer.source_data)
            task.last_run_at = datetime.now(timezone.utc)

            # 7. 更新状态
            buffer.status = "completed"
            buffer.processed_at = datetime.now(timezone.utc)
            await session.commit()
            await log_execution(session, task.id, "push", "success", buffer.id,
                                request_data=result.get('request'), response_data=result, duration_ms=int((time.time() - start) * 1000))

        except Exception as e:
            buffer.status = "failed"
            buffer.error_message = safe_message(e)
            buffer.retry_count += 1
            await session.commit()
            await log_execution(session, task.id, e.stage if isinstance(e, AdapterError) else stage, 'failed', buffer.id,
                                error=e, mapping_config=mapping.mapping_config)
