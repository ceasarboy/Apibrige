"""任务编排服务 — 检查任务依赖，确保前置任务完成"""
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.sync import SyncTask, DataBuffer


async def check_dependency_ready(session: AsyncSession, task: SyncTask) -> tuple[bool, str | None]:
    """检查前置依赖任务是否已完成

    Args:
        session: 数据库会话
        task: 当前任务

    Returns:
        (ready, reason) - ready 为 True 时可执行，否则 reason 说明原因
    """
    if task.depends_on_task_id is None:
        return True, None

    dep_task = await session.get(SyncTask, task.depends_on_task_id)
    if dep_task is None:
        return True, "前置任务已删除（视为就绪）"

    # 前置任务执行状态（通过查询其 data_buffer 完成情况）
    result = await session.execute(
        select(DataBuffer).where(
            DataBuffer.task_id == dep_task.id,
            DataBuffer.status.in_(["completed", "failed"]),
        )
    )
    buffers = result.scalars().all()

    # 检查前置任务是否有 completed 的 buffer（至少一次成功执行）
    completed = [b for b in buffers if b.status == "completed"]
    if not completed:
        return False, f"前置任务 {dep_task.id} 尚未成功执行"

    return True, None
