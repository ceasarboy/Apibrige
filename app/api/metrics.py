"""监控统计 API — SQL 聚合 + Redis 缓存 30s

统计窗口与分位数为跨数据库实现（datetime 边界 + Python 分位），
兼容 SQLite 与 PostgreSQL。
"""
import asyncio
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_session
from app.models.sync import ExecutionLog, SyncTask, DataBuffer
from app.schemas import MetricsOverview, MetricsTrend, MetricsTrendItem
from app.redis_client import get_redis

router = APIRouter()

_CACHE_TTL = 30  # 秒，与看板刷新间隔一致


async def _cache_client():
    """Redis 不可用时返回 None；看板统计仍可直接从平台数据库读取。"""
    try:
        client = await get_redis()
        await client.ping()
        return client
    except Exception:
        return None


def _percentile(values: list, q: float):
    """线性插值分位数（跨数据库）；空集返回 0"""
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low, high = int(pos), min(int(pos) + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


@router.get("/overview", response_model=MetricsOverview)
async def get_overview(
    period: str = Query("24h", description="统计周期: 1h/6h/24h/7d"),
    session: AsyncSession = Depends(get_session),
):
    """获取监控概览（带 Redis 缓存）"""
    cache_key = f"metrics:overview:{period}"
    redis = await _cache_client()
    cached = await redis.get(cache_key) if redis is not None else None
    if cached:
        import json
        return json.loads(cached)

    # 确定时间段（Python 侧算边界，跨 SQLite/PG）
    window_map = {"1h": 1, "6h": 6, "24h": 24, "7d": 24 * 7}
    hours = window_map.get(period, 24)
    window_start = datetime.now(timezone.utc) - timedelta(hours=hours)

    # 同步总量
    count_stmt = select(func.count(ExecutionLog.id)).where(
        ExecutionLog.created_at >= window_start
    )
    sync_count = (await session.execute(count_stmt)).scalar() or 0

    # 成功率
    success_stmt = select(func.count(ExecutionLog.id)).where(
        ExecutionLog.created_at >= window_start,
        ExecutionLog.status == "success",
    )
    success_count = (await session.execute(success_stmt)).scalar() or 0
    success_rate = round(success_count / sync_count * 100, 2) if sync_count > 0 else 100.0

    # 延迟分位数：取窗口内耗时值，Python 侧计算（跨库）。
    # 上限保护（审查 P2）：超长窗口 × 高频日志曾把全窗口行读进内存（form-rows
    # 502 的同根因模式）；超限时分位数按样本近似，不再无界。
    _LATENCY_SAMPLE_MAX = 100_000
    latency_stmt = select(ExecutionLog.duration_ms).where(
        ExecutionLog.created_at >= window_start,
        ExecutionLog.duration_ms.isnot(None),
    ).limit(_LATENCY_SAMPLE_MAX)
    duration_values = [v for v in (await session.execute(latency_stmt)).scalars().all()]
    p50 = _percentile(duration_values, 0.50)
    p95 = _percentile(duration_values, 0.95)
    p99 = _percentile(duration_values, 0.99)

    # 队列深度（pending + processing 的 buffer 数）
    queue_stmt = select(func.count(DataBuffer.id)).where(
        DataBuffer.status.in_(["pending", "processing"]),
    )
    queue_depth = (await session.execute(queue_stmt)).scalar() or 0

    # 任务状态分布
    task_stmt = select(SyncTask.status, func.count(SyncTask.id)).group_by(SyncTask.status)
    task_results = (await session.execute(task_stmt)).all()
    task_distribution = {row[0]: row[1] for row in task_results}

    result = MetricsOverview(
        sync_count=sync_count,
        success_rate=success_rate,
        p50_latency=float(p50),
        p95_latency=float(p95),
        p99_latency=float(p99),
        queue_depth=queue_depth,
        task_status_distribution=task_distribution,
    )

    # 缓存 30 秒
    if redis is not None:
        try:
            await redis.set(cache_key, result.model_dump_json(), ex=_CACHE_TTL)
        except Exception:
            pass
    return result


@router.get("/trend", response_model=MetricsTrend)
async def get_trend(
    period: str = Query("24h", description="统计周期"),
    interval_h: int = Query(1, ge=1, le=24, alias="interval", description="聚合间隔（小时）"),
    session: AsyncSession = Depends(get_session),
):
    """获取同步趋势数据（带 Redis 缓存）"""
    cache_key = f"metrics:trend:{period}:{interval_h}"
    redis = await _cache_client()
    cached = await redis.get(cache_key) if redis is not None else None
    if cached:
        import json
        result = json.loads(cached)
        return MetricsTrend(**result)

    window_map = {"1h": 1, "6h": 6, "24h": 24, "7d": 24 * 7}
    hours = window_map.get(period, 24)
    window_start = datetime.now(timezone.utc) - timedelta(hours=hours)
    bucket = timedelta(hours=interval_h)

    # 跨库聚合：取窗口内 (时间, 状态) 原始行，Python 侧按小时桶分组
    # （上限保护同延迟分位数：超限时趋势按最近样本近似）
    stmt = select(ExecutionLog.created_at, ExecutionLog.status).where(
        ExecutionLog.created_at >= window_start,
    ).limit(100_000)
    rows = (await session.execute(stmt)).all()

    buckets: dict[datetime, dict[str, int]] = {}
    for created_at, status in rows:
        ts = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
        offset = (ts - window_start) // bucket
        slot = window_start + offset * bucket
        entry = buckets.setdefault(slot, {"success": 0, "failed": 0})
        entry["success" if status == "success" else "failed"] += 1

    items = [
        MetricsTrendItem(
            timestamp=slot.isoformat(),
            success=counts["success"],
            failed=counts["failed"],
        )
        for slot, counts in sorted(buckets.items())
    ]

    result = MetricsTrend(items=items, interval=f"{interval_h}h")

    if redis is not None:
        try:
            await redis.set(cache_key, result.model_dump_json(), ex=_CACHE_TTL)
        except Exception:
            pass
    return result
