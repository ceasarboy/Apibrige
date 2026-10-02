"""传输模式路由器（按任务 sync_mode 分发到同步/异步处理器）"""
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.sync import SyncTask, ApiConnection, FieldMapping
from app.services.api_client import resolve_variables
from app.services.adapter_config import build_adapter_config
from app.services.db_client import fetch_from_db
from app.services.orchestrator import check_dependency_ready
from app.adapters.factory import ProtocolAdapterFactory
from app.services import sync_handler, async_handler
from app.config import settings


def _is_db(conn: ApiConnection) -> bool:
    """判断连接是否为数据库类型"""
    return bool(conn.db_type)


async def _fetch_source(source_conn: ApiConnection, mapping: FieldMapping,
                        task: SyncTask | None = None) -> dict:
    """拉取源数据：数据库走 DatabaseConnector，API 走协议适配器（同源）"""
    if _is_db(source_conn):
        incremental_field = getattr(mapping, "incremental_field", None) if mapping.sync_mode == 'incremental' else None
        last = getattr(task, "last_sync_value", None) if task else None
        data = await fetch_from_db(
            db_type=source_conn.db_type,
            db_config=source_conn.db_config or {},
            table=mapping.source_table or "",
            source_endpoint=mapping.source_endpoint,
            incremental_field=incremental_field,
            last_sync_value=last,
        )
        return {"data": data} if not isinstance(data, dict) else data
    config = build_adapter_config(source_conn, mapping, side="source")
    config["base_url"] = resolve_variables(config["base_url"])
    config["headers"] = resolve_variables(config["headers"])
    adapter = ProtocolAdapterFactory.create(source_conn.protocol, config)
    endpoint = resolve_variables(mapping.source_endpoint or "")
    return await adapter.fetch_data(endpoint)


async def _fetch_with_failure_log(session, task, source_conn, mapping):
    try:
        return await _fetch_source(source_conn, mapping, task)
    except Exception as error:
        from app.services.logger import log_execution
        await log_execution(session, task.id, 'source_read', 'failed', error=error)
        error._execution_logged = True
        raise


async def route_transfer(session: AsyncSession, task: SyncTask,
                         mapping: FieldMapping, source_conn: ApiConnection,
                         target_conn: ApiConnection) -> dict:
    task_id = task.id
    try:
        return await _route_transfer(session, task, mapping, source_conn, target_conn)
    except Exception as error:
        if not getattr(error, '_execution_logged', False):
            from app.services.logger import log_execution
            await session.rollback()
            await log_execution(session, task_id, 'prepare', 'failed', error=error)
        raise


async def _route_transfer(session: AsyncSession, task: SyncTask,
                         mapping: FieldMapping, source_conn: ApiConnection,
                         target_conn: ApiConnection) -> dict:
    """传输模式路由：根据 task.sync_mode 分发

    sync_mode:
        - sync: 走同步处理器
        - async: 走异步处理器
        - auto: 按数据量阈值选择（<阈值 同步，>=阈值 异步）

    Returns:
        处理结果
    """
    # 0. 检查任务依赖（P1：任务编排）
    ready, dep_reason = await check_dependency_ready(session, task)
    if not ready:
        from app.services.logger import log_execution
        await log_execution(session, task.id, 'dependency', 'failed', error_message=dep_reason,
                            response_data={'error_detail':{'code':'dependency_not_ready','stage':'dependency'}})
        return {"status": "blocked", "error": dep_reason, "depends_on": str(task.depends_on_task_id)}

    from app.services.hap_target import validate_policy
    validate_policy(mapping, target_conn, task)
    from app.services.hap_bulk import is_upsert
    if is_upsert(mapping) and mapping.sync_mode == 'full':
        from app.services.hap_execution import submit
        return await submit(session, task, mapping, source_conn, target_conn)
    mode = task.sync_mode

    # 删除对账只支持 sync 通道（GPOST 原有守卫，合并时丢失——回归补回）：
    # 异步/触发异步分支的 auto 不执行删差，静默不删除且幂等短路会掩盖"对账没在跑"。
    # 双键口径：delete_sync（既有映射）与 hap_delete_sync（发行包键）同等生效。
    _delete_sync = bool((mapping.mapping_config or {}).get("delete_sync")
                        or (mapping.mapping_config or {}).get("hap_delete_sync"))
    _del_async_err = ("delete_sync 仅支持 sync 同步通道；请将任务 sync_mode 改为 sync"
                       "（async/auto 通道不做删除对账，避免静默失效）")

    if mapping.sync_mode == 'incremental':
        from app.services.incremental import validate
        validate(mapping, source_conn, task, target_conn)
        if mode == 'async':
            from app.services.incremental_rounds import start_round
            return await start_round(session, task, mapping, source_conn, target_conn)

    if mode == "sync":
        return await sync_handler.sync_transfer(session, task, mapping, source_conn, target_conn)

    if mode == "async":
        if _delete_sync:
            return {"status": "failed", "error": _del_async_err}
        # 异步模式：先拉取源数据 → 提交异步
        source_data = await _fetch_with_failure_log(session, task, source_conn, mapping)
        buffer_id = await async_handler.submit_async(session, task.id, _normalize(source_data))
        return {"status": "async_submitted", "buffer_id": buffer_id}

    if mode == "auto":
        # auto 模式：拉取源数据 → 按数据量选择
        # 删除对账仅支持 sync 通道：小数据走 sync_transfer 时 router 自取的完整源
        # 与 webhook 推入不可区分（source_data 均非 None），会被"注入守卫"跳过删除；
        # 大数据走异步又不支持。两种结局各错一边 → 入口显式拒绝（与 async 同口径）
        if _delete_sync:
            return {"status": "failed", "error": _del_async_err}
        source_data = await _fetch_with_failure_log(session, task, source_conn, mapping)
        count = _estimate_count(source_data)
        threshold = task.auto_threshold or settings.auto_sync_threshold

        if count < threshold:
            return await sync_handler.sync_transfer(
                session, task, mapping, source_conn, target_conn, source_data=source_data
            )
        else:
            buffer_id = await async_handler.submit_async(session, task.id, _normalize(source_data))
            return {"status": "async_submitted", "buffer_id": buffer_id, "reason": f"数据量{count}>={threshold}"}

    return {"status": "failed", "error": f"未知 sync_mode: {mode}"}


def _estimate_count(source_data) -> int:
    """预估数据量"""
    if isinstance(source_data, list):
        return len(source_data)
    if isinstance(source_data, dict):
        # 尝试常见的列表字段
        for key in ("items", "data", "results", "list"):
            if key in source_data and isinstance(source_data[key], list):
                return len(source_data[key])
        return 1
    return 1


def _normalize(source_data) -> dict:
    """规范化为 dict（data_buffer 要求 JSONB）"""
    if isinstance(source_data, dict):
        return source_data
    return {"data": source_data}
