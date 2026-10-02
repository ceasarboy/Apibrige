"""指标刷新：让 Gauge 反映真实数量（design D1）

- **pull 时刷新**：在 `/metrics` 被抓取时刷新，Prometheus 抓取周期即刷新周期，
  不新增后台循环、不需要多 worker 锁。
- **隔离与降级**：任一数据源（平台库 / 连接池 / RabbitMQ）不可用时只记日志、
  不更新对应 Gauge，`/metrics` 仍返回 200——绝不因为刷新失败而让抓取整体失败。
- **诚实原则**：`apibridge_queue_depth` 在 MQ 不可达时**主动移除该序列**
  （宁可 no data，也不谎报 0）；缓冲积压由始终可用的 `apibridge_buffer_pending` 承担。

⚠ MQ 读取的两个关键约束（均系实测踩坑后固化）：
  1. 必须用 `aio_pika.connect`（**非 robust**）。`connect_robust` 在连接失败后会
     进入无限重连循环，`asyncio.wait_for` 无法终止它，会把 `/metrics` 拖到永不返回，
     进而挂死任何访问该端点的测试与 Prometheus 抓取。
  2. 失败后进入**冷却期**，避免每次抓取都重新等一遍超时。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy import func, select

logger = logging.getLogger(__name__)

# MQ 查询超时（无 MQ 环境下不能让 /metrics 挂住）
_MQ_TIMEOUT_SECONDS = 1.5
# 失败后的冷却时长：期间直接返回 None，不重复尝试连接
_MQ_FAIL_COOLDOWN_SECONDS = 60.0
# 不可用截止时间（time.monotonic）
_mq_unavailable_until = 0.0


# ============================================================
# 平台库指标
# ============================================================

async def refresh_buffer_pending(session) -> int:
    """data_buffer 中 pending / processing 的行数（跨环境始终可用）"""
    from app.models.sync import DataBuffer

    stmt = select(func.count(DataBuffer.id)).where(
        DataBuffer.status.in_(["pending", "processing"])
    )
    return int((await session.execute(stmt)).scalar() or 0)


async def refresh_active_tasks(session) -> int:
    """处于 active 状态的同步任务数"""
    from app.models.sync import SyncTask

    stmt = select(func.count(SyncTask.id)).where(SyncTask.status == "active")
    return int((await session.execute(stmt)).scalar() or 0)


async def refresh_active_connections(session) -> int:
    """处于 active 状态的连接数"""
    from app.models.sync import ApiConnection

    stmt = select(func.count(ApiConnection.id)).where(ApiConnection.status == "active")
    return int((await session.execute(stmt)).scalar() or 0)


# ============================================================
# 连接池指标
# ============================================================

def _pool_stats(db_type: str, pool: Any) -> tuple[int, int]:
    """返回 (当前连接数, 池上限)；驱动接口不同，取值失败返回 (0, 0)"""
    try:
        if db_type == "oracle":
            return int(getattr(pool, "opened", 0) or 0), int(getattr(pool, "max", 0) or 0)
        if db_type == "mysql":
            return int(getattr(pool, "size", 0) or 0), int(getattr(pool, "maxsize", 0) or 0)
        # asyncpg pool
        return int(pool.get_size()), int(pool.get_max_size())
    except Exception as e:  # 驱动差异导致的取值失败不影响其他指标
        logger.debug("读取 %s 连接池状态失败: %s", db_type, e)
        return 0, 0


def refresh_db_pools() -> tuple[int, int]:
    """汇总所有已注册连接池的 (当前连接数, 池上限)"""
    from app.connectors.database import _POOL_REGISTRY

    used = 0
    capacity = 0
    for (db_type, _fingerprint), pool in list(_POOL_REGISTRY.items()):
        u, c = _pool_stats(db_type, pool)
        used += u
        capacity += c
    return used, capacity


# ============================================================
# RabbitMQ 指标（不可达时不导出）
# ============================================================

async def refresh_queue_depth() -> int | None:
    """RabbitMQ 任务队列的就绪消息数；不可达返回 None（调用方移除该序列）

    使用 passive declare 读取，**不消费消息、不改变队列**。
    连接用非 robust 的一次性连接（见模块 docstring 的约束说明）。
    """
    global _mq_unavailable_until

    from app.config import settings

    if time.monotonic() < _mq_unavailable_until:
        return None  # 冷却期内不重试，避免拖慢每一次抓取

    conn: Any = None
    try:
        import aio_pika

        async def _read() -> int:
            nonlocal conn
            conn = await aio_pika.connect(settings.rabbitmq_url)
            channel = await conn.channel()
            queue = await channel.declare_queue(settings.task_queue_name,
                                                passive=True)
            return int(queue.declaration_result.message_count or 0)

        return await asyncio.wait_for(_read(), timeout=_MQ_TIMEOUT_SECONDS)
    except Exception as e:
        _mq_unavailable_until = time.monotonic() + _MQ_FAIL_COOLDOWN_SECONDS
        logger.debug("读取 RabbitMQ 队列深度失败（%.0fs 内不再重试）: %s",
                     _MQ_FAIL_COOLDOWN_SECONDS, e)
        return None
    finally:
        if conn is not None:
            try:
                await conn.close()
            except Exception:  # 关闭失败不影响结果
                pass


# ============================================================
# 统一刷新入口
# ============================================================

async def refresh_gauges() -> dict[str, Any]:
    """刷新全部 Gauge；返回各项结果（失败项记 None 并保留上一次值）

    单项失败不影响其余项；`/metrics` 端点调用此函数后无论如何都要正常返回。
    """
    from app.metrics_collector import (
        active_connections, active_tasks, buffer_pending,
        db_pool_connections, db_pool_size,
        mq_queue_depth as mq_depth_gauge,
        queue_depth as legacy_depth_gauge,
    )

    result: dict[str, Any] = {}

    # ---- 平台库 ----
    try:
        from app.database import async_session

        async with async_session() as session:
            result["buffer_pending"] = await refresh_buffer_pending(session)
            result["active_tasks"] = await refresh_active_tasks(session)
            result["active_connections"] = await refresh_active_connections(session)

        buffer_pending.set(result["buffer_pending"])
        active_tasks.set(result["active_tasks"])
        active_connections.set(result["active_connections"])
    except Exception as e:
        logger.warning("刷新平台库指标失败 (%s)", type(e).__name__)
        result["db_error"] = type(e).__name__

    # ---- 连接池（进程内，纯内存读取）----
    try:
        used, capacity = refresh_db_pools()
        db_pool_connections.set(used)
        db_pool_size.set(capacity)
        result["db_pool_connections"] = used
        result["db_pool_size"] = capacity
    except Exception as e:
        logger.warning("刷新连接池指标失败 (%s)", type(e).__name__)
        result["pool_error"] = type(e).__name__

    # ---- RabbitMQ（不可达时移除序列，不谎报 0）----
    # 同值双写：apibridge_mq_queue_depth{queue_name}（发行包新名）与
    # apibridge_queue_depth{queue_name}（原有指标名，保留给既有看板/告警）
    from app.config import settings

    depth = await refresh_queue_depth()
    result["queue_depth"] = depth
    queue_label = settings.task_queue_name
    if depth is None:
        for gauge in (mq_depth_gauge, legacy_depth_gauge):
            try:
                gauge.remove(queue_label)
            except KeyError:
                # prometheus_client 的 remove() 内部用 del 而非 pop：
                # 对**从未 set 过**的标签会抛 KeyError（无 MQ 环境首次刷新即命中）。
                # 该序列本就不存在，无需移除。
                pass
    else:
        mq_depth_gauge.labels(queue_name=queue_label).set(depth)
        legacy_depth_gauge.labels(queue_name=queue_label).set(depth)

    return result


_refresh_at = 0.0
_refresh_lock = asyncio.Lock()

async def refresh_cached():
    """Non-overlapping bounded scrape refresh; preserve old values on timeout."""
    global _refresh_at
    if _refresh_lock.locked() or time.monotonic() - _refresh_at < 30:
        return
    async with _refresh_lock:
        try:
            async with asyncio.timeout(2):
                await refresh_gauges()
        except Exception:
            logger.warning("指标刷新超时或不可用，保留已有值")
        finally:
            _refresh_at = time.monotonic()
