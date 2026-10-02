"""数据接收器 — 监听目标端，捕获同步到达的数据进行快照"""
import time
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class SnapshotRecord:
    """数据快照记录"""
    data: Any
    timestamp_ms: float
    source_tag: str = ""


@dataclass
class ReceiveResult:
    """接收结果"""
    success: bool
    snapshots: list[SnapshotRecord] = field(default_factory=list)
    total_count: int = 0
    timeout: bool = False
    error: str | None = None
    elapsed_ms: float = 0


class DataReceiver:
    """数据接收器

    支持：
    - 拉取模式：定时轮询目标端数据
    - 等待模式：等待目标端数据到达（超时检测）
    - 数据快照
    """

    async def poll(self, fetch_fn: Callable, max_retries: int = 3,
                   interval_ms: int = 500) -> ReceiveResult:
        """拉取模式：轮询获取目标端当前数据"""
        start = time.time()
        snapshots = []

        for attempt in range(max_retries):
            try:
                if asyncio.iscoroutinefunction(fetch_fn):
                    data = await fetch_fn()
                else:
                    data = fetch_fn()
                if data:
                    if isinstance(data, list):
                        for item in data:
                            snapshots.append(SnapshotRecord(
                                data=item,
                                timestamp_ms=(time.time() - start) * 1000,
                            ))
                    else:
                        snapshots.append(SnapshotRecord(
                            data=data,
                            timestamp_ms=(time.time() - start) * 1000,
                        ))
                if snapshots:
                    break
            except Exception as e:
                if attempt == max_retries - 1:
                    return ReceiveResult(
                        success=False, error=str(e),
                        elapsed_ms=(time.time() - start) * 1000,
                    )
            if attempt < max_retries - 1:
                await asyncio.sleep(interval_ms / 1000)

        return ReceiveResult(
            success=True,
            snapshots=snapshots,
            total_count=len(snapshots),
            elapsed_ms=(time.time() - start) * 1000,
        )

    async def wait_for_data(self, fetch_fn: Callable,
                            expected_count: int = 1,
                            timeout_ms: int = 30000,
                            poll_interval_ms: int = 500) -> ReceiveResult:
        """等待模式：等待目标端出现指定数量数据，支持超时"""
        start = time.time()
        snapshots = []
        last_count = 0

        while True:
            elapsed = (time.time() - start) * 1000
            if elapsed > timeout_ms:
                return ReceiveResult(
                    success=False, timeout=True, snapshots=snapshots,
                    total_count=len(snapshots), elapsed_ms=elapsed,
                    error=f"超时：等待 {timeout_ms}ms，收到 {len(snapshots)}/{expected_count} 条",
                )

            try:
                if asyncio.iscoroutinefunction(fetch_fn):
                    data = await fetch_fn()
                else:
                    data = fetch_fn()

                current_count = 0
                if data:
                    if isinstance(data, list):
                        for item in data:
                            snapshots.append(SnapshotRecord(
                                data=item, timestamp_ms=elapsed,
                            ))
                        current_count = len(data)
                    else:
                        snapshots.append(SnapshotRecord(
                            data=data, timestamp_ms=elapsed,
                        ))
                        current_count = 1

                if current_count >= expected_count and current_count != last_count:
                    break

                last_count = current_count
            except Exception:
                pass

            await asyncio.sleep(poll_interval_ms / 1000)

        return ReceiveResult(
            success=True,
            snapshots=snapshots,
            total_count=len(snapshots),
            elapsed_ms=elapsed,
        )
