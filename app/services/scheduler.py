"""进程内定时调度器（task 2.3）

每 30 秒扫描一次 scheduled 任务：next_run_at 到期即派发执行，
并用 croniter 从 schedule_cron 计算下一次运行时间。
派发为独立 asyncio 任务（各自持有数据库会话），不阻塞扫描循环。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from app.config import settings

logger = logging.getLogger(__name__)

_TICK_INTERVAL_SECONDS = 30
_loop_task: asyncio.Task | None = None


def next_run_from_cron(cron_expr: str, base: datetime) -> datetime | None:
    """从 cron 表达式计算下一次运行时间；无效表达式返回 None"""
    try:
        from croniter import croniter

        return croniter(cron_expr, base).get_next(datetime)
    except Exception as e:
        logger.warning("无效 cron 表达式 %r: %s", cron_expr, e)
        return None


async def tick(session_factory) -> int:
    """扫描一轮到期任务并派发；返回派发数量"""
    from sqlalchemy import and_

    from app.models.sync import ApiConnection, FieldMapping, SyncTask

    now = datetime.now(timezone.utc)
    dispatched = 0
    async with session_factory() as session:
        stmt = select(SyncTask).where(
            SyncTask.task_type == "scheduled",
            SyncTask.status == "active",
            or_(SyncTask.next_run_at.is_(None), SyncTask.next_run_at <= now),
        )
        tasks = (await session.execute(stmt)).scalars().all()
        for task in tasks:
            cron_expr = (task.schedule_cron or "").strip()
            if not cron_expr:
                # 未配置 cron 的 scheduled 任务没有调度含义（仍可手动/Webhook 触发）：
                # 静默跳过——若走下方"无效 cron"分支，会每 5 分钟推迟+告警一次，
                # 只要任务存活就永久刷日志
                continue
            nxt = next_run_from_cron(cron_expr, now)
            if nxt is None:
                # 非空但无效：推迟 5 分钟避免紧密循环（多为配置笔误，保留告警便于排查）
                task.next_run_at = now + timedelta(minutes=5)
                await session.commit()
                logger.warning("任务 %s 的 cron 表达式无效，已推迟", task.id)
                continue
            # 先推进 next_run_at 再派发，防止重复调度
            task.next_run_at = nxt
            await session.commit()
            asyncio.create_task(_dispatch(str(task.id)))
            dispatched += 1
    return dispatched


async def _dispatch(task_id: str) -> None:
    """独立会话执行一次到期任务"""
    from app.database import async_session
    from app.models.sync import ApiConnection, FieldMapping, SyncTask
    from app.services.router import route_transfer

    try:
        async with async_session() as session:
            task = await session.get(SyncTask, __import__("uuid").UUID(task_id))
            if task is None or task.status != "active":
                return
            mapping = (await session.execute(
                select(FieldMapping).where(FieldMapping.id == task.mapping_id)
            )).scalar_one()
            source_conn = (await session.execute(
                select(ApiConnection).where(
                    ApiConnection.id == mapping.source_connection_id)
            )).scalar_one()
            target_conn = (await session.execute(
                select(ApiConnection).where(
                    ApiConnection.id == mapping.target_connection_id)
            )).scalar_one()
            await route_transfer(session, task, mapping, source_conn, target_conn)
    except Exception as e:
        logger.exception("定时任务 %s 执行失败: %s", task_id, e)


async def scheduler_loop() -> None:
    """调度主循环（lifespan 启动）"""
    from app.database import async_session
    from app.cache import lock

    logger.info("定时调度器已启动（间隔 %ss）", _TICK_INTERVAL_SECONDS)
    while True:
        try:
            try:
                token = await lock.acquire_lock('scheduler_tick', ttl=30)
            except Exception:
                # 保持单 worker 环境下原有 Redis 故障兼容性。
                logger.warning('调度锁不可用，按单 worker 模式扫描')
                await tick(async_session)
            else:
                if token:
                    try:
                        await tick(async_session)
                    finally:
                        await lock.release_lock('scheduler_tick', token)
        except Exception as e:
            logger.exception("调度器扫描失败: %s", e)
        # 空闲连接池回收（审查 P2）：此前 sweep_idle_pools 无任何生产调用点，
        # 连接改配即泄漏旧池（旧密码会话常驻）。随调度循环周期执行，失败不影响调度
        try:
            from app.connectors.database import sweep_idle_pools
            swept = await sweep_idle_pools()
            if swept:
                logger.info("已回收 %s 个空闲连接池", swept)
        except Exception as e:
            logger.warning("空闲连接池回收失败: %s", e)
        await asyncio.sleep(_TICK_INTERVAL_SECONDS)


def start() -> asyncio.Task:
    """启动调度器（幂等）"""
    global _loop_task
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(scheduler_loop())
    return _loop_task


async def stop() -> None:
    """停止调度器"""
    global _loop_task
    if _loop_task is not None:
        _loop_task.cancel()
        try:
            await _loop_task
        except (asyncio.CancelledError, Exception):
            pass
        _loop_task = None
