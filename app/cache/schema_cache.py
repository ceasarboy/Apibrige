"""表结构缓存（Redis 缓存 schema，TTL 1h，未命中查 DB 后回填）"""
import json
from app.redis_client import get_redis
from app.config import settings


async def get_schema(conn_id: str, table: str, fetch_func) -> list[dict]:
    """获取表结构（带缓存）

    Args:
        conn_id: 连接ID
        table: 表名
        fetch_func: 缓存未命中时的查询函数（async，返回 list[dict]）

    Returns:
        表结构列表
    """
    client = await get_redis()
    cache_key = f"schema:{conn_id}:{table}"

    # 1. 先查 Redis
    cached = await client.get(cache_key)
    if cached is not None:
        return json.loads(cached)

    # 2. 未命中查 DB
    schema = await fetch_func()

    # 3. 回填 Redis
    await client.set(cache_key, json.dumps(schema, ensure_ascii=False), ex=settings.schema_cache_ttl)
    return schema


async def invalidate_schema(conn_id: str, table: str):
    """使表结构缓存失效（表结构变更时调用）"""
    client = await get_redis()
    await client.delete(f"schema:{conn_id}:{table}")
