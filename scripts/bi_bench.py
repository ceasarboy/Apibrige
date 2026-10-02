"""BI 聚合性能基准（task 6.4）

在设计性能预算量级（1 万 / 5 万行每桶 × 每行 5–10 字段）上实测单窗口聚合耗时，
并拆出「扫描」与「聚合计算」两段，便于定位瓶颈。

用法:
    python scripts/bi_bench.py            # 默认 10000 与 50000 两档
    BENCH_ROWS=50000 python scripts/bi_bench.py
"""
import asyncio
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models.sync import Base, DataBuffer  # noqa: E402
from app.services.bi_aggregator import (  # noqa: E402
    aggregate_rows, run_metric,
)
from app.services.bi_sources import load_resolvers, scan_window  # noqa: E402

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "bi_bench.db"
WINDOW = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
NOW = WINDOW + timedelta(minutes=1, seconds=30)

DEFN = {
    "metric_key": "bench",
    "name": "基准",
    "source_type": "data_buffer",
    "granularity": "1min",
    "chart_type": "line",
    "top_n": 100,
    # 3 个分组维度（用户上限）
    "dimensions": [
        {"name": "d1", "field": "f1"},
        {"name": "d2", "field": "f2"},
        {"name": "d3", "field": "f3"},
    ],
    # 3 个度量：count / sum / p95
    "measures": [
        {"key": "n", "agg": "count"},
        {"key": "amount_sum", "agg": "sum", "field": "amount"},
        {"key": "amount_p95", "agg": "p95", "field": "amount"},
    ],
}

BATCH = 5000
# 固定的占位 task_id（用含字母的 hex，避免 SQLite 将纯数字 hex 当作整数）
_TASK_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _row(i: int) -> dict:
    """每行 8 个字段（3 个维度 + 数值度量），维度取混合基数保证组合独立"""
    return {
        "f1": f"cat{i % 20}",
        "f2": f"reg{(i // 20) % 10}",
        "f3": f"st{(i // 200) % 5}",
        "f4": f"v{i}",
        "f5": i,
        "f6": i * 2,
        "f7": f"x{i % 7}",
        "f8": f"y{i % 3}",
        "amount": float(i % 1000),
    }


async def _seed(maker, rows: int) -> None:
    async with maker() as s:
        await s.execute(delete(DataBuffer))
        await s.commit()
    for start in range(0, rows, BATCH):
        batch = [(_row(i)) for i in range(start, min(start + BATCH, rows))]
        async with maker() as s:
            s.add_all([
                DataBuffer(task_id=_TASK_ID, source_data={},
                           transformed_data=d, status="completed", created_at=WINDOW)
                for d in batch
            ])
            await s.commit()


async def _bench(rows: int) -> dict:
    engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    t0 = time.perf_counter()
    await _seed(maker, rows)
    seed_s = time.perf_counter() - t0

    async with maker() as s:
        counted = (await s.execute(select(func.count(DataBuffer.id)))).scalar()

    # 拆段：扫描（含 JSON 解析）
    async with maker() as s:
        t0 = time.perf_counter()
        scanned = await scan_window(s, DEFN, WINDOW, NOW)
        resolvers = await load_resolvers(s, DEFN)
        scan_s = time.perf_counter() - t0

    # 拆段：分组 + 度量计算
    t0 = time.perf_counter()
    entries = aggregate_rows(scanned, DEFN, resolvers)
    compute_s = time.perf_counter() - t0

    # 端到端（扫描 + 计算 + 落库）
    # 注意：必须同时清空**水位线**，否则 run_metric 会认为窗口已算过而直接跳过
    # （曾因此得到 0.00s 的假结果）
    from app.models.bi import BiAggregationWatermark, BiMetricBucket

    async with maker() as s:
        await s.execute(delete(BiMetricBucket))
        await s.execute(delete(BiAggregationWatermark))
        await s.commit()
    async with maker() as s:
        t0 = time.perf_counter()
        result = await run_metric(s, DEFN, now=NOW)
        e2e_s = time.perf_counter() - t0

    await engine.dispose()
    return {
        "rows": rows, "counted": counted, "seed_s": seed_s,
        "scan_s": scan_s, "compute_s": compute_s, "e2e_s": e2e_s,
        "buckets": len(entries), "windows": result["windows"],
    }


async def main() -> int:
    sizes = [int(x) for x in os.environ.get("BENCH_ROWS", "10000,50000").split(",")]
    if DB_PATH.exists():
        DB_PATH.unlink()

    print(f"BI 聚合性能基准（3 维分组 + count/sum/p95 三度量，每行 8 字段）")
    print(f"{'行数':>8} {'扫描(含JSON)':>14} {'聚合计算':>10} {'端到端':>10} "
          f"{'结果行数':>9} {'行/秒(端到端)':>14}")
    print("-" * 74)
    ok = True
    for n in sizes:
        r = await _bench(n)
        # 防御假阳性：必须真的算出桶，否则耗时是无意义的
        if r["windows"] == 0 or r["buckets"] == 0:
            ok = False
            print(f"{r['counted']:>8}   ✗ 未产生任何窗口/桶（压测无效）")
            continue
        rate = r["counted"] / r["e2e_s"] if r["e2e_s"] else 0
        print(f"{r['counted']:>8} {r['scan_s']:>13.2f}s {r['compute_s']:>9.2f}s "
              f"{r['e2e_s']:>9.2f}s {r['buckets']:>9} {rate:>13.0f}")
        # 预算：单指标单窗口 < 3s（design.md）
        if r["e2e_s"] >= 3.0:
            ok = False
            print(f"         ⚠ 端到端 {r['e2e_s']:.2f}s 超出 3s 预算")
    print("-" * 74)
    print(f"预算：单指标单窗口 < 3s → {'达标' if ok else '未达标'}")
    if DB_PATH.exists():
        DB_PATH.unlink()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
