"""Celery 配置 + Worker 任务 + 补偿任务"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from celery import Celery
from sqlalchemy import select
from app.config import settings
from app.models.sync import DataBuffer

# Celery 应用
celery_app = Celery("apibridge", broker=settings.rabbitmq_url, backend="rpc://")
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    # 补偿任务定时调度
    beat_schedule={
        "recover-incremental-rounds": {
            "task": "app.celery_app.recover_incremental_rounds",
            "schedule": 60.0,
        },
        "compensate-pending": {
            "task": "app.celery_app.compensate_pending",
            "schedule": 60.0,  # 每60秒扫描一次
        },
    },
)


def _run_incremental(coroutine):
    # 每个 Celery 调用独立事件循环，结束时关闭循环绑定的连接池。
    async def run():
        try:
            return await coroutine
        finally:
            from app.database import close_engine
            from app.redis_client import close_redis
            from app.connectors.database import close_all_pools
            await close_all_pools()
            await close_redis()
            await close_engine()
    return asyncio.run(run())


@celery_app.task(name='app.celery_app.process_incremental_batch', acks_late=True, reject_on_worker_lost=True)
def process_incremental_batch(buffer_id):
    from app.services.incremental_rounds import process_batch
    return _run_incremental(process_batch(buffer_id))


@celery_app.task(name='app.celery_app.recover_incremental_rounds')
def recover_incremental_rounds():
    from app.services.incremental_rounds import recover
    return _run_incremental(recover())


@celery_app.task(name="app.celery_app.process_buffer")
def process_buffer(buffer_id: str):
    """Worker 消费任务：处理 data_buffer 中的记录"""
    from app.services.async_handler import process_async
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(process_async(buffer_id))
    finally:
        loop.close()


@celery_app.task(name="app.celery_app.compensate_pending")
def compensate_pending():
    """投递失败补偿：扫描 pending 超时记录重新投递"""
    from app.database import async_session

    async def _scan():
        async with async_session() as session:
            # 查找超过5分钟仍为 pending 的记录
            threshold = datetime.now(timezone.utc) - timedelta(minutes=5)
            result = await session.execute(
                select(DataBuffer).where(
                    DataBuffer.status == "pending",
                    DataBuffer.created_at < threshold
                )
            )
            stale_buffers = result.scalars().all()
            for buffer in stale_buffers:
                # 重新投递
                process_buffer.delay(str(buffer.id))
            return len(stale_buffers)

    loop = asyncio.new_event_loop()
    try:
        count = loop.run_until_complete(_scan())
        return f"compensated {count} pending buffers"
    finally:
        loop.close()
