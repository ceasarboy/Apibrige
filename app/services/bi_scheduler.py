"""独立 BI 聚合调度：默认关闭，不清理同步缓冲；Redis 不可用时跳过本轮。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, or_, select

from app.config import settings

logger = logging.getLogger(__name__)

_LOCK_NAME = "bi_aggregation_tick"
_LOCK_TTL = 120
_loop_task: asyncio.Task | None = None
_degraded_warned = False


# ============================================================
# 聚合一轮
# ============================================================

async def list_enabled_defs(session):
    """取所有启用中的指标定义"""
    from app.models.bi import BiMetricDef

    return (await session.execute(
        select(BiMetricDef).where(BiMetricDef.enabled.is_(True))
    )).scalars().all()


async def check_watermark_lag(
    session, defn: dict, now: datetime | None = None
) -> dict | None:
    """水位线滞后检测；未超阈值返回 None（task 3.3）"""
    from app.models.bi import BiAggregationWatermark
    from app.services.bi_aggregator import to_utc
    from app.services.bi_defs import GRANULARITY_SECONDS

    now = now or datetime.now(timezone.utc)
    wm = await session.get(BiAggregationWatermark, defn["metric_key"])
    if wm is None or wm.last_bucket is None:
        return None

    window_seconds = GRANULARITY_SECONDS.get(defn.get("granularity", "1min"), 60)
    threshold = settings.bi_watermark_lag_warn_windows * window_seconds
    lag_seconds = (now - to_utc(wm.last_bucket)).total_seconds()
    if lag_seconds <= threshold:
        return None
    return {
        "metric_key": defn["metric_key"],
        "lag_seconds": int(lag_seconds),
        "lag_windows": int(lag_seconds // window_seconds),
        "threshold_seconds": threshold,
    }


from app.services.bi_runtime import serialize_aggregation

@serialize_aggregation
async def aggregate_all(session, now: datetime | None = None) -> dict:
    """对所有启用指标跑一轮增量聚合

    单指标异常被捕获并记入结果，不中断其余指标（隔离）。
    """
    from app.services.bi_aggregator import run_metric

    now = now or datetime.now(timezone.utc)
    defs = await list_enabled_defs(session)
    results: list[dict] = []

    definitions = [row.to_def_dict() for row in defs]
    for defn in definitions:
        try:
            async with asyncio.timeout(settings.bi_query_timeout_seconds):
                result = await run_metric(session, defn, now=now)
            lag = await check_watermark_lag(session, defn, now)
            if lag:
                logger.warning(
                    "BI 指标 %s 水位线滞后 %s 个窗口（阈值 %s 窗口），"
                    "聚合可能已停摆，请检查调度与数据量",
                    lag["metric_key"], lag["lag_windows"],
                    settings.bi_watermark_lag_warn_windows)
                result["lag_warning"] = lag
            results.append(result)
        except Exception as e:  # 单指标失败隔离
            await session.rollback()
            logger.warning("BI 指标 %s 聚合失败 (%s)", defn.get("metric_key"), type(e).__name__)
            results.append({"metric_key": defn.get("metric_key"), "error": "BI aggregation failed"})

    return {"metrics": len(defs), "results": results}


# ============================================================
# 明细保留清理（**不接入自动循环**，见下方说明）
# ============================================================

async def purge_expired_buffers(
    session,
    retention_days: int | None = None,
    batch_size: int | None = None,
    max_batches: int | None = None,
    now: datetime | None = None,
) -> dict:
    """分批清理过期中间明细（保留能力，需显式调用）

    过期判定：`expires_at` 已过（若非空），否则 `created_at` 早于保留期。
    单批行数有上限，避免长事务与锁竞争。聚合结果（bi_metric_buckets）不受影响。

    ⚠️ 本函数**不**由 `_run_once` / `bi_loop` 调用：合并后的 DataBuffer 是增量轮次的
    working set（round_id / batch_no / attempt_token / lease_until / next_retry_at），
    按年龄删除会破坏在途轮次与失败重试恢复。如需清理，请由运维显式调用，
    或在确认无在途轮次后另行接入维护任务。
    """
    from app.models.sync import DataBuffer

    retention_days = (settings.bi_retention_days
                      if retention_days is None else retention_days)
    batch_size = settings.bi_purge_batch_size if batch_size is None else batch_size
    max_batches = settings.bi_purge_max_batches if max_batches is None else max_batches
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)

    expired = or_(
        and_(DataBuffer.expires_at.isnot(None), DataBuffer.expires_at < now),
        and_(DataBuffer.expires_at.is_(None), DataBuffer.created_at < cutoff),
    )

    deleted = 0
    batches = 0
    while batches < max_batches:
        ids = (await session.execute(
            select(DataBuffer.id).where(expired).limit(batch_size)
        )).scalars().all()
        if not ids:
            break
        await session.execute(delete(DataBuffer).where(DataBuffer.id.in_(ids)))
        await session.commit()
        deleted += len(ids)
        batches += 1
        if len(ids) < batch_size:
            break

    return {"deleted": deleted, "batches": batches, "retention_days": retention_days}


# ============================================================
# 单轮聚合（不清理同步数据）
# ============================================================

async def _run_once() -> dict:
    from app.database import async_session
    async with async_session() as session:
        return {"aggregate": await aggregate_all(session)}

async def bi_loop() -> None:
    """BI 调度主循环

    每轮先经 Redis 锁抢占：多 worker 部署时只允许一个进程执行。
    Redis 异常时跳过本轮聚合（单 worker 默认部署安全），告警只打一次。
    抢锁失败（返回 None）说明他进程在跑，本轮直接跳过。
    """
    from app.cache import lock as redis_lock

    global _degraded_warned

    interval = settings.bi_tick_interval_seconds
    logger.info("BI 聚合调度器已启动（间隔 %ss）", interval)

    while True:
        token = None
        try:
            try:
                token = await redis_lock.acquire_lock(_LOCK_NAME, ttl=_LOCK_TTL)
            except Exception as lock_err:
                if not _degraded_warned:
                    logger.warning(
                        "BI 聚合锁不可用（Redis 异常: %s），跳过本轮聚合——"
                        "等待 Redis 恢复后续跑",
                        lock_err)
                    _degraded_warned = True

            if token is not None:
                async with asyncio.timeout(60):
                    await _run_once()
            # else: 抢锁失败 = 他进程持锁，本轮跳过
        except Exception as e:
            logger.warning("BI 调度器轮次异常 (%s)", type(e).__name__)
        finally:
            if token is not None:
                try:
                    await redis_lock.release_lock(_LOCK_NAME, token)
                except Exception as rel_err:
                    logger.warning("BI 聚合锁释放失败: %s", rel_err)

        await asyncio.sleep(interval)


def start() -> asyncio.Task:
    """启动 BI 调度器（幂等）"""
    global _loop_task
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(bi_loop())
    return _loop_task


async def stop() -> None:
    """停止 BI 调度器"""
    global _loop_task
    if _loop_task is not None:
        _loop_task.cancel()
        try:
            await _loop_task
        except (asyncio.CancelledError, Exception):
            pass
        _loop_task = None
