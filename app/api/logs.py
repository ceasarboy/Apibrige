"""日志查询 API — 支持时间/任务/状态过滤的分页查询"""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_session
from app.models.sync import ExecutionLog
from app.schemas import LogResponse, PaginatedLogs
from app.security.desensitize import desensitize_record, redact_text


def _safe_log(log):
    out = LogResponse.model_validate(log).model_dump()
    out['request_data'] = desensitize_record(out.get('request_data'))
    out['response_data'] = desensitize_record(out.get('response_data'))
    out['error_message'] = redact_text(out.get('error_message'))
    if log.status == 'failed':
        from app.services.failure_logs import normalize_detail
        data = out.get('response_data') or {}
        out['response_data'] = {**data, 'error_detail': normalize_detail(data.get('error_detail') or data, log.action)}
    return LogResponse(**out)

router = APIRouter()


@router.get("", response_model=PaginatedLogs)
async def list_logs(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    task_id: UUID | None = Query(None, description="按任务过滤"),
    status: str | None = Query(None, description="按状态过滤: success/failed"),
    action: str | None = Query(None, description="按动作过滤: fetch/transform/push"),
    start_time: str | None = Query(None, description="开始时间 ISO 格式"),
    end_time: str | None = Query(None, description="结束时间 ISO 格式"),
    session: AsyncSession = Depends(get_session),
):
    """分页查询执行日志

    支持组合过滤条件: task_id, status, action, 时间范围
    """
    # 构建查询
    stmt = select(ExecutionLog)
    count_stmt = select(func.count(ExecutionLog.id))

    if task_id:
        stmt = stmt.where(ExecutionLog.task_id == task_id)
        count_stmt = count_stmt.where(ExecutionLog.task_id == task_id)
    if status:
        stmt = stmt.where(ExecutionLog.status == status)
        count_stmt = count_stmt.where(ExecutionLog.status == status)
    if action:
        stmt = stmt.where(ExecutionLog.action == action)
        count_stmt = count_stmt.where(ExecutionLog.action == action)
    from datetime import datetime

    def _parse_dt(v: str | None, name: str):
        if not v:
            return None
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(422, f"{name} 不是合法的 ISO 时间: {v}") from None

    start_dt = _parse_dt(start_time, "start_time")
    end_dt = _parse_dt(end_time, "end_time")
    if start_dt:
        stmt = stmt.where(ExecutionLog.created_at >= start_dt)
        count_stmt = count_stmt.where(ExecutionLog.created_at >= start_dt)
    if end_dt:
        stmt = stmt.where(ExecutionLog.created_at <= end_dt)
        count_stmt = count_stmt.where(ExecutionLog.created_at <= end_dt)

    # 总数
    total = (await session.execute(count_stmt)).scalar()

    # 分页
    offset = (page - 1) * page_size
    stmt = stmt.order_by(ExecutionLog.created_at.desc()).offset(offset).limit(page_size)
    result = await session.execute(stmt)
    logs = result.scalars().all()

    return PaginatedLogs(
        items=[_safe_log(log) for log in logs],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{log_id}", response_model=LogResponse)
async def get_log(log_id: UUID, session: AsyncSession = Depends(get_session),
                  record_page: int = Query(1, ge=1), record_page_size: int = Query(20, ge=1, le=100)):
    """查询单条日志详情"""
    log = await session.get(ExecutionLog, log_id)
    if log is None:
        raise HTTPException(404, "日志不存在")
    result = _safe_log(log)
    if log.status == 'failed':
        from app.services.failure_logs import failure_context
        result.response_data = {**(result.response_data or {}),
            'failure_context': await failure_context(session, log, record_page, record_page_size)}
    return result
