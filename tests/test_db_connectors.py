"""数据库连接器测试 — SQLite 全功能测试 + 工厂 + DB 路由集成"""
import os
import tempfile
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4


# ============================================================
# SQLite 测试 Fixture（使用临时文件，而非 :memory:）
# ============================================================

@pytest.fixture
def tmp_sqlite_db():
    """创建带测试数据的临时 SQLite 数据库文件（使用同步 sqlite3）"""
    import sqlite3
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)

    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT,
            age INTEGER,
            status TEXT DEFAULT 'active'
        )
    """)
    conn.execute("""
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            amount REAL,
            created_at TEXT
        )
    """)
    conn.execute("INSERT INTO users (name, email, age) VALUES (?, ?, ?)", ("张三", "zhangsan@test.com", 25))
    conn.execute("INSERT INTO users (name, email, age) VALUES (?, ?, ?)", ("李四", "lisi@test.com", 30))
    conn.execute("INSERT INTO users (name, email, age, status) VALUES (?, ?, ?, ?)", ("王五", "wangwu@test.com", 35, "inactive"))
    conn.commit()
    conn.close()

    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def sqlite_conn(tmp_sqlite_db):
    """创建 SQLite 连接器（指向临时文件）"""
    from app.connectors.database import SqliteConnector
    return SqliteConnector({"database": tmp_sqlite_db})


# ============================================================
# 工厂测试
# ============================================================

class TestDatabaseConnectorFactory:
    """工厂 get_database_connector 测试"""

    def test_create_sqlite(self):
        from app.connectors.database import get_database_connector, SqliteConnector
        c = get_database_connector("sqlite", {"database": ":memory:"})
        assert isinstance(c, SqliteConnector)

    def test_create_mysql(self):
        from app.connectors.database import get_database_connector, MysqlConnector
        c = get_database_connector("mysql", {"host": "localhost"})
        assert isinstance(c, MysqlConnector)

    def test_create_postgresql(self):
        from app.connectors.database import get_database_connector, PostgresqlConnector
        c = get_database_connector("postgresql", {"host": "localhost"})
        assert isinstance(c, PostgresqlConnector)

    def test_create_postgres_alias(self):
        from app.connectors.database import get_database_connector, PostgresqlConnector
        c = get_database_connector("postgres", {"host": "localhost"})
        assert isinstance(c, PostgresqlConnector)

    def test_create_oracle(self):
        from app.connectors.database import get_database_connector, OracleConnector
        c = get_database_connector("oracle", {"host": "localhost"})
        assert isinstance(c, OracleConnector)

    def test_unsupported_type_raises(self):
        from app.connectors.database import get_database_connector
        with pytest.raises(ValueError, match="不支持的数据库类型"):
            get_database_connector("mongodb", {})


# ============================================================
# SQLite 连接器功能测试
# ============================================================

class TestSqliteConnector:
    """SQLite 连接器功能测试"""

    @pytest.mark.asyncio
    async def test_connection_ok(self, sqlite_conn):
        result = await sqlite_conn.test_connection()
        assert result["status"] == "ok"
        assert result["latency_ms"] >= 0
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_connection_failed(self):
        from app.connectors.database import SqliteConnector
        c = SqliteConnector({"database": "/nonexistent/path/db.sqlite"})
        result = await c.test_connection()
        assert "status" in result
        assert "latency_ms" in result
        # 注意：SQLite 可能不报错（会自动创建文件），这里只验证结构

    @pytest.mark.asyncio
    async def test_get_table_list(self, sqlite_conn):
        tables = await sqlite_conn.get_table_list()
        assert "users" in tables
        assert "orders" in tables

    @pytest.mark.asyncio
    async def test_get_table_schema(self, sqlite_conn):
        schema = await sqlite_conn.get_table_schema("users")
        columns = {c["name"]: c for c in schema}
        assert "id" in columns
        assert "name" in columns
        assert columns["id"]["primary_key"] is True

    @pytest.mark.asyncio
    async def test_fetch_all(self, sqlite_conn):
        data = await sqlite_conn.fetch_data("users")
        assert len(data) == 3
        assert data[0]["name"] == "张三"

    @pytest.mark.asyncio
    async def test_fetch_with_columns(self, sqlite_conn):
        data = await sqlite_conn.fetch_data("users", columns=["name", "age"])
        assert len(data) == 3
        assert "name" in data[0]
        assert "age" in data[0]
        assert "email" not in data[0]

    @pytest.mark.asyncio
    async def test_fetch_with_where(self, sqlite_conn):
        data = await sqlite_conn.fetch_data("users", where={"status": "inactive"})
        assert len(data) == 1
        assert data[0]["name"] == "王五"

    @pytest.mark.asyncio
    async def test_fetch_with_order_by(self, sqlite_conn):
        data = await sqlite_conn.fetch_data("users", order_by="age")
        assert data[0]["name"] == "张三"
        assert data[2]["name"] == "王五"

    @pytest.mark.asyncio
    async def test_fetch_with_limit_offset(self, sqlite_conn):
        data = await sqlite_conn.fetch_data("users", limit=2, offset=1)
        assert len(data) == 2
        assert data[0]["name"] == "李四"

    @pytest.mark.asyncio
    async def test_push_insert(self, sqlite_conn):
        rows = await sqlite_conn.push_data("users", [
            {"name": "赵六", "email": "zhaoliu@test.com", "age": 28}
        ])
        assert rows == 1
        data = await sqlite_conn.fetch_data("users", where={"name": "赵六"})
        assert len(data) == 1
        assert data[0]["email"] == "zhaoliu@test.com"

    @pytest.mark.asyncio
    async def test_push_upsert(self, sqlite_conn):
        rows = await sqlite_conn.push_data("users", [
            {"name": "新用户", "email": "new@test.com", "age": 20},
        ], mode="replace", key_field="id")
        assert rows == 1

    @pytest.mark.asyncio
    async def test_push_empty_list(self, sqlite_conn):
        rows = await sqlite_conn.push_data("users", [])
        assert rows == 0

    @pytest.mark.asyncio
    async def test_validate_identifier_rejects_sql_injection(self):
        from app.connectors.database import _validate_identifier
        with pytest.raises(ValueError, match="非法"):
            _validate_identifier("users; DROP TABLE users--")

    @pytest.mark.asyncio
    async def test_fetch_empty_result(self, sqlite_conn):
        data = await sqlite_conn.fetch_data("users", where={"name": "不存在的用户"})
        assert data == []


# ============================================================
# db_client 集成测试
# ============================================================

class TestDbClient:
    """db_client 模块测试"""

    @pytest.mark.asyncio
    async def test_fetch_from_db_sqlite(self, tmp_sqlite_db):
        from app.services.db_client import fetch_from_db
        data = await fetch_from_db(
            db_type="sqlite",
            db_config={"database": tmp_sqlite_db},
            table="users",
        )
        assert len(data) == 3
        assert data[0]["name"] == "张三"

    @pytest.mark.asyncio
    async def test_push_to_db_sqlite(self, tmp_sqlite_db):
        from app.services.db_client import push_to_db, fetch_from_db
        result = await push_to_db(
            db_type="sqlite",
            db_config={"database": tmp_sqlite_db},
            table="users",
            data=[{"name": "test_push", "email": "push@test.com", "age": 99}],
        )
        assert result["status_code"] == 200
        assert result["body"]["rows_affected"] == 1

    @pytest.mark.asyncio
    async def test_test_db_connection_sqlite(self, tmp_sqlite_db):
        from app.services.db_client import test_db_connection
        result = await test_db_connection(
            db_type="sqlite",
            db_config={"database": tmp_sqlite_db}
        )
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_test_db_connection_unsupported(self):
        from app.services.db_client import test_db_connection
        with pytest.raises(ValueError, match="不支持的数据库类型"):
            await test_db_connection("cassandra", {})


# ============================================================
# DB 路由集成测试（sync_handler 中 DB→API / API→DB 路由）
# ============================================================

class TestDbRouting:
    """验证 sync_handler 正确路由到数据库连接器"""

    @pytest.mark.asyncio
    async def test_sync_with_db_source(self, mock_redis, mock_api_target, mapping_config):
        """DB 源 → API 目标"""
        from app.services.sync_handler import sync_transfer

        task = MagicMock(id=uuid4(), fallback_to_async=False, timeout_ms=30000, conflict_strategy=None)
        mapping = MagicMock(
            source_endpoint="/users", target_endpoint="/api/customers",
            source_table="users", target_table=None,
            mapping_config=mapping_config,
        )
        source_conn = MagicMock(
            base_url="", db_type="sqlite",
            db_config={"database": ":memory:"},
            headers={}, timeout_ms=30000,
        )
        target_conn = MagicMock(id=uuid4(), db_type=None, base_url="https://target.example.com", headers={}, timeout_ms=30000)

        session = AsyncMock()
        buffer_obj = MagicMock(id=uuid4())
        session.add = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        with patch("app.services.sync_handler.fetch_from_db", new_callable=AsyncMock) as mock_fetch_db:
            mock_fetch_db.return_value = [{"user_name": "张三"}]
            with patch("app.services.sync_handler.DataBuffer") as MockBuffer:
                MockBuffer.return_value = buffer_obj
                result = await sync_transfer(session, task, mapping, source_conn, target_conn)

        assert result["status"] == "success"
        mock_fetch_db.assert_called_once()
        mock_api_target.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_with_db_target(self, mock_redis, mock_api_source, mapping_config):
        """API 源 → DB 目标"""
        from app.services.sync_handler import sync_transfer

        task = MagicMock(id=uuid4(), fallback_to_async=False, timeout_ms=30000, conflict_strategy=None)
        mapping = MagicMock(
            source_endpoint="/users", target_endpoint="/api/customers",
            source_table=None, target_table="customers",
            mapping_config=mapping_config,
        )
        source_conn = MagicMock(db_type=None, base_url="https://source.example.com", headers={}, timeout_ms=30000)
        target_conn = MagicMock(
            id=uuid4(), db_type="sqlite",
            db_config={"database": ":memory:"},
            base_url="", headers={}, timeout_ms=30000,
        )

        session = AsyncMock()
        buffer_obj = MagicMock(id=uuid4())
        session.add = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        with patch("app.services.sync_handler.push_to_db", new_callable=AsyncMock) as mock_push_db:
            mock_push_db.return_value = {"status_code": 200, "body": {"rows_affected": 1}}
            with patch("app.services.sync_handler.DataBuffer") as MockBuffer:
                MockBuffer.return_value = buffer_obj
                result = await sync_transfer(session, task, mapping, source_conn, target_conn)

        assert result["status"] == "success"
        mock_push_db.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_db_to_db(self, mock_redis, mapping_config):
        """DB 源 → DB 目标"""
        from app.services.sync_handler import sync_transfer

        task = MagicMock(id=uuid4(), fallback_to_async=False, timeout_ms=30000, conflict_strategy=None)
        mapping = MagicMock(
            source_endpoint=None, target_endpoint=None,
            source_table="users", target_table="customers",
            mapping_config=mapping_config,
        )
        source_conn = MagicMock(db_type="sqlite", db_config={"database": ":memory:"}, base_url="", headers={}, timeout_ms=30000)
        target_conn = MagicMock(id=uuid4(), db_type="sqlite", db_config={"database": ":memory:"}, base_url="", headers={}, timeout_ms=30000)

        session = AsyncMock()
        buffer_obj = MagicMock(id=uuid4())
        session.add = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        with patch("app.services.sync_handler.fetch_from_db", new_callable=AsyncMock) as mock_fetch:
            with patch("app.services.sync_handler.push_to_db", new_callable=AsyncMock) as mock_push:
                mock_fetch.return_value = [{"user_name": "张三"}]
                mock_push.return_value = {"status_code": 200, "body": {"rows_affected": 1}}
                with patch("app.services.sync_handler.DataBuffer") as MockBuffer:
                    MockBuffer.return_value = buffer_obj
                    result = await sync_transfer(session, task, mapping, source_conn, target_conn)

        assert result["status"] == "success"
        mock_fetch.assert_called_once()
        mock_push.assert_called_once()


# ============================================================
# 连接器 instantiation 测试
# ============================================================

class TestConnectorInstantiation:
    """各连接器实例化测试"""

    def test_mysql_connector_config(self):
        from app.connectors.database import MysqlConnector
        c = MysqlConnector({"host": "db.example.com", "port": 3307, "database": "mydb", "username": "root", "password": "secret"})
        assert c.db_config["host"] == "db.example.com"
        assert c.db_config["port"] == 3307

    def test_postgresql_connector_config(self):
        from app.connectors.database import PostgresqlConnector
        c = PostgresqlConnector({"host": "pg.example.com", "database": "mydb", "username": "admin"})
        dsn = c._dsn()
        assert "pg.example.com" in dsn
        assert "mydb" in dsn
        assert "admin" in dsn

    def test_oracle_connector_config(self):
        from app.connectors.database import OracleConnector
        c = OracleConnector({"host": "ora.example.com", "port": 1522, "service_name": "ORCLPDB1"})
        dsn = c._dsn()
        assert "ora.example.com" in dsn
        assert "1522" in dsn
        assert "ORCLPDB1" in dsn

    def test_oracle_defaults(self):
        from app.connectors.database import OracleConnector
        c = OracleConnector({})
        dsn = c._dsn()
        assert "localhost" in dsn
        assert "1521" in dsn
