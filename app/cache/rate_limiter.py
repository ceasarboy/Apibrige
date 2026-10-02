"""API 限流组件（滑动窗口）"""
import time
from app.redis_client import get_redis
from app.config import settings


async def rate_limit(key: str, limit: int | None = None, window: int = 1) -> bool:
    """滑动窗口限流

    Args:
        key: 限流标识（如目标API的 base_url）
        limit: 窗口内允许的请求数（默认用配置值）
        window: 窗口大小（秒，默认1秒）

    Returns:
        True 表示允许通过，False 表示被限流
    """
    if limit is None:
        limit = settings.rate_limit_default

    client = await get_redis()
    now = time.time()
    window_start = now - window
    redis_key = f"rate_limit:{key}"

    # Lua 脚本保证原子性：清除窗口外记录 + 计数 + 添加当前时间戳
    script = """
    local key = KEYS[1]
    local window_start = tonumber(ARGV[1])
    local now = tonumber(ARGV[2])
    local limit = tonumber(ARGV[3])

    -- 清除窗口外的记录
    redis.call('ZREMRANGEBYSCORE', key, 0, window_start)
    -- 当前计数
    local count = redis.call('ZCARD', key)
    if count < limit then
        redis.call('ZADD', key, now, now .. ':' .. math.random())
        redis.call('EXPIRE', key, 2)
        return 1
    else
        return 0
    end
    """
    result = await client.eval(script, 1, redis_key, window_start, now, limit)
    return result == 1
