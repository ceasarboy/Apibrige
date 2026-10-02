"""操作日志记录器"""
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.sync import ExecutionLog
from app.security.desensitize import desensitize_record, redact_text


async def log_execution(
    session: AsyncSession,
    task_id,
    action: str,
    status: str,
    buffer_id=None,
    request_data: dict | None = None,
    response_data: dict | None = None,
    error_message: str | None = None,
    duration_ms: int | None = None,
    error: Exception | None = None,
    mapping_config: dict | None = None,
):
    """记录执行日志"""
    if status == 'failed':
        from app.services.failure_logs import error_detail, normalize_detail, safe_message
        response_data = dict(response_data or {})
        if isinstance(mapping_config, dict):
            response_data['mapping_fields'] = [{k:f[k] for k in ('source','target') if isinstance(f.get(k),str)}
                                               for f in mapping_config.get('fields', []) if isinstance(f,dict)]
        raw = response_data.get('error_detail') or response_data
        response_data['error_detail'] = error_detail(error, action) if error else normalize_detail(raw, action)
        if error is not None:
            error_message = safe_message(error)
    log = ExecutionLog(
        task_id=task_id,
        buffer_id=buffer_id,
        action=action,
        status=status,
        request_data=desensitize_record(request_data),
        response_data=desensitize_record(response_data),
        error_message=redact_text(error_message),
        duration_ms=duration_ms,
    )
    session.add(log)
    await session.commit()
