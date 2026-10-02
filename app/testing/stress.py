"""压测引擎 — 并发控制、梯度加压、持续时间、结果统计"""
import time
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class StressConfig:
    """压测配置"""
    target_concurrency: int = 10          # 目标并发数
    ramp_up_steps: int = 3               # 梯度加压步数
    ramp_up_interval_ms: int = 2000      # 每步间隔
    duration_ms: int = 10000             # 持续压测时间
    request_interval_ms: int = 0         # 请求间隔（0=无间隔）


@dataclass
class StressMetrics:
    """压测结果指标"""
    total_requests: int = 0
    successful: int = 0
    failed: int = 0
    min_latency_ms: float = 0
    max_latency_ms: float = 0
    avg_latency_ms: float = 0
    p50_latency_ms: float = 0
    p95_latency_ms: float = 0
    p99_latency_ms: float = 0
    qps: float = 0                     # 每秒请求数
    elapsed_ms: float = 0
    latencies: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.successful / self.total_requests if self.total_requests > 0 else 0

    def summary(self) -> str:
        return (
            f"总请求: {self.total_requests}, 成功: {self.successful}, 失败: {self.failed}, "
            f"成功率: {self.success_rate:.2%}, QPS: {self.qps:.1f}, "
            f"P50: {self.p50_latency_ms:.1f}ms, P95: {self.p95_latency_ms:.1f}ms, "
            f"P99: {self.p99_latency_ms:.1f}ms"
        )


class StressRunner:
    """压测运行器

    支持：
    - 梯度加压（ramp-up）
    - 固定并发持续压测
    - 完全统计（P50/P95/P99/QPS）
    """

    async def run(self, fetch_fn: Callable, config: StressConfig,
                  data_factory: Callable | None = None) -> StressMetrics:
        """执行压测

        Args:
            fetch_fn: 被测函数
            config: 压测配置
            data_factory: 测试数据工厂（每轮调用获取数据）

        Returns:
            StressMetrics 完整压测指标
        """
        metrics = StressMetrics()
        start = time.time()

        # 阶段1：梯度加压
        step_concurrency = max(1, config.target_concurrency // config.ramp_up_steps)
        current_concurrency = step_concurrency

        for _ in range(config.ramp_up_steps):
            await self._run_phase(fetch_fn, current_concurrency,
                                  config.ramp_up_interval_ms, metrics, data_factory)
            current_concurrency = min(current_concurrency + step_concurrency,
                                      config.target_concurrency)

        # 阶段2：目标并发持续压测
        deadline = time.time() + config.duration_ms / 1000
        while time.time() < deadline:
            remaining_ms = (deadline - time.time()) * 1000
            phase_duration = min(1000, remaining_ms)  # 每次最多1秒
            if phase_duration < 100:
                break
            await self._run_phase(fetch_fn, config.target_concurrency,
                                  phase_duration, metrics, data_factory)

        metrics.elapsed_ms = (time.time() - start) * 1000
        self._calculate_percentiles(metrics)

        if metrics.elapsed_ms > 0:
            metrics.qps = metrics.total_requests / (metrics.elapsed_ms / 1000)

        return metrics

    async def _run_phase(self, fetch_fn: Callable, concurrency: int,
                         duration_ms: float, metrics: StressMetrics,
                         data_factory: Callable | None = None):
        """单阶段运行"""
        deadline = time.time() + duration_ms / 1000
        semaphore = asyncio.Semaphore(concurrency)
        tasks = []

        async def worker():
            async with semaphore:
                start_t = time.time()
                data = data_factory() if data_factory else None
                try:
                    if asyncio.iscoroutinefunction(fetch_fn):
                        await fetch_fn(data) if data else await fetch_fn()
                    else:
                        fetch_fn(data) if data else fetch_fn()
                    elapsed = (time.time() - start_t) * 1000
                    metrics.successful += 1
                    metrics.latencies.append(elapsed)
                except Exception as e:
                    metrics.failed += 1
                    metrics.errors.append(str(e))
                finally:
                    metrics.total_requests += 1

        while time.time() < deadline:
            tasks.append(asyncio.create_task(worker()))
            # 控制请求间隔
            if len(tasks) >= concurrency * 2:  # 限制任务积累
                done, tasks = await asyncio.wait(tasks, timeout=0.01)
                tasks = list(tasks)

        if tasks:
            await asyncio.gather(*tasks)

    def _calculate_percentiles(self, metrics: StressMetrics):
        """计算百分位延迟"""
        latencies = sorted(metrics.latencies)
        if not latencies:
            return
        metrics.min_latency_ms = latencies[0]
        metrics.max_latency_ms = latencies[-1]
        metrics.avg_latency_ms = sum(latencies) / len(latencies)
        metrics.p50_latency_ms = self._percentile(latencies, 50)
        metrics.p95_latency_ms = self._percentile(latencies, 95)
        metrics.p99_latency_ms = self._percentile(latencies, 99)

    @staticmethod
    def _percentile(sorted_data: list[float], p: float) -> float:
        """计算百分位数"""
        if not sorted_data:
            return 0
        k = (len(sorted_data) - 1) * p / 100
        f = int(k)
        c = k - f
        if f + 1 < len(sorted_data):
            return sorted_data[f] + c * (sorted_data[f + 1] - sorted_data[f])
        return sorted_data[f]
