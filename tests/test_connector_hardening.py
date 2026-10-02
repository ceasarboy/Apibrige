"""连接器硬化单测

覆盖：Oracle ROWNUM 分页 SQL 生成、Oracle client 模式守卫、
全局池注册表、MySQL DictCursor NameError 回归、SQLite 冲突上抛与真实行数。
不依赖任何真实数据库驱动（缺驱动处以模块替身注入）。
"""
import sqlite3
import sys
import types

import pytest

from app.connectors import database as db_mod
from app.connectors.database import (
    SqliteConnector,
    _config_fingerprint,
    _get_registered_pool,
    build_oracle_fetch_sql,
    close_all_pools,
    get_database_connector,
)


# ============================================================
# Oracle ROWNUM 分页 SQL（11g 兼容）
# ============================================================

class TestOracleFetchSql:
    def test_no_limit_plain_select(self):
        sql, params = build_oracle_fetch_sql("users")
        assert sql == 'SELECT * FROM "USERS"'
        assert params == {}

    def test_limit_only_rownum(self):
        sql, params = build_oracle_fetch_sql("users", limit=3)
        assert "ROWNUM <= :lim" in sql
        assert "FETCH NEXT" not in sql
        assert params == {"lim": 3}

    def test_offset_limit_double_rownum(self):
        sql, params = build_oracle_fetch_sql("users", limit=2, offset=2)
        assert "ROWNUM <= :hi" in sql
        assert "rn > :lo" in sql
        assert "FETCH NEXT" not in sql
        assert params == {"hi": 4, "lo": 2}

    def test_where_and_order_preserved(self):
        sql, params = build_oracle_fetch_sql(
            "users", where={"name": "张三"}, order_by="id", limit=10
        )
        assert '"NAME" = :w_0' in sql
        assert 'ORDER BY "ID"' in sql
        assert params["w_0"] == "张三"
        assert params["lim"] == 10

    def test_columns_uppercased(self):
        sql, _ = build_oracle_fetch_sql("t", columns=["id", "name"])
        assert sql.startswith('SELECT ID, NAME FROM "T"')

    def test_injection_rejected(self):
        with pytest.raises(ValueError):
            build_oracle_fetch_sql("t; DROP TABLE x")
        with pytest.raises(ValueError):
            build_oracle_fetch_sql("t", where={"a = 1 OR": 2})


# ============================================================
# Oracle 客户端模式守卫
# ============================================================

def _fake_oracledb(init_impl):
    fake = types.ModuleType("oracledb")
    calls = []

    def _init(lib_dir=None):
        calls.append(lib_dir)
        if init_impl is not None:
            init_impl(lib_dir)

    fake.init_oracle_client = _init
    return fake, calls


@pytest.fixture()
def reset_oracle_flag(monkeypatch):
    monkeypatch.setattr(db_mod, "_ORACLE_THICK_INITIALIZED", False)
    yield
    # 收尾还原，避免影响其他用例
    monkeypatch.setattr(db_mod, "_ORACLE_THICK_INITIALIZED", False)


class TestOracleClientGuard:
    def test_thin_default_never_init(self, monkeypatch, reset_oracle_flag):
        from app.config import settings

        fake, calls = _fake_oracledb(None)
        monkeypatch.setitem(sys.modules, "oracledb", fake)
        monkeypatch.setattr(settings, "oracle_client_mode", "thin")

        db_mod._ensure_oracle_client()
        assert calls == []

    def test_thick_init_called_once(self, monkeypatch, reset_oracle_flag):
        from app.config import settings

        fake, calls = _fake_oracledb(None)
        monkeypatch.setitem(sys.modules, "oracledb", fake)
        monkeypatch.setattr(settings, "oracle_client_mode", "thick")
        monkeypatch.setattr(settings, "oracle_client_lib_dir", "/fake/ic")

        db_mod._ensure_oracle_client()
        db_mod._ensure_oracle_client()  # 第二次必须命中标志，不重复加载
        assert calls == ["/fake/ic"]

    def test_thick_init_failure_raises_diagnosable(self, monkeypatch, reset_oracle_flag):
        from app.config import settings

        def _boom(lib_dir=None):
            raise OSError("lib not found")

        fake, _ = _fake_oracledb(_boom)
        monkeypatch.setitem(sys.modules, "oracledb", fake)
        monkeypatch.setattr(settings, "oracle_client_mode", "thick")
        monkeypatch.setattr(settings, "oracle_client_lib_dir", "/bad/path")

        with pytest.raises(RuntimeError, match="oracle_client_init_failed"):
            db_mod._ensure_oracle_client()
        assert db_mod._ORACLE_THICK_INITIALIZED is False  # 失败后允许重试


# ============================================================
# 全局池注册表
# ============================================================

class FakePool:
    """同步 close 的池替身（oracledb 同步池语义）"""

    def __init__(self, tag="p"):
        self.tag = tag
        self.closed = False

    def close(self):
        self.closed = True


class TestPoolRegistry:
    @pytest.mark.asyncio
    async def test_same_config_reuses_pool(self):
        cfg = {"host": "h1", "password": "p"}
        p1 = await _get_registered_pool("oracle", cfg, FakePool)
        p2 = await _get_registered_pool("oracle", cfg, FakePool)
        assert p1 is p2
        await close_all_pools()
        assert p1.closed is True

    @pytest.mark.asyncio
    async def test_different_config_separate_pools(self):
        p1 = await _get_registered_pool("oracle", {"host": "a"}, FakePool)
        p2 = await _get_registered_pool("oracle", {"host": "b"}, FakePool)
        assert p1 is not p2
        await close_all_pools()
        assert _len_registry() == 0

    @pytest.mark.asyncio
    async def test_password_change_new_pool(self):
        p1 = await _get_registered_pool("mysql", {"host": "a", "password": "x"}, FakePool)
        p2 = await _get_registered_pool("mysql", {"host": "a", "password": "y"}, FakePool)
        assert p1 is not p2
        await close_all_pools()

    @pytest.mark.asyncio
    async def test_fingerprint_order_insensitive(self):
        assert _config_fingerprint({"a": 1, "b": 2}) == _config_fingerprint({"b": 2, "a": 1})
        assert _config_fingerprint({"a": 1}) != _config_fingerprint({"a": 2})

    @pytest.mark.asyncio
    async def test_async_factory_supported(self):
        async def make():
            return FakePool("async")

        p = await _get_registered_pool("postgresql", {"host": "pg"}, make)
        assert p.tag == "async"
        await close_all_pools()
        assert p.closed is True


def _len_registry() -> int:
    return len(db_mod._POOL_REGISTRY)


# ============================================================
# MySQL DictCursor NameError 回归
# ============================================================

class _FakeCursor:
    def __init__(self, rows, description=None):
        self._rows = rows
        self.description = description

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.last_sql = sql

    async def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, cls=None):
        self._cursor.cursor_cls = cls
        return self._cursor


class _FakePoolCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    """池替身：acquire() 返回连接的异步上下文"""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakePoolCtx(self._conn)


def _mysql_connector_with_pool(monkeypatch, fake_conn):
    conn = get_database_connector("mysql", {"host": "h", "database": "d"})
    pool = _FakePool(fake_conn)

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(conn, "_get_pool", fake_get_pool)
    return conn


class TestMysqlFetchData:
    @pytest.mark.asyncio
    async def test_dictcursor_path_no_nameerror(self, monkeypatch):
        """原实现 fetch_data 引用未导入的 aiomysql → NameError；修复后走 DictCursor 正常返回"""
        fake_aiomysql = types.ModuleType("aiomysql")
        fake_aiomysql.DictCursor = type("DictCursor", (), {})
        monkeypatch.setitem(sys.modules, "aiomysql", fake_aiomysql)

        rows = [{"id": 1, "name": "张三"}, {"id": 2, "name": "李四"}]
        cursor = _FakeCursor(rows)
        cursor.cursor_cls = None
        conn = _mysql_connector_with_pool(monkeypatch, _FakeConn(cursor))

        got = await conn.fetch_data("t")
        assert got == rows
        assert cursor.cursor_cls is fake_aiomysql.DictCursor

    @pytest.mark.asyncio
    async def test_fallback_assembles_dicts_without_dictcursor(self, monkeypatch):
        """字典游标不可用时回退为显式列名组装字典"""
        fake_aiomysql = types.ModuleType("aiomysql")  # 无 DictCursor 属性
        monkeypatch.setitem(sys.modules, "aiomysql", fake_aiomysql)

        rows = [("a", 1), ("b", 2)]
        description = (("name",), ("id",))
        cursor = _FakeCursor(rows, description=description)
        cursor.cursor_cls = "sentinel"
        conn = _mysql_connector_with_pool(monkeypatch, _FakeConn(cursor))

        got = await conn.fetch_data("t")
        assert got == [{"name": "a", "id": 1}, {"name": "b", "id": 2}]
        assert cursor.cursor_cls is None


# ============================================================
# SQLite：真实行数与冲突上抛
# ============================================================

@pytest.fixture()
def sqlite_db(tmp_path):
    db = tmp_path / "hard.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO items VALUES (1, ' existing')")
    conn.commit()
    conn.close()
    return str(db)


class TestSqlitePush:
    @pytest.mark.asyncio
    async def test_insert_returns_real_count(self, sqlite_db):
        c = SqliteConnector({"database": sqlite_db})
        n = await c.push_data("items", [{"id": i, "name": f"n{i}"} for i in range(2, 7)])
        assert n == 5

    @pytest.mark.asyncio
    async def test_conflict_raises_not_silently_swallowed(self, sqlite_db):
        c = SqliteConnector({"database": sqlite_db})
        with pytest.raises(sqlite3.IntegrityError):
            await c.push_data("items", [{"id": 1, "name": "dup"}])

    @pytest.mark.asyncio
    async def test_upsert_replace_semantics(self, sqlite_db):
        c = SqliteConnector({"database": sqlite_db})
        n = await c.push_data(
            "items",
            [{"id": 1, "name": "updated"}, {"id": 2, "name": "new"}],
            key_field="id",
            mode="upsert",
        )
        assert n == 2
        got = await c.fetch_data("items", where={"id": 1})
        assert got == [{"id": 1, "name": "updated"}]
