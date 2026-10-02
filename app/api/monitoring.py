"""Additive operational monitoring; counts are execution-log and buffer units."""
from datetime import datetime, timedelta, timezone
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_session
from app.models.sync import ApiConnection, DataBuffer, ExecutionLog, FieldMapping, SyncTask
from app.bi_schemas import TaskMetricItem, ConnectionMetricItem, ErrorMetricItem, TaskRunItem
from app.security.desensitize import redact_text
router = APIRouter()
_PERIOD_HOURS = {"1h": 1, "6h": 6, "24h": 24, "7d": 24 * 7}
# 每个错误分组保留的原样本上限（spec：≤ 50 条）
_MAX_ERROR_SAMPLES = 50


def _window_start(period: str) -> datetime:
    return datetime.now(timezone.utc) - timedelta(
        hours=_PERIOD_HOURS.get(period, 24))


async def _task_name_map(session: AsyncSession) -> dict[str, str]:
    """任务 id → 显示名（取映射名，缺省回退短 id）"""
    rows = (await session.execute(
        select(SyncTask.id, FieldMapping.name)
        .join(FieldMapping, SyncTask.mapping_id == FieldMapping.id)
    )).all()
    return {str(tid): (name or str(tid)[:8]) for tid, name in rows}


async def _task_connection_map(session: AsyncSession) -> dict[str, tuple[str, str]]:
    """任务 id → (源连接 id, 目标连接 id)"""
    rows = (await session.execute(
        select(SyncTask.id, FieldMapping.source_connection_id,
               FieldMapping.target_connection_id)
        .join(FieldMapping, SyncTask.mapping_id == FieldMapping.id)
    )).all()
    return {str(t): (str(s), str(g)) for t, s, g in rows}


async def _log_stats(session, start):
    """Aggregate in the DB; avoid loading every log into the API process."""
    return (await session.execute(select(
        ExecutionLog.task_id,
        func.sum(case((ExecutionLog.status == "success", 1), else_=0)),
        func.sum(case((ExecutionLog.status == "failed", 1), else_=0)),
        func.sum(ExecutionLog.duration_ms), func.count(ExecutionLog.duration_ms),
        func.max(ExecutionLog.created_at),
    ).where(ExecutionLog.created_at >= start).group_by(ExecutionLog.task_id))).all()


@router.get("/tasks", response_model=list[TaskMetricItem])
async def get_task_metrics(period: str = Query("24h"), session: AsyncSession = Depends(get_session)):
    start = _window_start(period)
    stats = {str(r[0]): r for r in await _log_stats(session, start)}
    counts = {str(t): int(n) for t, n in (await session.execute(
        select(DataBuffer.task_id, func.count(DataBuffer.id))
        .where(DataBuffer.created_at >= start).group_by(DataBuffer.task_id))).all()}
    names = await _task_name_map(session)
    items = []
    for key in sorted(set(names) | set(stats)):
        row = stats.get(key)
        items.append(TaskMetricItem(
            task_id=key, task_name=names.get(key, key[:8]),
            success=int(row[1] or 0) if row else 0,
            failed=int(row[2] or 0) if row else 0,
            last_run_at=row[5].isoformat() if row and row[5] else None,
            avg_duration_ms=round(row[3] / row[4], 2) if row and row[4] else None,
            buffered_batches=counts.get(key, 0)))
    return sorted(items, key=lambda item: (-item.failed, -item.success))


@router.get("/connections", response_model=list[ConnectionMetricItem])
async def get_connection_metrics(period: str = Query("24h"), session: AsyncSession = Depends(get_session)):
    task_connections = await _task_connection_map(session)
    names = {str(k): v for k, v in (await session.execute(select(ApiConnection.id, ApiConnection.name))).all()}
    stats = {}
    for task_id, ok, failed, duration, count, _ in await _log_stats(session, _window_start(period)):
        pair = task_connections.get(str(task_id))
        if not pair:
            continue
        for side, key in (("source", pair[0]), ("target", pair[1])):
            entry = stats.setdefault((key, side), [0, 0, 0, 0])
            for i, value in enumerate((ok, failed, duration, count)):
                entry[i] += value or 0
    items = []
    for (key, side), (ok, failed, duration, count) in stats.items():
        items.append(ConnectionMetricItem(
            connection_id=key, connection_name=names.get(key, key[:8]), side=side,
            success_rate=round(ok / (ok + failed) * 100, 2) if ok + failed else 100,
            avg_latency_ms=round(duration / count, 2) if count else None, failed=int(failed)))
    return sorted(items, key=lambda item: (item.success_rate, -item.failed))


@router.get("/errors", response_model=list[ErrorMetricItem])
async def get_error_metrics(
    period: str = Query("24h", description="统计周期: 1h/6h/24h/7d"),
    limit: int = Query(10, ge=1, le=100, description="Top N"),
    session: AsyncSession = Depends(get_session),
):
    """失败原因归一化聚合 Top N（每项附 ≤50 条原样本）"""
    from app.services.bi_defs import normalize_error

    window_start = _window_start(period)
    rows = (await session.execute(
        select(ExecutionLog.error_message, ExecutionLog.created_at)
        .where(ExecutionLog.status == "failed",
               ExecutionLog.created_at >= window_start)
        .limit(50_000)   # 上限保护：Top N 归一化按样本近似，不无界读（审查 P2）
    )).all()

    groups: dict[str, dict] = {}
    for message, created in rows:
        message = redact_text(message)
        key = normalize_error(message)
        entry = groups.setdefault(key, {"count": 0, "last": None, "samples": []})
        entry["count"] += 1
        if entry["last"] is None or created > entry["last"]:
            entry["last"] = created
        if (message and len(entry["samples"]) < _MAX_ERROR_SAMPLES
                and message not in entry["samples"]):
            entry["samples"].append(message)

    top = sorted(groups.items(), key=lambda kv: -kv[1]["count"])[:limit]
    return [
        ErrorMetricItem(
            error_type=key, count=entry["count"],
            last_seen=entry["last"].isoformat() if entry["last"] else None,
            samples=entry["samples"],
        )
        for key, entry in top
    ]


@router.get("/tasks/{task_id}/runs", response_model=list[TaskRunItem])
async def get_task_runs(
    task_id: UUID,
    limit: int = Query(20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """单任务最近 N 次执行明细（看板下钻用）"""
    task = await session.get(SyncTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")

    logs = (await session.execute(
        select(ExecutionLog)
        .where(ExecutionLog.task_id == task_id)
        .order_by(ExecutionLog.created_at.desc())
        .limit(limit)
    )).scalars().all()

    return [
        TaskRunItem(
            created_at=log.created_at.isoformat() if log.created_at else None,
            action=log.action, status=log.status,
            duration_ms=log.duration_ms, error_message=redact_text(log.error_message),
        )
        for log in logs
    ]
