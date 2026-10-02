"""同步任务 API — CRUD + 执行 + 暂停/恢复"""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_session
from app.models.sync import SyncTask, FieldMapping, ApiConnection, DataBuffer
from app.schemas import TaskCreate, TaskUpdate, TaskResponse, MessageResponse
from app.services.router import route_transfer

router = APIRouter()


# ============================================================
# CRUD
# ============================================================

@router.post("", response_model=TaskResponse, status_code=201)
async def create_task(
    payload: TaskCreate,
    session: AsyncSession = Depends(get_session),
):
    """创建同步任务"""
    # mapping_id 必须真实存在：SQLite 未开外键，不校验会建出"永远静默不执行"
    # 的僵尸任务（调度器 scalar_one 抛异常只落日志）（审查 P2）
    if await session.get(FieldMapping, payload.mapping_id) is None:
        raise HTTPException(422, detail={"code": "mapping_not_found",
                                         "message": "mapping_id 不存在"})
    from app.services.incremental_config import validate_task_change
    await validate_task_change(session, payload.model_dump())
    task = SyncTask(
        checkpoint_version=0,
        mapping_id=payload.mapping_id,
        task_type=payload.task_type,
        schedule_cron=payload.schedule_cron,
        webhook_config=payload.webhook_config,
        sync_mode=payload.sync_mode,
        fallback_to_async=payload.fallback_to_async,
        auto_threshold=payload.auto_threshold,
        depends_on_task_id=payload.depends_on_task_id,
        conflict_strategy=payload.conflict_strategy,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    status: str | None = Query(None, description="按状态过滤: active/paused/error"),
    session: AsyncSession = Depends(get_session),
):
    """查询任务列表"""
    stmt = select(SyncTask).order_by(SyncTask.created_at.desc())
    if status:
        stmt = stmt.where(SyncTask.status == status)
    result = await session.execute(stmt)
    return result.scalars().all()


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: UUID, session: AsyncSession = Depends(get_session)):
    """查询单个任务"""
    task = await session.get(SyncTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task


@router.put("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: UUID,
    payload: TaskUpdate,
    session: AsyncSession = Depends(get_session),
):
    """更新任务"""
    task = await session.get(SyncTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")

    update_data = payload.model_dump(exclude_unset=True)
    from app.api._guards import reject_null_non_nullable
    update_data = reject_null_non_nullable(SyncTask, update_data)
    if update_data.get("mapping_id") is not None             and await session.get(FieldMapping, update_data["mapping_id"]) is None:
        raise HTTPException(422, detail={"code": "mapping_not_found",
                                         "message": "mapping_id 不存在"})
    from app.services.incremental_config import validate_task_change
    await validate_task_change(session, update_data, task)
    for field, value in update_data.items():
        setattr(task, field, value)

    # 改 cron 即重算 next_run_at（审查 P2）：调度器只筛 next_run_at <= now，
    # 不重算的话"改成更高频"要等旧时间点才生效（最坏一个旧周期），"改低频"
    # 会在旧时间点多跑一次。空 cron → None（与调度器空 cron 语义一致）。
    if "schedule_cron" in update_data:
        from datetime import datetime as _dt, timezone as _tz
        from app.services.scheduler import next_run_from_cron
        cron = update_data["schedule_cron"]
        if cron:
            nxt = next_run_from_cron(cron, _dt.now(_tz.utc))
            # 库列是 naive TIMESTAMP，统一存 naive UTC
            task.next_run_at = nxt.replace(tzinfo=None) if nxt else None
        else:
            task.next_run_at = None

    await session.commit()
    await session.refresh(task)
    return task


@router.delete("/{task_id}", response_model=MessageResponse)
async def delete_task(task_id: UUID, session: AsyncSession = Depends(get_session)):
    """删除任务"""
    from app.services.incremental_rounds import lock_task
    locked = await lock_task(session, task_id)
    from app.services.hap_execution import guard_pending
    await guard_pending(session, [task_id])
    if locked is not None and locked.active_round_id:
        raise HTTPException(409, '存在未完成轮次，不能删除任务')
    task = await session.get(SyncTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")

    await session.delete(task)
    await session.commit()
    return MessageResponse(message="任务已删除", id=task_id)


# ============================================================
# 执行 / 暂停 / 恢复
# ============================================================

@router.post("/{task_id}/run")
async def run_task(task_id: UUID, session: AsyncSession = Depends(get_session)):
    """触发同步任务（按任务配置的 sync_mode 路由）"""
    task = (await session.execute(select(SyncTask).where(SyncTask.id == task_id))).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, "任务不存在")

    mapping = (await session.execute(select(FieldMapping).where(FieldMapping.id == task.mapping_id))).scalar_one_or_none()
    if mapping is None:
        raise HTTPException(404, "字段映射不存在")

    source_conn = (await session.execute(select(ApiConnection).where(ApiConnection.id == mapping.source_connection_id))).scalar_one()
    target_conn = (await session.execute(select(ApiConnection).where(ApiConnection.id == mapping.target_connection_id))).scalar_one()

    result = await route_transfer(session, task, mapping, source_conn, target_conn)
    return result


@router.post("/{task_id}/pause", response_model=MessageResponse)
async def pause_task(task_id: UUID, session: AsyncSession = Depends(get_session)):
    """暂停任务"""
    from app.services.incremental_rounds import lock_task
    task = await lock_task(session, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status == "paused":
        raise HTTPException(400, "任务已暂停")
    task.status = "paused"
    await session.commit()
    return MessageResponse(message="任务已暂停", id=task_id)


@router.post("/{task_id}/resume", response_model=MessageResponse)
async def resume_task(task_id: UUID, session: AsyncSession = Depends(get_session)):
    """恢复任务"""
    task = await session.get(SyncTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status != "paused":
        raise HTTPException(400, "任务未处于暂停状态")
    task.status = "active"
    await session.commit()
    return MessageResponse(message="任务已恢复", id=task_id)


@router.get('/{task_id}/round')
async def get_round(task_id: UUID, session: AsyncSession = Depends(get_session)):
    from app.services.incremental_rounds import round_detail
    task = await session.get(SyncTask, task_id)
    if task is None:
        raise HTTPException(404, '任务不存在')
    return await round_detail(session, task)


@router.post('/{task_id}/round/{round_id}/batch/{batch_id}/refresh-source')
async def refresh_failed_batch_source(task_id: UUID, round_id: UUID, batch_id: UUID, expected_revision: int = 0, session: AsyncSession = Depends(get_session)):
    task = await session.get(SyncTask, task_id)
    if task is None: raise HTTPException(404, 'task missing')
    from app.services.batch_refresh import refresh_batch
    return await refresh_batch(session, task_id, round_id, batch_id, expected_revision)


@router.post('/{task_id}/round/retry')
async def retry_incremental_round(task_id: UUID, session: AsyncSession = Depends(get_session)):
    from app.services.incremental_rounds import retry_round, process_batch, round_detail
    task = await session.get(SyncTask, task_id)
    if task is None:
        raise HTTPException(404, '任务不存在')
    result = await retry_round(session, task)
    await session.commit()
    if task.sync_mode == 'sync':
        for batch in result['batches']:
            await process_batch(batch['id'])
        await session.refresh(task)
        result = await round_detail(session, task, UUID(result['round_id']))
    return result


@router.delete('/{task_id}/round')
async def delete_failed_round(task_id: UUID, session: AsyncSession = Depends(get_session)):
    from app.services.incremental_rounds import delete_unsealed_round
    task = await session.get(SyncTask, task_id)
    if task is None:
        raise HTTPException(404, '任务不存在')
    return await delete_unsealed_round(session, task)


@router.get('/{task_id}/hap-progress')
async def hap_progress(task_id: UUID, session: AsyncSession = Depends(get_session)):
    if await session.get(SyncTask, task_id) is None:
        raise HTTPException(404, '任务不存在')
    from app.services.hap_execution import progress
    return await progress(session, task_id)


@router.post('/{task_id}/hap-retry')
async def hap_retry(task_id: UUID, session: AsyncSession = Depends(get_session)):
    task = await session.get(SyncTask, task_id)
    if task is None:
        raise HTTPException(404, '任务不存在')
    if task.active_round_id:
        return await retry_incremental_round(task_id, session)
    from app.services.incremental import unseal
    pending = (await session.scalars(select(DataBuffer).where(DataBuffer.task_id == task_id,
        DataBuffer.round_id.is_(None), DataBuffer.status.in_(['pending','processing','failed','validation_failed']),
        DataBuffer.config_snapshot.isnot(None)))).all()
    if not any(unseal(b.config_snapshot).get('mapping_config', {}).get('hap_write_mode') == 'upsert' for b in pending):
        raise HTTPException(409, '没有未完成的 HAP 全量批次；恢复不会创建新一轮写入')
    return await run_task(task_id, session)


@router.post('/{task_id}/hap-cancel-unsent')
async def hap_cancel_unsent(task_id: UUID, session: AsyncSession = Depends(get_session)):
    """只取消尚未发送的全量批次，不允许丢弃未知写入结果。"""
    from app.services.hap_execution import cancel_unsent
    return await cancel_unsent(session, task_id)
