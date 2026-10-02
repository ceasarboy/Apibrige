"""幂等去重组件（数据哈希计算 + Redis Set 存储与查询）"""
import hashlib
import json
from app.redis_client import get_redis


def compute_hash(data: dict) -> str:
    """计算数据哈希（SHA256，用于幂等去重）

    default=str：DB 源（Oracle/MySQL/PG）的行值是驱动原生对象
    （DATE→datetime、NUMERIC→Decimal、BLOB→bytes），json.dumps 不带
    default 会直接 TypeError（审查 P1）。与 conflict.compute_record_hash 同口径。
    """
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


async def is_duplicate(task_id: str, data: dict) -> bool:
    """检查数据是否已成功处理过（只读 GET，不占位）

    Returns:
        True 表示重复（应跳过），False 表示首次（应处理）
    """
    data_hash = compute_hash(data)
    client = await get_redis()
    key = f"idempotency:{task_id}:{data_hash}"
    exists = await client.get(key)
    return exists is not None


async def mark_processed(task_id: str, data: dict) -> None:
    """推送成功后标记已处理（task：幂等键生命周期——失败不占位，重试可重跑）"""
    data_hash = compute_hash(data)
    client = await get_redis()
    key = f"idempotency:{task_id}:{data_hash}"
    await client.set(key, "1", nx=True, ex=86400)  # 保留24小时


async def clear_task(task_id: str):
    """清除任务的幂等记录（重试场景）"""
    client = await get_redis()
    async for key in client.scan_iter(f"idempotency:{task_id}:*"):
        await client.delete(key)
