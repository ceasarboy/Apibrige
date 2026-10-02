"""Redis 连接封装（连接池、健康检查）"""
import redis.asyncio as redis
from app.config import settings

_pool: redis.ConnectionPool | None = None


async def get_redis() -> redis.Redis:
    """获取 Redis 客户端（连接池复用）"""
    global _pool
    if _pool is None:
        _pool = redis.ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=20,
        )
    return redis.Redis(connection_pool=_pool)


async def close_redis():
    """关闭 Redis 连接池"""
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


async def health_check() -> bool:
    """Redis 健康检查"""
    try:
        client = await get_redis()
        return await client.ping()
    except Exception:
        return False
