# -*- coding: utf-8 -*-
"""指标刷新测试（tasks 2.2 – 2.5）

覆盖：
- 2.2 apibridge_buffer_pending 反映 data_buffer 待处理行数
- 2.3 apibridge_queue_depth 真实读取；MQ 不可达时**移除序列**（不谎报 0）
- 2.4 active_tasks / active_connections / db_pool_* 反映真实数量
- 2.5 刷新失败降级：只记日志、保留上一次值、/metrics 仍返回 200

⚠ MQ 相关用例的重点：必须用**非 robust** 连接读取。
`connect_robust` 连接失败后无限重连，会把 `/metrics` 拖到永不返回
（曾因此挂死全量测试），故此处专门回归。
"""
import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from unittest.mock import patch

from app.config import settings
from app.metrics_collector import (
    active_connections, active_tasks, buffer_pending, db_pool_connections,
    db_pool_size, generate_latest, queue_depth, registry,
)
from app.models.sync import ApiConnection, Base, DataBuffer, SyncTask
from app.services.metrics_refresh import (
    refresh_active_connections,
    refresh_active_tasks,
    refresh_buffer_pending,
    refresh_db_pools,
    refresh_gauges,
    refresh_queue_depth,
)

UTC = timezone.utc
QUEUE_LABEL = settings.task_queue_name


@pytest.fixture(autouse=True)
def _reset_mq_cooldown():
    """每例重置 MQ 失败冷却与缓存连接，避免测试间互相影响"""
    import app.services.metrics_refresh as mr
    import app.rabbitmq as rabbit

    mr._mq_unavailable_until = 0.0
    rabbit._connection = None
    rabbit._channel = None
    yield
    mr._mq_unavailable_until = 0.0
    rabbit._connection = None
    rabbit._channel = None


@pytest.fixture
def db_maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/mr.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    asyncio.run(engine.dispose())


def rendered() -> str:
    return generate_latest(registry).decode("utf-8")


def has_series(text: str, name: str) -> bool:
    """是否导出了该指标名的样本行（带标签也能正确识别）"""
    return any(
        line.split("{", 1)[0].split(" ", 1)[0].strip() == name
        for line in text.splitlines() if line and not line.startswith("#")
    )


async def add_buffers(maker, status: str, n: int = 1) -> None:
    async with maker() as s:
        for _ in range(n):
            s.add(DataBuffer(task_id=uuid.uuid4(), source_data={},
                             status=status, created_at=datetime.now(UTC)))
        await s.commit()


# ---- MQ 替身 -------------------------------------------------

class _Decl:
    message_count = 42


class _Queue:
    declaration_result = _Decl()


class _Channel:
    async def declare_queue(self, name, passive=False):
        assert passive is True, "必须是 passive declare（不消费、不改动队列）"
        return _Queue()


class _Conn:
    async def channel(self):
        return _Channel()

    async def close(self):
        pass


async def _connect_ok(url=None):
    return _Conn()


async def _connect_boom(url=None):
    raise ConnectionRefusedError("no MQ here")


async def _connect_hang(url=None):
    await asyncio.sleep(30)


# ============================================================
# 2.2 缓冲待处理量
# ============================================================

class TestBufferPending:

    async def test_counts_pending_and_processing(self, db_maker):
        await add_buffers(db_maker, "pending", 3)
        await add_buffers(db_maker, "processing", 2)
        await add_buffers(db_maker, "completed", 7)   # 不计入
        await add_buffers(db_maker, "failed", 4)      # 不计入
        async with db_maker() as s:
            assert await refresh_buffer_pending(s) == 5

    async def test_zero_when_empty(self, db_maker):
        async with db_maker() as s:
            assert await refresh_buffer_pending(s) == 0

    def test_series_always_present(self):
        """即使为 0 也必须导出（跨环境可靠的积压指标）"""
        buffer_pending.set(0)
        assert has_series(rendered(), "apibridge_buffer_pending")


# ============================================================
# 2.3 MQ 队列深度（不可达时不导出）
# ============================================================

class TestQueueDepth:

    async def test_returns_count_when_reachable(self):
        with patch("aio_pika.connect", _connect_ok):
            assert await refresh_queue_depth() == 42

    async def test_returns_none_when_unreachable(self):
        with patch("aio_pika.connect", _connect_boom):
            assert await refresh_queue_depth() is None

    async def test_returns_none_on_timeout(self):
        with patch("aio_pika.connect", _connect_hang), \
             patch("app.services.metrics_refresh._MQ_TIMEOUT_SECONDS", 0.05):
            assert await refresh_queue_depth() is None

    async def test_uses_non_robust_connect(self):
        """回归：必须调用 `aio_pika.connect`（非 robust）

        `connect_robust` 失败后无限重连，wait_for 无法终止，
        会把 /metrics 拖到永不返回（曾挂死全量测试）。
        这里用**行为断言**而非源码字符串匹配（后者会被 docstring 误伤）。
        """
        calls = {"connect": 0, "robust": 0}

        async def _counting_connect(url=None, **kwargs):
            calls["connect"] += 1
            return _Conn()

        async def _forbidden_robust(url=None, **kwargs):
            calls["robust"] += 1
            raise AssertionError("不得使用 connect_robust（会无限重连）")

        with patch("aio_pika.connect", _counting_connect), \
             patch("aio_pika.connect_robust", _forbidden_robust):
            assert await refresh_queue_depth() == 42

        assert calls["connect"] == 1, "应调用 aio_pika.connect"
        assert calls["robust"] == 0, "不得调用 connect_robust"

    async def test_cooldown_prevents_repeat_attempts(self):
        """失败后进入冷却：冷却期内不得重复尝试连接（否则每次抓取都等超时）"""
        calls: list[int] = []

        async def _counting_boom(url=None):
            calls.append(1)
            raise ConnectionRefusedError("no MQ")

        with patch("aio_pika.connect", _counting_boom):
            assert await refresh_queue_depth() is None
            assert await refresh_queue_depth() is None
            assert await refresh_queue_depth() is None
        assert len(calls) == 1, f"冷却期内应只尝试一次，实际 {len(calls)} 次"

    async def test_cooldown_expires_and_retries(self):
        """冷却期过后应重新尝试（恢复能力，不能永久放弃）"""
        import app.services.metrics_refresh as mr

        calls: list[int] = []

        async def _counting_boom(url=None):
            calls.append(1)
            raise ConnectionRefusedError("no MQ")

        with patch("aio_pika.connect", _counting_boom):
            assert await refresh_queue_depth() is None
            mr._mq_unavailable_until = 0.0        # 模拟冷却到期
            assert await refresh_queue_depth() is None
        assert len(calls) == 2

    def test_series_removed_when_unreachable(self):
        """MQ 不可达时必须**移除序列**——宁可 no data，也不谎报 0"""
        queue_depth.labels(queue_name=QUEUE_LABEL).set(7)
        assert has_series(rendered(), "apibridge_queue_depth")
        queue_depth.remove(QUEUE_LABEL)   # 带标签才支持 remove（无标签指标会抛错）
        assert not has_series(rendered(), "apibridge_queue_depth")

    async def test_remove_absent_label_does_not_raise(self, db_maker):
        """回归：队列序列**从未出现过**时刷新不得抛异常

        prometheus_client 的 remove() 用 del 而非 pop，
        对不存在的标签会抛 KeyError（无 MQ 环境首次刷新即命中）。
        """
        queue_depth._metrics.pop((QUEUE_LABEL,), None)  # 确保从未 set 过
        with patch("app.database.async_session", db_maker), \
             patch("aio_pika.connect", _connect_boom):
            result = await refresh_gauges()
        assert result["queue_depth"] is None
        assert not has_series(rendered(), "apibridge_queue_depth")


# ============================================================
# 2.4 活跃任务 / 连接 / 连接池
# ============================================================

class TestActiveCounters:

    async def test_active_tasks_counts_only_active(self, db_maker):
        async with db_maker() as s:
            mapping = uuid.uuid4()
            for status in ("active", "active", "paused", "failed"):
                s.add(SyncTask(id=uuid.uuid4(), mapping_id=mapping,
                               task_type="manual", status=status))
            await s.commit()
        async with db_maker() as s:
            assert await refresh_active_tasks(s) == 2

    async def test_active_connections_counts_only_active(self, db_maker):
        async with db_maker() as s:
            for status in ("active", "active", "active", "disabled"):
                s.add(ApiConnection(id=uuid.uuid4(), name="c",
                                    connection_type="source", protocol="rest",
                                    status=status))
            await s.commit()
        async with db_maker() as s:
            assert await refresh_active_connections(s) == 3

    def test_pool_stats_per_driver(self):
        """三种驱动的连接池接口不同，取值应各自正确"""
        from app.services.metrics_refresh import _pool_stats

        class OraclePool:
            opened, max = 3, 10

        class MySQLPool:
            size, maxsize = 5, 20

        class PGPool:
            def get_size(self):
                return 2

            def get_max_size(self):
                return 8

        assert _pool_stats("oracle", OraclePool()) == (3, 10)
        assert _pool_stats("mysql", MySQLPool()) == (5, 20)
        assert _pool_stats("postgresql", PGPool()) == (2, 8)

    def test_pool_stats_tolerates_broken_driver(self):
        """驱动接口取值失败 → (0, 0)，不抛异常"""
        from app.services.metrics_refresh import _pool_stats

        class Broken:
            pass

        assert _pool_stats("oracle", Broken()) == (0, 0)

    def test_refresh_db_pools_sums_registry(self):
        from app.connectors import database as dbmod

        class PGPool:
            def get_size(self):
                return 2

            def get_max_size(self):
                return 8

        fake = {("postgresql", "fp1"): PGPool(), ("postgresql", "fp2"): PGPool()}
        with patch.object(dbmod, "_POOL_REGISTRY", fake):
            assert refresh_db_pools() == (4, 16)

    def test_refresh_db_pools_empty(self):
        from app.connectors import database as dbmod

        with patch.object(dbmod, "_POOL_REGISTRY", {}):
            assert refresh_db_pools() == (0, 0)


# ============================================================
# 2.5 刷新失败降级
# ============================================================

class TestRefreshDegradation:

    async def test_db_failure_keeps_previous_value(self, db_maker):
        """平台库不可用 → 保留上一次值，不抛异常"""
        buffer_pending.set(99)

        def _boom_session():
            raise RuntimeError("db down")

        with patch("app.database.async_session", _boom_session), \
             patch("aio_pika.connect", _connect_boom):
            result = await refresh_gauges()

        assert "db_error" in result
        assert buffer_pending._value.get() == 99, "刷新失败不得把 Gauge 清零/改变"
        assert has_series(rendered(), "apibridge_buffer_pending")

    async def test_mq_failure_removes_series_but_keeps_others(self, db_maker):
        with patch("app.database.async_session", db_maker), \
             patch("aio_pika.connect", _connect_boom):
            result = await refresh_gauges()

        assert result["queue_depth"] is None
        assert not has_series(rendered(), "apibridge_queue_depth")
        # 其他指标不受影响
        assert has_series(rendered(), "apibridge_buffer_pending")
        assert has_series(rendered(), "apibridge_active_tasks")

    async def test_success_path_sets_all_gauges(self, db_maker):
        await add_buffers(db_maker, "pending", 2)

        with patch("app.database.async_session", db_maker), \
             patch("aio_pika.connect", _connect_boom):
            result = await refresh_gauges()

        assert result["buffer_pending"] == 2
        assert buffer_pending._value.get() == 2
        assert active_tasks._value.get() == 0
        assert active_connections._value.get() == 0
        assert db_pool_connections._value.get() == 0
        assert db_pool_size._value.get() == 0

    def test_metrics_endpoint_returns_200_when_refresh_fails(self, db_maker):
        """/metrics 端点：刷新失败仍必须返回 200（绝不让抓取整体失败）"""
        import time
        from fastapi.testclient import TestClient
        from app.main import app

        def _boom_session():
            raise RuntimeError("db down")

        client = TestClient(app)
        # 必须同时屏蔽 MQ：否则会真去连 RabbitMQ 而挂住整个测试
        with patch("app.database.async_session", _boom_session), \
             patch("aio_pika.connect", _connect_boom):
            t0 = time.perf_counter()
            resp = client.get("/metrics")
            elapsed = time.perf_counter() - t0
        assert resp.status_code == 200
        assert "apibridge_buffer_pending" in resp.text
        assert elapsed < 5.0, f"/metrics 不应被 MQ 拖慢，实际 {elapsed:.2f}s"
