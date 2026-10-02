"""分布式锁组件（Redis SET NX EX + 锁释放）"""
import uuid
from app.redis_client import get_redis


async def acquire_lock(lock_name: str, ttl: int = 30) -> str | None:
    """获取分布式锁

    Args:
        lock_name: 锁名称
        ttl: 锁超时时间（秒），防止死锁

    Returns:
        锁令牌（成功）或 None（失败，表示已被占用）
    """
    token = str(uuid.uuid4())
    client = await get_redis()
    result = await client.set(f"lock:{lock_name}", token, nx=True, ex=ttl)
    return token if result else None


async def release_lock(lock_name: str, token: str) -> bool:
    """释放分布式锁（仅持有者可释放）

    Returns:
        True 表示成功释放，False 表示锁已过期或不属于当前持有者
    """
    client = await get_redis()
    key = f"lock:{lock_name}"
    # Lua 脚本保证原子性：检查令牌匹配后删除
    script = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
        return redis.call('del', KEYS[1])
    else
        return 0
    end
    """
    result = await client.eval(script, 1, key, token)
    return result == 1


async def refresh_lock(lock_name: str, token: str, ttl: int = 300) -> bool:
    """续期分布式锁（仅持有者可续期，Lua 保证检查+过期原子性）

    Returns:
        True 表示续期成功；False 表示锁已过期或不属于当前持有者
    """
    client = await get_redis()
    key = f"lock:{lock_name}"
    script = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
        return redis.call('expire', KEYS[1], ARGV[2])
    else
        return 0
    end
    """
    result = await client.eval(script, 1, key, token, ttl)
    return result == 1
