"""数据库客户端 — 统一的数据读写接口

与 api_client.py 对称，通过 DatabaseConnector 提供 fetch/push 操作。
sync_handler 和 router 通过此模块统一调用 DB 数据源。
"""
from typing import Any
from app.connectors.database import get_database_connector, DatabaseConnector


async def fetch_from_db(
    db_type: str,
    db_config: dict,
    table: str,
    source_endpoint: str | None = None,
    batch_size: int = 1000,
    incremental_field: str | None = None,
    last_sync_value: str | None = None,
) -> list[dict]:
    """从数据库表拉取数据

    Args:
        db_type: 数据库类型 (mysql/postgresql/sqlite/oracle)
        db_config: 连接配置
        table: 源表名
        source_endpoint: 可选的 SQL 片段（预留）
        batch_size: 单次查询上限
        incremental_field: 增量字段；与 last_sync_value 同时提供时只拉取
                           该字段大于其值的行，并按该字段升序分页（task 2.2）
        last_sync_value: 上次同步到的增量值

    Returns:
        数据字典列表
    """
    from app.connectors.batch import batch_fetch

    where_gt = ({incremental_field: last_sync_value}
                if incremental_field and last_sync_value else None)
    order_by = incremental_field if incremental_field else None

    connector = get_database_connector(db_type, db_config)
    try:
        # task 7.4：经 batch_fetch 分页拉取，避免单条巨查询；内存按页消化
        data: list[dict] = []
        async for page in batch_fetch(
            lambda offset, limit: connector.fetch_data(
                table=table, offset=offset, limit=limit,
                order_by=order_by, where_gt=where_gt),
            batch_size=batch_size,
        ):
            data.extend(page)
        return data
    finally:
        await connector.disconnect()


async def push_to_db(
    db_type: str,
    db_config: dict,
    table: str,
    data: list[dict],
    target_endpoint: str | None = None,
    key_field: str | None = None,
    mode: str = "insert",
) -> dict:
    """推送数据到数据库表

    Args:
        db_type: 数据库类型
        db_config: 连接配置
        table: 目标表名
        data: 要写入的数据
        target_endpoint: 可选的 SQL 片段（预留）
        key_field: 主键字段（用于 upsert/replace）
        mode: 写入模式 insert|upsert|replace

    Returns:
        {"status_code": 200, "body": {"rows_affected": N}}
    """
    connector = get_database_connector(db_type, db_config)
    try:
        rows = await connector.push_data(table=table, data_list=data, key_field=key_field, mode=mode)
        return {"status_code": 200, "body": {"rows_affected": rows}}
    finally:
        await connector.disconnect()


async def test_db_connection(db_type: str, db_config: dict) -> dict:
    """测试数据库连接

    Returns:
        {"status": "ok"|"failed", "latency_ms": int, "error": str|None}
    """
    connector = get_database_connector(db_type, db_config)
    try:
        return await connector.test_connection()
    finally:
        await connector.disconnect()
