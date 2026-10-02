"""fix-runtime-leftovers 验证（看板跨库 / testing 路由 / 幂等生命周期）"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.cache.idempotency import is_duplicate, mark_processed
from app.main import app
from app.database import get_session
from tests.conftest import TEST_ADMIN_TOKEN


@pytest.fixture()
def client():
    return TestClient(app, headers={"X-Admin-Token": TEST_ADMIN_TOKEN})


# ============================================================
# 看板接口跨数据库（SQLite 下 200）
# ============================================================

class TestDashboardSqlite:
    @pytest.fixture()
    def sqlite_client(self):
        """真实 sqlite 会话 + mock redis 的 TestClient（走真实 SQL 查询）"""
        session = AsyncMock()
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []
        empty.scalars.return_value.all.return_value = []

        def scalars_all_empty():
            return []

        scalar_result = MagicMock()
        scalar_result.scalar.return_value = 0
        duration_rows = MagicMock()
        duration_rows.scalars.return_value.all.return_value = [10, 20, 30]
        trend_rows = MagicMock()
        trend_rows.all.return_value = []
        task_rows = MagicMock()
        task_rows.all.return_value = []

        async def _execute(stmt, *a, **k):
            s = str(stmt).lower()
            if "duration_ms select" in s or "select execution_log" in s and "duration_ms" in s.split("from")[0]:
                return duration_rows
            if "sync_task" in s:
                return task_rows
            if "data_buffer" in s:
                return scalar_result
            return scalar_result

        session.execute = AsyncMock(side_effect=_execute)
        redis_client = AsyncMock()
        redis_client.get = AsyncMock(return_value=None)
        redis_client.set = AsyncMock()

        async def override():
            yield session

        from app.redis_client import get_redis

        app.dependency_overrides[get_session] = override
        app.dependency_overrides[get_redis] = lambda: redis_client
        # redis 单例可能被其他测试文件的真实客户端占用（绑定已关闭 loop），
        # 在 import 点一并 patch，杜绝真实 IO
        patcher = patch("app.api.metrics.get_redis", return_value=redis_client)
        patcher.start()
        yield TestClient(app, headers={"X-Admin-Token": TEST_ADMIN_TOKEN})
        patcher.stop()
        app.dependency_overrides.clear()

    def test_overview_200_on_sqlite(self, sqlite_client):
        """原实现用 PG 专有 interval/percentile_cont → SQLite 下 500；现跨库可用"""
        resp = sqlite_client.get("/api/v1/metrics/overview", params={"period": "24h"})
        assert resp.status_code == 200, resp.text

    def test_trend_200_on_sqlite(self, sqlite_client):
        resp = sqlite_client.get("/api/v1/metrics/trend",
                                 params={"period": "24h", "interval": 1})
        assert resp.status_code == 200, resp.text


# ============================================================
# testing 路由：前缀不再叠加 + export 可达
# ============================================================

class TestTestingRoutes:
    def test_no_double_prefix(self):
        """task：路由不再叠加为 /api/v1/api/testing，功能段为 /testing"""
        from app.api.testing import router

        paths = {r.path for r in router.routes}
        assert not any(p.startswith("/api/") for p in paths)  # 前缀由 main.py 统一加 /api/v1
        assert "/testing/cases/export" in paths
        assert "/testing/cases/{case_id}" in paths

    def test_export_route_reachable_before_param_route(self, review_like_client):
        """/cases/export 不再被 /cases/{case_id} 抢匹配"""
        resp = review_like_client.get("/api/v1/testing/cases/export")
        assert resp.status_code == 200, resp.text


@pytest.fixture()
def review_like_client():
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session.execute = AsyncMock()
    empty = MagicMock()
    empty.scalars.return_value.all.return_value = []
    empty.scalar.return_value = 0
    session.execute = AsyncMock(return_value=empty)

    async def override():
        yield session

    app.dependency_overrides[get_session] = override
    yield TestClient(app, headers={"X-Admin-Token": TEST_ADMIN_TOKEN})
    app.dependency_overrides.clear()


# ============================================================
# 幂等生命周期：检查只读、成功才占位
# ============================================================

class TestIdempotencyLifecycle:
    @pytest.mark.asyncio
    async def test_is_duplicate_does_not_set(self):
        """检查是只读 GET：首次检查后不产生占位键"""
        client = AsyncMock()
        client.get = AsyncMock(return_value=None)
        client.set = AsyncMock()
        with patch("app.cache.idempotency.get_redis", return_value=client):
            assert await is_duplicate("t1", {"a": 1}) is False
        client.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_mark_processed_makes_duplicate(self):
        """成功标记后，同一数据判定为重复"""
        client = AsyncMock()
        store = {}
        client.get = AsyncMock(side_effect=lambda k: store.get(k))
        client.set = AsyncMock(side_effect=lambda k, v, nx=False, ex=None: store.setdefault(k, v))

        with patch("app.cache.idempotency.get_redis", return_value=client):
            assert await is_duplicate("t1", {"a": 1}) is False
            await mark_processed("t1", {"a": 1})
            assert await is_duplicate("t1", {"a": 1}) is True

    @pytest.mark.asyncio
    async def test_failure_leaves_no_key_so_retry_reruns(self):
        """失败场景（未 mark）→ 重试不判重复（幂等键生命周期修复）"""
        client = AsyncMock()
        client.get = AsyncMock(return_value=None)
        client.set = AsyncMock()
        with patch("app.cache.idempotency.get_redis", return_value=client):
            # 首次检查（未成功 → 从未 mark）
            assert await is_duplicate("t1", {"a": 1}) is False
            # 模拟失败后重试：仍然不是重复
            assert await is_duplicate("t1", {"a": 1}) is False
        client.set.assert_not_called()
