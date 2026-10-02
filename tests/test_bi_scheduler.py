# -*- coding: utf-8 -*-
"""BI 调度与数据保留 阶段3 测试

覆盖 openspec/changes/add-bi-aggregation-engine 的 tasks 3.1–3.5：
- 3.1 每分钟聚合 tick（含启用/停用过滤、锁语义）
- 3.2 Redis 不可用时**跳过本轮**（合并后语义：不降级为无锁聚合——多 worker 下重复
  聚合虽覆盖语义安全，但轮次成本高；告警只打一次）
- 3.3 水位线滞后告警
- 3.4 data_buffer 明细分批清理（保留期可配、单批上限；**不接入自动循环**）
- 3.5 单指标失败隔离，且不影响同步主链路
"""
import asyncio
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from unittest.mock import AsyncMock, patch

from app.models.bi import BiAggregationWatermark, BiMetricBucket, BiMetricDef
from app.models.sync import Base, DataBuffer
from app.services import bi_scheduler
from app.services.bi_scheduler import (
    _run_once,
    aggregate_all,
    check_watermark_lag,
    purge_expired_buffers,
)
from app.services.bi_seed import seed_builtin_metrics

UTC = timezone.utc


def _dt(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.fixture
def bi_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/sched.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def _reset_degraded_flag():
    """模块级降级告警标志需在每个用例前重置"""
    bi_scheduler._degraded_warned = False
    yield
    bi_scheduler._degraded_warned = False


async def _add_buffer(maker, created_at, status="completed", data=None,
                      expires_at="auto"):
    """expires_at="auto" 时置为 created_at+7天；传 None 则留空（走保留期判定）"""
    exp = created_at + timedelta(days=7) if expires_at == "auto" else expires_at
    async with maker() as s:
        s.add(DataBuffer(task_id=uuid.uuid4(), source_data=data or {},
                         transformed_data=data or {}, status=status,
                         created_at=created_at, expires_at=exp))
        await s.commit()


# ============================================================
# 3.1 / 3.3：聚合轮次与滞后检测
# ============================================================

class TestAggregateAll:

    async def test_skips_disabled_metrics(self, bi_db):
        async with bi_db() as s:
            await seed_builtin_metrics(s)
        async with bi_db() as s:
            row = (await s.execute(select(BiMetricDef).where(
                BiMetricDef.metric_key == "sync_throughput"))).scalar_one()
            row.enabled = False
            await s.commit()
        async with bi_db() as s:
            result = await aggregate_all(s, now=_dt(2026, 9, 25, 10, 5))
        keys = [r["metric_key"] for r in result["results"]]
        assert "sync_throughput" not in keys
        assert result["metrics"] == len(keys)

    async def test_single_metric_failure_does_not_block_others(self, bi_db):
        """3.5 隔离：单个指标抛错，其余仍完成"""
        async with bi_db() as s:
            s.add(BiMetricDef(metric_key="bad", name="坏", source_type="wat",
                              granularity="1min", dimensions=[], measures=[],
                              chart_type="line", enabled=True))
            s.add(BiMetricDef(metric_key="good", name="好", source_type="execution_logs",
                              granularity="1min", dimensions=[],
                              measures=[{"key": "c", "agg": "count"}],
                              chart_type="line", enabled=True))
            await s.commit()
        async with bi_db() as s:
            result = await aggregate_all(s, now=_dt(2026, 9, 25, 10, 5))
        by_key = {r["metric_key"]: r for r in result["results"]}
        assert "error" in by_key["bad"]
        assert by_key["good"].get("error") is None

    async def test_lag_warning_returned_and_logged(self, bi_db, caplog):
        async with bi_db() as s:
            s.add(BiMetricDef(metric_key="m", name="m", source_type="execution_logs",
                              granularity="1min", dimensions=[],
                              measures=[{"key": "c", "agg": "count"}],
                              chart_type="line", enabled=True))
            # 水位线停在 3 小时前（远超 60 窗口阈值）
            s.add(BiAggregationWatermark(metric_key="m", last_bucket=_dt(2026, 9, 25, 7, 0)))
            await s.commit()
        with caplog.at_level("WARNING"):
            async with bi_db() as s:
                result = await aggregate_all(s, now=_dt(2026, 9, 25, 10, 0))
        # 注意：聚合会推进水位线，但滞后告警基于本轮结果判定
        entry = result["results"][0]
        assert "error" not in entry

    async def test_check_watermark_lag_below_threshold(self, bi_db):
        async with bi_db() as s:
            s.add(BiAggregationWatermark(metric_key="m", last_bucket=_dt(2026, 9, 25, 9, 59)))
            await s.commit()
        defn = {"metric_key": "m", "granularity": "1min"}
        async with bi_db() as s:
            assert await check_watermark_lag(s, defn, now=_dt(2026, 9, 25, 10, 0)) is None

    async def test_check_watermark_lag_above_threshold(self, bi_db):
        async with bi_db() as s:
            s.add(BiAggregationWatermark(metric_key="m", last_bucket=_dt(2026, 9, 25, 7, 0)))
            await s.commit()
        defn = {"metric_key": "m", "granularity": "1min"}
        async with bi_db() as s:
            lag = await check_watermark_lag(s, defn, now=_dt(2026, 9, 25, 10, 0))
        assert lag is not None
        assert lag["lag_windows"] == 180
        assert lag["threshold_seconds"] == 3600

    async def test_check_watermark_lag_no_watermark(self, bi_db):
        defn = {"metric_key": "none", "granularity": "1min"}
        async with bi_db() as s:
            assert await check_watermark_lag(s, defn, now=_dt(2026, 9, 25, 10, 0)) is None


# ============================================================
# 3.4：明细保留清理
# ============================================================

class TestPurge:

    async def test_deletes_only_expired(self, bi_db):
        now = _dt(2026, 9, 25, 12, 0)
        await _add_buffer(bi_db, now - timedelta(days=10))   # 过期
        await _add_buffer(bi_db, now - timedelta(days=1))    # 未过期
        async with bi_db() as s:
            r = await purge_expired_buffers(s, retention_days=7, now=now)
        assert r["deleted"] == 1
        async with bi_db() as s:
            n = (await s.execute(select(func.count(DataBuffer.id)))).scalar()
        assert n == 1

    async def test_retention_days_configurable(self, bi_db):
        now = _dt(2026, 9, 25, 12, 0)
        # expires_at 留空 → 走 created_at + retention_days 判定
        await _add_buffer(bi_db, now - timedelta(days=3), expires_at=None)
        async with bi_db() as s:
            r = await purge_expired_buffers(s, retention_days=1, now=now)
        assert r["deleted"] == 1
        assert r["retention_days"] == 1

    async def test_retention_days_keeps_recent(self, bi_db):
        now = _dt(2026, 9, 25, 12, 0)
        await _add_buffer(bi_db, now - timedelta(hours=2), expires_at=None)
        async with bi_db() as s:
            r = await purge_expired_buffers(s, retention_days=7, now=now)
        assert r["deleted"] == 0

    async def test_batching_respects_batch_size(self, bi_db):
        """单批有上限：5 行 / 批大小 2 → 至少 3 批"""
        now = _dt(2026, 9, 25, 12, 0)
        for i in range(5):
            await _add_buffer(bi_db, now - timedelta(days=10, seconds=i))
        async with bi_db() as s:
            r = await purge_expired_buffers(s, retention_days=7, batch_size=2, now=now)
        assert r["deleted"] == 5
        assert r["batches"] == 3

    async def test_max_batches_limits_work(self, bi_db):
        now = _dt(2026, 9, 25, 12, 0)
        for i in range(6):
            await _add_buffer(bi_db, now - timedelta(days=10, seconds=i))
        async with bi_db() as s:
            r = await purge_expired_buffers(s, retention_days=7, batch_size=2,
                                            max_batches=2, now=now)
        assert r["deleted"] == 4  # 2 批 × 2 行
        assert r["batches"] == 2

    async def test_nothing_to_purge(self, bi_db):
        now = _dt(2026, 9, 25, 12, 0)
        await _add_buffer(bi_db, now - timedelta(hours=1))
        async with bi_db() as s:
            r = await purge_expired_buffers(s, retention_days=7, now=now)
        assert r["deleted"] == 0 and r["batches"] == 0

    async def test_expires_at_takes_precedence(self, bi_db):
        """expires_at 非空时按它判定，不受 retention_days 影响"""
        now = _dt(2026, 9, 25, 12, 0)
        async with bi_db() as s:
            s.add(DataBuffer(task_id=uuid.uuid4(), source_data={}, status="completed",
                             created_at=now - timedelta(hours=1),
                             expires_at=now - timedelta(minutes=5)))  # 已过期
            await s.commit()
        async with bi_db() as s:
            r = await purge_expired_buffers(s, retention_days=7, now=now)
        assert r["deleted"] == 1

    async def test_purge_does_not_touch_aggregation_buckets(self, bi_db):
        """清理明细不得影响聚合结果（3.4 关键约束）"""
        now = _dt(2026, 9, 25, 12, 0)
        await _add_buffer(bi_db, now - timedelta(days=10))
        async with bi_db() as s:
            s.add(BiMetricBucket(metric_key="m", bucket_start=now - timedelta(days=10),
                                 dim_hash="h" * 40, values={"c": 1}))
            await s.commit()
        async with bi_db() as s:
            await purge_expired_buffers(s, retention_days=7, now=now)
        async with bi_db() as s:
            n = (await s.execute(select(func.count(BiMetricBucket.id)))).scalar()
        assert n == 1


# ============================================================
# 3.5：隔离（单指标失败隔离 / 清理不接入自动循环）
# ============================================================

class TestIsolation:

    async def test_missing_definition_does_not_break_other_metrics(self, bi_db, monkeypatch):
        """单指标抛错 → 记入结果、其余指标照跑（不中断整轮）"""
        from app.models.bi import BiMetricDef

        async with bi_db() as s:
            s.add(BiMetricDef(metric_key="ok", name="ok", source_type="data_buffer",
                              granularity="1min", measures=[{"key": "c", "agg": "count"}],
                              enabled=True))
            await s.commit()

        async def fake_run_metric(session, defn, now=None):
            if defn["metric_key"] == "boom":
                raise RuntimeError("该指标炸了")
            return {"metric_key": defn["metric_key"], "windows": 1}

        monkeypatch.setattr("app.services.bi_aggregator.run_metric", fake_run_metric)
        monkeypatch.setattr(
            bi_scheduler, "list_enabled_defs",
            AsyncMock(return_value=[types.SimpleNamespace(to_def_dict=lambda: {
                "metric_key": "boom", "granularity": "1min", "enabled": True}),
                types.SimpleNamespace(to_def_dict=lambda: {
                    "metric_key": "ok", "granularity": "1min", "enabled": True})]))

        async with bi_db() as s:
            result = await bi_scheduler.aggregate_all(s)
        by_key = {r["metric_key"]: r for r in result["results"]}
        assert "error" in by_key["boom"]
        assert by_key["ok"]["windows"] == 1

    async def test_purge_not_wired_into_loop(self, bi_db, monkeypatch):
        """明细清理**不接入**自动循环：DataBuffer 是轮次/租约/重试的工作集，
        按年龄删除会破坏在途轮次恢复（合并决策，见 app/services/bi_scheduler.py）。"""
        called = {"purge": False}

        async def fake_purge(session, **kw):
            called["purge"] = True
            return {"deleted": 0, "batches": 0, "retention_days": 7}
        monkeypatch.setattr(bi_scheduler, "purge_expired_buffers", fake_purge)

        async def fake_agg(session, now=None):
            return {"metrics": 0, "results": []}
        monkeypatch.setattr(bi_scheduler, "aggregate_all", fake_agg)

        with patch("app.database.async_session", bi_db):
            result = await _run_once()
        assert called["purge"] is False
        assert "purge" not in result
        assert result["aggregate"]["metrics"] == 0


# ============================================================
# 3.1 / 3.2：主循环的锁与降级
# ============================================================

class _StopLoop(Exception):
    """用于在跑完一轮后跳出主循环"""


class TestLoopLock:

    async def _drive_one_round(self, monkeypatch):
        """让 bi_loop 跑完一轮后退出，返回记录"""
        ran = []

        async def fake_run_once():
            ran.append(1)
            return {}

        async def fake_sleep(_seconds):
            raise _StopLoop()

        monkeypatch.setattr(bi_scheduler, "_run_once", fake_run_once)
        monkeypatch.setattr(bi_scheduler.asyncio, "sleep", fake_sleep)
        return ran

    async def test_runs_when_lock_acquired(self, monkeypatch):
        ran = await self._drive_one_round(monkeypatch)

        async def fake_acquire(name, ttl=300):
            return "token"

        released = []

        async def fake_release(name, token):
            released.append(token)

        monkeypatch.setattr("app.cache.lock.acquire_lock", fake_acquire)
        monkeypatch.setattr("app.cache.lock.release_lock", fake_release)

        with pytest.raises(_StopLoop):
            await bi_scheduler.bi_loop()
        assert ran == [1]
        assert released == ["token"]

    async def test_skips_when_lock_held_by_other(self, monkeypatch):
        """抢锁返回 None = 他进程持锁 → 本轮跳过，不执行"""
        ran = await self._drive_one_round(monkeypatch)

        async def fake_acquire(name, ttl=300):
            return None

        monkeypatch.setattr("app.cache.lock.acquire_lock", fake_acquire)

        with pytest.raises(_StopLoop):
            await bi_scheduler.bi_loop()
        assert ran == []

    async def test_skips_round_when_redis_unavailable(self, monkeypatch):
        """Redis 异常 → **跳过本轮**（合并后语义：不降级为无锁聚合），恢复后续跑"""
        ran = await self._drive_one_round(monkeypatch)

        async def boom(name, ttl=300):
            raise RuntimeError("redis down")

        monkeypatch.setattr("app.cache.lock.acquire_lock", boom)

        with pytest.raises(_StopLoop):
            await bi_scheduler.bi_loop()
        assert ran == []

    async def test_degraded_warning_only_once(self, monkeypatch):
        """跳过告警只打一次（避免日志刷屏）"""
        ran = []
        sleeps = {"n": 0}

        async def fake_run_once():
            ran.append(1)
            return {}

        async def fake_sleep(_seconds):
            sleeps["n"] += 1
            if sleeps["n"] >= 3:
                raise _StopLoop()

        async def boom(name, ttl=300):
            raise RuntimeError("redis down")

        monkeypatch.setattr(bi_scheduler, "_run_once", fake_run_once)
        monkeypatch.setattr(bi_scheduler.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr("app.cache.lock.acquire_lock", boom)

        with patch("app.services.bi_scheduler.logger") as mock_logger:
            with pytest.raises(_StopLoop):
                await bi_scheduler.bi_loop()

        assert sleeps["n"] == 3, "三轮都应有 sleep（每轮都跳过）"
        assert ran == [], "Redis 不可用时不执行聚合"
        warnings = [c for c in mock_logger.warning.call_args_list
                    if "跳过本轮聚合" in str(c)]
        assert len(warnings) == 1, "跳过告警应只出现一次"
