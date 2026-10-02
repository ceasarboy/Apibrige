"""Oracle thick 模式冒烟脚本（task 1.3 / 4.2 前置验证）

用法（Instant Client 与服务端地址按需调整）：
    APIBRIDGE_ORACLE_TEST=1 pytest tests/test_oracle_thick.py -v

本脚本用于快速人工验证四步：thick 初始化 → 连接 → 读 → 写。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.connectors import database as db_mod
from app.connectors.database import OracleConnector, close_all_pools
from app.config import settings


async def main() -> int:
    host = os.environ.get("APIBRIDGE_ORACLE_HOST", "localhost")
    port = int(os.environ.get("APIBRIDGE_ORACLE_PORT", "1521"))
    sid = os.environ.get("APIBRIDGE_ORACLE_SID", "XE")
    config = {
        "host": host,
        "port": port,
        "sid": sid,
        "username": os.environ.get("APIBRIDGE_ORACLE_USER", "system"),
        "password": os.environ.get("APIBRIDGE_ORACLE_PASSWORD", "oracle"),
    }
    print(f"[1] thick 模式连接 {host}:{port}/{sid} ...")
    settings.oracle_client_mode = "thick"
    settings.oracle_client_lib_dir = os.environ.get(
        "APIBRIDGE_ORACLE_CLIENT_LIB_DIR",
        r"E:\tools\instantclient\instantclient_19_25",
    )
    conn = OracleConnector(config)

    result = await conn.test_connection()
    print(f"[2] test_connection: {result}")
    if result["status"] != "ok":
        return 1

    tables = await conn.get_table_list()
    print(f"[3] user_tables 前 5 张: {tables[:5]}")

    # 建冒烟表（存在则重建）
    pool = await conn._get_pool()

    def _ddl():
        with pool.acquire() as c:
            with c.cursor() as cur:
                cur.execute(
                    "BEGIN EXECUTE IMMEDIATE 'DROP TABLE APIBRIDGE_SMOKE'; "
                    "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -942 THEN RAISE; END IF; END;"
                )
                cur.execute("CREATE TABLE APIBRIDGE_SMOKE (ID NUMBER(10) PRIMARY KEY, NAME VARCHAR2(100))")
            c.commit()

    await asyncio.to_thread(_ddl)

    n = await conn.push_data(
        "APIBRIDGE_SMOKE",
        [{"id": i, "name": f"row{i}"} for i in range(1, 4)],
        key_field="id",
        mode="upsert",
    )
    rows = await conn.fetch_data("APIBRIDGE_SMOKE", order_by="id")
    print(f"[4] MERGE upsert 写入 {n} 行，回读 {len(rows)} 行: {rows}")
    ok = n == 3 and len(rows) == 3
    await close_all_pools()
    print("SMOKE", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
