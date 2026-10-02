"""数据库连接器缓存策略：批量预取 + 写入缓冲"""
import asyncio
from typing import AsyncIterator, Callable, Awaitable
from app.config import settings


async def batch_fetch(
    fetch_page: Callable[[int, int], Awaitable[list]],
    batch_size: int = 1000,
) -> AsyncIterator[list]:
    """增量同步批量预取（分页拉取，避免内存溢出）

    Args:
        fetch_page: 分页查询函数 (offset, limit) -> list[dict]
        batch_size: 每批大小（默认 1000）

    Yields:
        每批数据列表
    """
    offset = 0
    while True:
        batch = await fetch_page(offset, batch_size)
        if not batch:
            break
        yield batch
        if len(batch) < batch_size:
            break
        offset += batch_size
