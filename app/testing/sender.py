"""数据发送器 — 构造测试数据并调用源端 API/DB 进行数据发送"""
import time
import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class SendResult:
    """单次发送结果"""
    success: bool
    latency_ms: float
    request_data: Any
    response_data: Any = None
    error: str | None = None


@dataclass
class BatchSendResult:
    """批量发送结果汇总"""
    total: int = 0
    success: int = 0
    failed: int = 0
    results: list[SendResult] = field(default_factory=list)
    min_latency_ms: float = 0
    max_latency_ms: float = 0
    avg_latency_ms: float = 0

    def __post_init__(self):
        self._recalculate()

    def _recalculate(self):
        latencies = [r.latency_ms for r in self.results if r.success]
        if latencies:
            self.min_latency_ms = min(latencies)
            self.max_latency_ms = max(latencies)
            self.avg_latency_ms = sum(latencies) / len(latencies)

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total > 0 else 0


class DataSender:
    """数据发送器

    支持：
    - 单条/批量发送
    - 构造测试数据（模板 + 变量替换）
    - 并发控制
    """

    def __init__(self):
        self._counter = 0

    def generate_data(self, template: dict, count: int) -> list[dict]:
        """按模板生成测试数据

        支持变量：{{index}} {{timestamp}} {{uuid}}
        """
        import uuid as _uuid
        ts = str(int(time.time() * 1000))
        results = []
        for i in range(count):
            record = {}
            for key, value in template.items():
                if isinstance(value, str):
                    value = value.replace("{{index}}", str(i + self._counter))
                    value = value.replace("{{timestamp}}", ts)
                    value = value.replace("{{uuid}}", str(_uuid.uuid4()))
                record[key] = value
            results.append(record)
        self._counter += count
        return results

    async def send_single(self, fetch_fn: Callable, data: dict | None = None) -> SendResult:
        """发送单条数据"""
        start = time.time()
        try:
            if asyncio.iscoroutinefunction(fetch_fn):
                result = await fetch_fn(data) if data else await fetch_fn()
            else:
                result = fetch_fn(data) if data else fetch_fn()
            elapsed = (time.time() - start) * 1000
            return SendResult(success=True, latency_ms=elapsed, request_data=data, response_data=result)
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            return SendResult(success=False, latency_ms=elapsed, request_data=data, error=str(e))

    async def send_batch(self, fetch_fn: Callable, data_list: list[dict],
                         concurrency: int = 1) -> BatchSendResult:
        """批量发送数据（支持并发控制）"""
        semaphore = asyncio.Semaphore(concurrency)

        async def _send_one(data):
            async with semaphore:
                return await self.send_single(fetch_fn, data)

        tasks = [_send_one(d) for d in data_list]
        results = await asyncio.gather(*tasks)

        batch = BatchSendResult(total=len(results), results=list(results))
        batch.success = sum(1 for r in results if r.success)
        batch.failed = batch.total - batch.success

        latencies = [r.latency_ms for r in results if r.success]
        if latencies:
            batch.min_latency_ms = min(latencies)
            batch.max_latency_ms = max(latencies)
            batch.avg_latency_ms = sum(latencies) / len(latencies)

        return batch
