"""Oracle 11g thick 模式真实集成测试

运行前提（与目标系统同源的测试库）：
    docker run -d --name apibridge-oracle-test -p 1521:1521 \\
      -e ORACLE_DISABLE_ASYNCH_IO=true oracleinanutshell/oracle-xe-11g
    Instant Client 19 解压目录经 APIBRIDGE_ORACLE_CLIENT_LIB_DIR 提供（或用默认路径）。

设置环境变量 APIBRIDGE_ORACLE_TEST=1 启用本文件，否则全部跳过
（CI / 无 Oracle 环境不受影响）。
"""
import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("APIBRIDGE_ORACLE_TEST") != "1",
    reason="需要真实 Oracle 11g 测试库（APIBRIDGE_ORACLE_TEST=1 启用）",
)

ORACLE_CONFIG = {
    "host": os.environ.get("APIBRIDGE_ORACLE_HOST", "localhost"),
    "port": int(os.environ.get("APIBRIDGE_ORACLE_PORT", "1521")),
    "sid": os.environ.get("APIBRIDGE_ORACLE_SID", "XE"),
    "username": os.environ.get("APIBRIDGE_ORACLE_USER", "system"),
    "password": os.environ.get("APIBRIDGE_ORACLE_PASSWORD", "oracle"),
}

TABLE = "APIBRIDGE_TEST"

SEED_ROWS = [
    (1, "alpha", 10.5),
    (2, "beta", 20.0),
    (3, "gamma", 30.25),
    (4, "delta", 40.0),
    (5, "epsilon", 50.75),
]


@pytest.fixture()
def oracle_conn():
    from app.config import settings
    from app.connectors.database import OracleConnector

    old_mode = settings.oracle_client_mode
    old_lib = settings.oracle_client_lib_dir
    settings.oracle_client_mode = "thick"
    settings.oracle_client_lib_dir = os.environ.get(
        "APIBRIDGE_ORACLE_CLIENT_LIB_DIR",
        r"E:\tools\instantclient\instantclient_19_25",
    )

    yield OracleConnector(dict(ORACLE_CONFIG))

    settings.oracle_client_mode = old_mode
    settings.oracle_client_lib_dir = old_lib


@pytest.fixture()
async def seeded_table(oracle_conn):
    """建表 + 5 行种子数据；用例结束清池"""
    from app.connectors.database import close_all_pools

    pool = await oracle_conn._get_pool()

    def _ddl():
        with pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"BEGIN EXECUTE IMMEDIATE 'DROP TABLE {TABLE}'; "
                    "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -942 THEN RAISE; END IF; END;"
                )
                cur.execute(
                    f"CREATE TABLE {TABLE} ("
                    "ID NUMBER(10) PRIMARY KEY, "
                    "NAME VARCHAR2(100), AMOUNT NUMBER(10,2))"
                )
                for rid, name, amount in SEED_ROWS:
                    cur.execute(
                        f"INSERT INTO {TABLE} (ID, NAME, AMOUNT) VALUES (:1, :2, :3)",
                        [rid, name, amount],
                    )
            conn.commit()

    await asyncio.to_thread(_ddl)
    yield
    await close_all_pools()


class TestOracleThickConnection:
    @pytest.mark.asyncio
    async def test_connection_ok_and_thick(self, oracle_conn):
        """thick 模式连接 11g 成功，且驱动确实运行在 thick 模式"""
        import oracledb

        result = await oracle_conn.test_connection()
        assert result["status"] == "ok", result["error"]
        assert isinstance(result["latency_ms"], int)
        assert oracledb.is_thin_mode() is False


class TestOracleThickMetadata:
    @pytest.mark.asyncio
    async def test_table_list(self, seeded_table, oracle_conn):
        tables = await oracle_conn.get_table_list()
        assert TABLE in tables

    @pytest.mark.asyncio
    async def test_table_schema(self, seeded_table, oracle_conn):
        schema = await oracle_conn.get_table_schema(TABLE)
        by_name = {c["name"]: c for c in schema}
        assert set(by_name) == {"ID", "NAME", "AMOUNT"}
        assert by_name["ID"]["primary_key"] is True
        assert by_name["NAME"]["type"] == "VARCHAR2"


class TestOracleThickFetch:
    @pytest.mark.asyncio
    async def test_fetch_all(self, seeded_table, oracle_conn):
        rows = await oracle_conn.fetch_data(TABLE)
        assert len(rows) == 5
        assert set(rows[0]) == {"id", "name", "amount"}

    @pytest.mark.asyncio
    async def test_fetch_pagination_offset_limit(self, seeded_table, oracle_conn):
        """11g ROWNUM 分页：第 3–4 行"""
        rows = await oracle_conn.fetch_data(TABLE, order_by="id", limit=2, offset=2)
        assert [r["id"] for r in rows] == [3, 4]

    @pytest.mark.asyncio
    async def test_fetch_limit_only(self, seeded_table, oracle_conn):
        rows = await oracle_conn.fetch_data(TABLE, order_by="id", limit=2)
        assert [r["id"] for r in rows] == [1, 2]

    @pytest.mark.asyncio
    async def test_fetch_where(self, seeded_table, oracle_conn):
        rows = await oracle_conn.fetch_data(TABLE, where={"name": "beta"})
        assert len(rows) == 1 and rows[0]["id"] == 2


class TestOracleThickPush:
    @pytest.mark.asyncio
    async def test_push_insert_batch_real_rowcount(self, seeded_table, oracle_conn):
        """executemany 批量插入返回真实受影响行数"""
        pool = await oracle_conn._get_pool()

        def _truncate():
            with pool.acquire() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"TRUNCATE TABLE {TABLE}")
                conn.commit()

        await asyncio.to_thread(_truncate)

        rows = [{"id": i, "name": f"n{i}", "amount": float(i)} for i in range(1, 6)]
        n = await oracle_conn.push_data(TABLE, rows)
        assert n == 5
        assert len(await oracle_conn.fetch_data(TABLE)) == 5

    @pytest.mark.asyncio
    async def test_push_upsert_merge(self, seeded_table, oracle_conn):
        """MERGE upsert：更新 1 行 + 新增 1 行，返回真实计数"""
        n = await oracle_conn.push_data(
            TABLE,
            [
                {"id": 1, "name": "alpha2", "amount": 11.0},
                {"id": 6, "name": "zeta", "amount": 60.0},
            ],
            key_field="id",
            mode="upsert",
        )
        assert n == 2
        updated = await oracle_conn.fetch_data(TABLE, where={"id": 1})
        assert updated[0]["name"] == "alpha2"
        assert len(await oracle_conn.fetch_data(TABLE)) == 6


class TestOracleIncremental:
    @pytest.mark.asyncio
    async def test_fetch_where_gt_incremental(self, seeded_table, oracle_conn):
        """task 2.2：where_gt 增量过滤对真实 11g 生效"""
        rows = await oracle_conn.fetch_data(TABLE, where_gt={"id": 3}, order_by="id")
        assert [r["id"] for r in rows] == [4, 5]

    @pytest.mark.asyncio
    async def test_where_gt_combined_with_limit(self, seeded_table, oracle_conn):
        """where_gt 与 limit 组合（增量 + 限量拉取）"""
        rows = await oracle_conn.fetch_data(TABLE, where_gt={"id": 1},
                                            order_by="id", limit=2)
        assert [r["id"] for r in rows] == [2, 3]
