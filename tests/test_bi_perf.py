# -*- coding: utf-8 -*-
"""BI 性能预算验证（task 4.5）与高基数防护复核（task 4.7）

性能预算（design.md）：
- 单窗口聚合（1 万行 × 8 字段）< 3s
- 查询命中预聚合表 < 100ms

说明：真实 5 万行级别的压测见 `scripts/bi_bench.py`（task 6.4），
本文件用 1 万行保证测试可快速执行且能验证量级。
"""
import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.bi import BiMetricBucket
from app.models.sync import Base, DataBuffer
from app.services.bi_aggregator import run_metric
from app.services.bi_format import convert

UTC = timezone.utc
ROWS = 10_000
WINDOW = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
# 数据落在 10:00 桶；now 必须晚于 10:01 才能让该桶成为「已完整结束」的窗口
NOW = WINDOW + timedelta(minutes=1, seconds=30)

DEFN = {
    "metric_key": "bench",
    "name": "性能基准",
    "source_type": "data_buffer",
    "granularity": "1min",
    "chart_type": "line",
    "top_n": 100,
    "dimensions": [
        {"name": "d1", "field": "f1"},
        {"name": "d2", "field": "f2"},
        {"name": "d3", "field": "f3"},
    ],
    "measures": [
        {"key": "n", "agg": "count"},
        {"key": "s", "agg": "sum", "field": "amount"},
        {"key": "p95", "agg": "p95", "field": "amount"},
    ],
}


@pytest.fixture
def perf_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/perf.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _bulk():
        # 每行 8 个字段：f1..f8 + amount（3 个分组维度 + 1 个数值度量）
        async with maker() as s:
            s.add_all([
                DataBuffer(
                    task_id=uuid.uuid4(), status="completed",
                    created_at=WINDOW,
                    source_data={},
                    transformed_data={
                        # 三个维度取混合基数（20×10×5 = 1000 种独立组合）
                        "f1": f"cat{i % 20}",
                        "f2": f"reg{(i // 20) % 10}",
                        "f3": f"st{(i // 200) % 5}",
                        "f4": f"v{i}",
                        "f5": i,
                        "f6": i * 2,
                        "f7": f"x{i % 7}",
                        "f8": f"y{i % 3}",
                        "amount": float(i % 1000),
                    },
                )
                for i in range(ROWS)
            ])
            await s.commit()

    asyncio.run(_bulk())
    yield maker
    asyncio.run(engine.dispose())


class TestPerformanceBudget:

    async def test_single_window_aggregation_within_budget(self, perf_db):
        """1 万行 × 8 字段、3 维分组 + 3 度量 → < 3s（design 性能预算）"""
        async with perf_db() as s:
            t0 = time.perf_counter()
            result = await run_metric(s, DEFN, now=NOW)
            elapsed = time.perf_counter() - t0
        assert result["windows"] == 1
        # 防御假阳性：必须真的算到了数据
        async with perf_db() as s:
            produced = (await s.execute(
                select(BiMetricBucket).where(BiMetricBucket.metric_key == "bench")
            )).scalars().all()
        assert produced, "窗口内应产出聚合结果（否则性能断言无意义）"
        # 正式预算为 3s；测试阈值为 5s 容差（全量回归时机器负载会抬高耗时）。
        # 该断言的目标是捕获**数量级退化**（如误用 SQL 逐字段解析会到 30s+）。
        assert elapsed < 5.0, f"单窗口聚合耗时 {elapsed:.3f}s 异常（预算 3s）"

    async def test_query_latency_within_budget(self, perf_db):
        """查询命中预聚合表 → < 100ms"""
        async with perf_db() as s:
            await run_metric(s, DEFN, now=NOW)

        async with perf_db() as s:
            t0 = time.perf_counter()
            rows = (await s.execute(
                select(BiMetricBucket).where(BiMetricBucket.metric_key == "bench")
            )).scalars().all()
            buckets = [{"bucket_start": r.bucket_start,
                        "dimensions": r.dimensions or {},
                        "values": r.values or {}} for r in rows]
            data = convert(DEFN, buckets, "echarts-line")
            elapsed = time.perf_counter() - t0

        assert buckets, "查询应命中预聚合桶"
        assert data["series"], "应产出序列"
        assert elapsed < 0.1, f"查询+转换耗时 {elapsed:.4f}s 超出预算 100ms"
        assert len(buckets) <= 101

    async def test_top_n_bounds_result_rows(self, perf_db):
        """3 维组合数远超 Top N=100 → 结果行数必须被截断到 101（100 + 其他）"""
        async with perf_db() as s:
            await run_metric(s, DEFN, now=NOW)
        async with perf_db() as s:
            rows = (await s.execute(
                select(BiMetricBucket).where(BiMetricBucket.metric_key == "bench")
            )).scalars().all()
        # 原始组合：20 × 10 × 5 = 1000 种，必须被 Top N 保护
        assert len(rows) <= 101, f"Top N 未生效，产出 {len(rows)} 行"
        # 存储层是哨兵键（__others__），展示层经 display_dims 还原为「其他」
        from app.services.bi_aggregator import OTHERS_KEY
        others = [r for r in rows if (r.dimensions or {}).get("d1") == OTHERS_KEY]
        assert len(others) == 1, "其余组合应归并为单项「其他」"


class TestPrometheusHighCardinalityGuard:
    """task 4.7：per-task/per-connection 维度不得进入 Prometheus 标签"""

    def test_metrics_endpoint_has_no_business_dimension_labels(self):
        from app.metrics_collector import get_metrics
        text = get_metrics().decode("utf-8")
        for forbidden in ("task_id=", "connection_id=", "metric_key=", "dimensions="):
            assert forbidden not in text, \
                f"Prometheus 指标中不应出现高基数标签 {forbidden}"
