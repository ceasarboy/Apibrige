"""RabbitMQ 连接封装（连接、通道、队列声明）"""
import aio_pika
from app.config import settings

_connection: aio_pika.RobustConnection | None = None
_channel: aio_pika.RobustChannel | None = None


async def get_channel() -> aio_pika.RobustChannel:
    """获取 RabbitMQ 通道（复用连接）"""
    global _connection, _channel
    if _connection is None or _connection.is_closed:
        _connection = await aio_pika.connect_robust(settings.rabbitmq_url)
        _channel = await _connection.channel()
        # 声明任务队列
        await _channel.declare_queue(settings.task_queue_name, durable=True)
    return _channel


async def get_queue() -> aio_pika.RobustQueue:
    """获取任务队列"""
    channel = await get_channel()
    return await channel.get_queue(settings.task_queue_name)


async def close_rabbitmq():
    """关闭 RabbitMQ 连接"""
    global _connection, _channel
    if _connection is not None and not _connection.is_closed:
        await _connection.close()
    _connection = None
    _channel = None


async def health_check() -> bool:
    """RabbitMQ 健康检查"""
    try:
        channel = await get_channel()
        return not channel.is_closed
    except Exception:
        return False
