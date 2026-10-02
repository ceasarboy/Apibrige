"""同步传输处理器（FastAPI 路由内完成全链路）"""
import time
import asyncio
from datetime import datetime, timezone
from typing import Any
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.sync import DataBuffer, SyncTask, ApiConnection, FieldMapping
from app.services.transformer import transform_payload
from app.services.api_client import resolve_variables
from app.services.adapter_config import build_adapter_config
from app.services.db_client import fetch_from_db, push_to_db
from app.services.validator import validate_batch
from app.services.failure_logs import validation_detail, safe_message
from app.services.logger import log_execution
from app.services.conflict import detect_hash_conflict
from app.adapters.factory import ProtocolAdapterFactory
from app.adapters.base import ProtocolAdapter
from app.cache.idempotency import is_duplicate, mark_processed
from app.services.conflict import compute_record_hash
from app.cache.rate_limiter import rate_limit
from app.cache import lock
from app.metrics_collector import record_sync, record_api_call
from app.adapters.errors import AdapterError


def _is_db(conn: ApiConnection) -> bool:
    """判断连接是否为数据库类型"""
    return bool(conn.db_type)


def _api_adapter(conn: ApiConnection, mapping: FieldMapping,
                 side: str) -> tuple[ProtocolAdapter, dict]:
    """为 API 连接构造协议适配器（与探针同源的唯一传输入口）"""
    config = build_adapter_config(conn, mapping, side=side)
    # 变量解析保留在传输入口（端点/请求头支持 {{变量}}）
    config["base_url"] = resolve_variables(config["base_url"])
    config["headers"] = resolve_variables(config["headers"])
    adapter = ProtocolAdapterFactory.create(conn.protocol, config)
    return adapter, config


async def _fetch_source(source_conn: ApiConnection, mapping: FieldMapping,
                        task: SyncTask | None = None) -> Any:
    """拉取源数据：数据库走 DatabaseConnector，API 走协议适配器"""
    if _is_db(source_conn):
        incremental_field = getattr(mapping, "incremental_field", None) if mapping.sync_mode == 'incremental' else None
        last = getattr(task, "last_sync_value", None) if task else None
        return await fetch_from_db(
            db_type=source_conn.db_type,
            db_config=source_conn.db_config or {},
            table=mapping.source_table or "",
            source_endpoint=mapping.source_endpoint,
            incremental_field=incremental_field,
            last_sync_value=last,
        )
    adapter, config = _api_adapter(source_conn, mapping, side="source")
    # 源稳定性双读（删差在途防护）：hap_delete_sync（发行包键）与 delete_sync（既有映射键）
    # 同等生效——只认新键会让既有映射的删差在源翻页期间读到不完整键集而误删
    _stable_needed = source_conn.protocol == 'hap' and (
        (mapping.mapping_config or {}).get('hap_delete_sync')
        or (mapping.mapping_config or {}).get('delete_sync'))
    if _stable_needed:
        from app.config import settings
        adapter.config.update(_stable_scan=True, _scan_max_rows=settings.incremental_max_rows,
                              _scan_max_bytes=settings.incremental_max_bytes)
    endpoint = resolve_variables(mapping.source_endpoint or "")
    t0 = time.time()
    try:
        result = await adapter.fetch_data(endpoint)
        if _stable_needed:
            from app.services.incremental import serialize
            again = await adapter.fetch_data(endpoint)
            if serialize(result) != serialize(again):
                raise AdapterError('hap_source_changed', '源集合读取期间发生变化，未执行写入或删除', 'source_read')
    except Exception:
        record_api_call(config['base_url'], 'failed', time.time() - t0)
        raise
    record_api_call(config['base_url'], 'ok', time.time() - t0)
    return result


async def _push_target(target_conn: ApiConnection, mapping: FieldMapping, data: Any) -> dict:
    """推送数据到目标：数据库走 DatabaseConnector，API 走协议适配器"""
    if _is_db(target_conn):
        mapping_cfg = mapping.mapping_config or {}
        key_field = mapping_cfg.get("key_field")
        return await push_to_db(
            db_type=target_conn.db_type,
            db_config=target_conn.db_config or {},
            table=mapping.target_table or "",
            data=data if isinstance(data, list) else [data],
            target_endpoint=mapping.target_endpoint,
            key_field=key_field,
            mode="upsert" if key_field else "insert",
        )
    adapter, config = _api_adapter(target_conn, mapping, side="target")
    endpoint = resolve_variables(mapping.target_endpoint or "")
    t0 = time.time()
    try:
        from app.services.hap_target import enabled, push
        result = await push(adapter, config, mapping, data) if enabled(mapping) else await adapter.push_data(endpoint, data)
    except Exception:
        record_api_call(config['base_url'], 'failed', time.time() - t0)
        raise
    if not result.get('skipped'):
        record_api_call(config['base_url'], 'ok', time.time() - t0)
    return result


async def _completed_record_hashes(session, task_id, exclude_buffer_id) -> set[str]:
    """任务内**成功完成**批次的记录哈希集合（冲突检测比对源）

    只认 status=completed：失败批次的 hash 仍在库中（写入发生在推送前），
    若一并计入，失败后的重试会把"上次没写成功的数据"误判为已存在而永久
    跳过——数据补不上且日志记 success（审查 P1）。
    """
    from sqlalchemy import select as sel
    hash_result = await session.execute(
        sel(DataBuffer.data_hash).where(
            DataBuffer.task_id == task_id,
            DataBuffer.data_hash.isnot(None),
            DataBuffer.status == "completed",
            DataBuffer.id != exclude_buffer_id,
        )
    )
    return {h
            for row in hash_result.scalars().all()
            for h in (row or "").split(",") if h}


async def sync_transfer(session: AsyncSession, task: SyncTask,
                        mapping: FieldMapping, source_conn: ApiConnection,
                        target_conn: ApiConnection,
                        source_data=None) -> dict:
    """同步传输：拉取源→写data_buffer→转换→推送目标→更新状态

    Args:
        source_data: 可选，路由器统一拉取时传入；为 None 则内部拉取

    Returns:
        {"status": "success"|"failed", "buffer_id": "...", "error": "..."}
    """
    start = time.time()
    from app.services.hap_target import validate_policy, enabled
    validate_policy(mapping, target_conn, task, injected=source_data is not None and enabled(mapping))

    from app.services.hap_bulk import is_upsert
    if is_upsert(mapping) and mapping.sync_mode == 'full':
        from app.services.hap_execution import submit
        return await submit(session, task, mapping, source_conn, target_conn)
    if mapping.sync_mode == 'incremental':
        from app.services.incremental import reject
        from app.services import incremental_rounds as rounds
        if source_data is not None:
            reject('incremental_push_unsupported', '增量任务必须读取配置的源，不支持 Webhook 注入数据')
        result = await rounds.start_round(session, task, mapping, source_conn, target_conn)
        await session.commit()
        for batch in result['batches']:
            await rounds.process_batch(batch['id'])
        await session.refresh(task)
        return await rounds.round_detail(session, task, __import__('uuid').UUID(result['round_id']))

    policy_enabled = (mapping.mapping_config or {}).get('field_policy') == 'inherit'
    from app.services.field_policy import freeze, execution_key
    mapping = await freeze(session, mapping, source_conn, target_conn)
    idempotency_key = execution_key(task.id, mapping.mapping_config) if policy_enabled else str(task.id)

    # 1. 分布式锁防止并发
    lock_token = await lock.acquire_lock(f"sync_task:{task.id}", ttl=300)
    if lock_token is None:
        return {"status": "failed", "error": "任务正在执行中"}

    buffer = None
    stage = 'source_read'
    lost_lock = asyncio.Event()
    async def renew_lock():
        while True:
            await asyncio.sleep(100)
            try:
                if not await lock.refresh_lock(f'sync_task:{task.id}', lock_token, ttl=300):
                    lost_lock.set()
                    return
            except Exception:
                lost_lock.set()
                return
    heartbeat = asyncio.create_task(renew_lock())
    # 注入标记（取数前记录）：webhook 等推入的单条不是完整源集合，
    # 删除对账阶段据此跳过（source_data 取数后必非 None，不能在后面再判）
    _source_injected = source_data is not None
    try:
        # 2. 拉取源数据（若未由路由器提供）
        if source_data is None:
            source_data = await _fetch_source(source_conn, mapping, task)

        # 3. 幂等检查
        # delete_sync 开启时为对账式同步：即使源数据未变也要执行删除比对，
        # 不做幂等短路（否则已删行的清理永远不会执行）
        delete_sync_enabled = bool((mapping.mapping_config or {}).get("delete_sync") or (mapping.mapping_config or {}).get("hap_delete_sync"))
        if not delete_sync_enabled:
            if await is_duplicate(idempotency_key, source_data if isinstance(source_data, dict) else {"data": source_data}):
                return {"status": "success", "buffer_id": None, "message": "重复数据已跳过"}

        # 4. 写 data_buffer
        source_payload = source_data if isinstance(source_data, dict) else {"data": source_data}
        buffer = DataBuffer(
            task_id=task.id,
            source_data=source_payload,
            status="processing",
        )
        session.add(buffer)
        await session.commit()

        stage = 'transform'
        # 5. 转换（列表源逐条转换，与探针一致）
        transformed = transform_payload(source_data, mapping.mapping_config)
        buffer.transformed_data = transformed

        # 5.5 数据校验（P1：必填/类型/范围/正则）
        validations = mapping.mapping_config.get("validations") if mapping.mapping_config else None
        if validations:
            # 转换后的数据可能是单条或多条
            records = transformed if isinstance(transformed, list) else [transformed]
            validation_result = validate_batch(records, validations)
            if not validation_result["valid"]:
                buffer.status = "validation_failed"
                buffer.error_message = f"数据校验失败: {validation_result['failed']}/{validation_result['total']} 条不通过"
                await session.commit()
                await log_execution(session, task.id, "validate", "failed", buffer.id,
                                    error_message=buffer.error_message,
                                    response_data={'error_detail': validation_detail(validation_result)}, mapping_config=mapping.mapping_config)
                return {
                    "status": "validation_failed",
                    "buffer_id": str(buffer.id),
                    "error": buffer.error_message,
                    "details": validation_result["errors"],
                }

        # task 7.6：按记录写入 data_hash（与 detect_hash_conflict 同一哈希函数）
        records_now = transformed if isinstance(transformed, list) else [transformed]
        buffer.data_hash = ",".join(compute_record_hash(r) for r in records_now)

        records_full = list(records_now)

        # 5.7 冲突检测（P1：有 conflict_strategy 时启用）
        if task.conflict_strategy and task.conflict_strategy != "overwrite":
            records = transformed if isinstance(transformed, list) else [transformed]
            # 通过 data_buffer 获取已有哈希——只认**成功完成**批次：
            # 失败批次的 hash 若也计入，重试会把"上次没写成功的数据"误判为
            # 已存在而永久跳过（审查 P1）；助手函数可单测
            existing_hashes = await _completed_record_hashes(
                session, task.id, buffer.id)
            to_push, conflict_count = detect_hash_conflict(
                records, existing_hashes, strategy=task.conflict_strategy
            )
            if conflict_count > 0:
                await log_execution(session, task.id, "conflict", "skipped", buffer.id,
                                    error_message=f"跳过 {conflict_count} 条冲突数据，策略: {task.conflict_strategy}")
                # task 7.7：skip 真实生效——全部被跳过时不再推送，也不谎报成功
                if not to_push and not delete_sync_enabled:
                    buffer.status = "completed"
                    buffer.processed_at = datetime.now(timezone.utc)
                    await session.commit()
                    await log_execution(session, task.id, "push", "skipped", buffer.id,
                                        error_message=f"全部 {conflict_count} 条为冲突数据，按 skip 策略跳过推送")
                    record_sync("success", getattr(task, "task_type", "realtime") or "realtime",
                                time.time() - start)
                    return {"status": "skipped", "buffer_id": str(buffer.id),
                            "skipped": conflict_count}
                transformed = to_push

        # 6. 限流检查
        if not await rate_limit(f"target:{target_conn.id}"):
            buffer.status = "failed"
            buffer.error_message = "目标API限流"
            await session.commit()
            await log_execution(session, task.id, "push", "failed", buffer.id, error_message="限流")
            return {"status": "failed", "buffer_id": str(buffer.id), "error": "目标API限流"}

        stage = 'push'
        # 7. 推送目标
        if lost_lock.is_set():
            raise AdapterError('sync_lock_lost', '任务锁已失效，停止后续写入', 'push')
        result = await _push_target(target_conn, mapping, transformed) if transformed else {'status_code': 200, 'body': {'skipped': len(records_full)}}
        if result.get('skipped') and not delete_sync_enabled:
            buffer.status = 'completed'
            await session.commit()
            await log_execution(session, task.id, 'push', 'skipped', buffer.id, response_data=result)
            return {'status':'skipped', 'buffer_id':str(buffer.id), 'message':'无可发送记录'}

        # 8. 更新状态
        buffer.status = "completed"
        buffer.processed_at = datetime.now(timezone.utc)
        await session.commit()

        await log_execution(session, task.id, "push", "success", buffer.id,
                            request_data=result.get('request'), response_data=result, duration_ms=int((time.time() - start) * 1000))

        stage = 'delete'
        # 删除基于完整转换记录（冲突过滤前）；空键/不完整键不删除。
        # 推入数据（source_data，如 webhook 单条）不是完整源集合——删差会误删目标
        # 其余行，一律跳过（与 webhook 层的 422 拦截构成双层防御）
        cfg = mapping.mapping_config or {}
        if (not _source_injected and (mapping.sync_mode or 'full') == 'full'
                and (cfg.get('delete_sync') or cfg.get('hap_delete_sync'))):
            if lost_lock.is_set():
                raise AdapterError('sync_lock_lost', '任务锁已失效，未执行删除', 'delete')
            # HAP 目标：hap_delete_sync（发行包键）与 delete_sync（既有映射键）同等生效——
            # 原实现按 delete_sync 对 HAP 目标做逐行删差，迁移到 hap_target.reconcile 后
            # 必须继续认这个键，否则既有映射的删除同步会静默失效
            if target_conn.protocol == 'hap' and (cfg.get('hap_delete_sync') or cfg.get('delete_sync')):
                from app.services.hap_target import reconcile
                adapter, config = _api_adapter(target_conn, mapping, side='target')
                deletion = await reconcile(adapter, config, mapping, records_full)
                await log_execution(session, task.id, 'delete', 'skipped' if deletion.get('skipped') else 'success', buffer.id, response_data=deletion)
            elif _is_db(target_conn) and cfg.get('delete_sync'):
                from app.services.hap_target import value
                # 纵深防御（validate_policy 已在入口拒绝）：过滤/视图源的删差会误删
                # 子集之外的目标行——万一守卫被绕过，这里宁可跳过也不执行
                from app.services.sync_config import side_config
                if side_config(cfg, 'source').get('filters') or side_config(cfg, 'source').get('view_id'):
                    await log_execution(session, task.id, 'delete', 'skipped', buffer.id,
                                        error_message='源为过滤/视图集合，删除对账已跳过（完整源集合守卫）')
                else:
                    key = cfg.get('key_field')
                    keys = [value(r, key) for r in records_full] if key else []
                    if not keys or any(not k for k in keys):
                        await log_execution(session, task.id, 'delete', 'skipped', buffer.id, error_message='源键集合为空或不完整，跳过删除')
                    else:
                        from app.connectors.database import get_database_connector
                        connector = get_database_connector(target_conn.db_type, target_conn.db_config or {})
                        try:
                            target_rows = await connector.fetch_data(mapping.target_table, columns=[key])
                            stale = {value(r, key) for r in target_rows} - set(keys) - {''}
                            deleted = await connector.delete_rows(mapping.target_table, key, sorted(stale)) if stale else 0
                            await log_execution(session, task.id, 'delete', 'success', buffer.id, response_data={'deleted': deleted})
                        finally:
                            await connector.disconnect()

        # task 2.3：维护 last_run_at
        task.last_run_at = datetime.now(timezone.utc)

        # task 2.2：增量同步成功后推进 last_sync_value（取源记录增量字段最大值）
        incremental_field = getattr(mapping, "incremental_field", None) if mapping.sync_mode == 'incremental' else None
        if incremental_field and isinstance(source_data, list) and source_data:
            values = [r.get(incremental_field) for r in source_data
                      if isinstance(r, dict) and r.get(incremental_field) is not None]
            if values:
                task.last_sync_value = str(max(values))
        await session.commit()

        record_sync("success", getattr(task, "task_type", "realtime") or "realtime",
                    time.time() - start)
        # 幂等标记只在推送成功后写入：失败重试可重跑
        await mark_processed(idempotency_key,
                             source_payload if isinstance(source_payload, dict)
                             else {"data": source_payload})
        return {"status": "success", "buffer_id": str(buffer.id)}

    except Exception as e:
        # 失败处理
        buffer_id = buffer.id if buffer is not None else None
        if buffer_id:
            buffer.status = "failed"
            buffer.error_message = safe_message(e)
            await session.commit()
        await log_execution(session, task.id, e.stage if isinstance(e, AdapterError) else stage, 'failed', buffer_id,
                            error=e, mapping_config=mapping.mapping_config)

        record_sync("failed", getattr(task, "task_type", "realtime") or "realtime",
                    time.time() - start)
        # fallback_to_async 开关（任务3.2）
        if task.fallback_to_async and not isinstance(e, AdapterError):
            from app.services.async_handler import submit_async
            # 注意：source_data 是函数参数、恒在 dir() 里——旧写法 `'source_data' in dir()`
            # 恒真，源读失败时会提交 None 撞 NOT NULL 约束、掩盖真实故障
            if source_data is None:
                return {"status": "failed", "buffer_id": str(buffer_id) if buffer_id else None,
                        "error": f"{type(e).__name__}: 源数据读取失败，未提交异步"}
            async_id = await submit_async(session, task.id, source_data)
            return {"status": "fallback_to_async", "buffer_id": str(async_id), "sync_error": str(e)}

        return {"status": "failed", "buffer_id": str(buffer_id) if buffer_id else None, "error": str(e)}

    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass
        await lock.release_lock(f"sync_task:{task.id}", lock_token)
