# -*- coding: utf-8 -*-
"""BI 聚合引擎 阶段2 测试：数据源 / 度量算子 / 幂等 / 水位线 / 回填 / Top N

覆盖 openspec/changes/add-bi-aggregation-engine 的 tasks 2.1–2.10。
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.bi import BiAggregationWatermark, BiMetricBucket
from app.models.sync import (
    ApiConnection,
    Base,
    DataBuffer,
    ExecutionLog,
    FieldMapping,
    SyncTask,
)
from app.services.bi_aggregator import (
    OTHERS_KEY,
    OTHERS_LABEL,
    aggregate_rows,
    apply_computed,
    backfill,
    bucket_start_for,
    compute_measure,
    dim_value,
    estimate_cardinality,
    get_field,
    iter_windows,
    json_get,
    run_metric,
    windows_between,
    write_window,
    _match_filter,
    _rule_match,
)
from app.services.bi_sources import SourceError, load_resolvers, scan_window

UTC = timezone.utc


def _dt(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


# ============================================================
# Part A：时间工具
# ============================================================

class TestTimeUtils:

    def test_bucket_start_1min(self):
        assert bucket_start_for(_dt(2026, 9, 25, 10, 3, 42), "1min") == _dt(2026, 9, 25, 10, 3)

    def test_bucket_start_5min(self):
        assert bucket_start_for(_dt(2026, 9, 25, 10, 7, 5), "5min") == _dt(2026, 9, 25, 10, 5)

    def test_bucket_start_1h(self):
        assert bucket_start_for(_dt(2026, 9, 25, 10, 59, 59), "1h") == _dt(2026, 9, 25, 10, 0)

    def test_bucket_start_1d(self):
        assert bucket_start_for(_dt(2026, 9, 25, 23, 30), "1d") == _dt(2026, 9, 25)

    def test_naive_datetime_treated_as_utc(self):
        assert bucket_start_for(datetime(2026, 9, 25, 10, 3, 42), "1min") == _dt(2026, 9, 25, 10, 3)

    def test_iter_windows_without_watermark_starts_previous_complete(self):
        """无水位线：只算上一个完整窗口（当前桶仍在累积，不碰）"""
        now = _dt(2026, 9, 25, 10, 1, 30)
        windows = iter_windows(None, now, "1min")
        assert windows == [(_dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 1))]

    def test_iter_windows_continues_from_watermark(self):
        now = _dt(2026, 9, 25, 10, 5, 10)
        windows = iter_windows(_dt(2026, 9, 25, 10, 1), now, "1min")
        assert [w[0] for w in windows] == [
            _dt(2026, 9, 25, 10, 2), _dt(2026, 9, 25, 10, 3), _dt(2026, 9, 25, 10, 4)]

    def test_iter_windows_no_gap_and_no_overlap(self):
        now = _dt(2026, 9, 25, 11, 0)
        windows = iter_windows(_dt(2026, 9, 25, 10, 0), now, "5min", max_windows=100)
        for (s1, e1), (s2, e2) in zip(windows, windows[1:]):
            assert e1 == s2, "窗口必须首尾相接，不跳窗不重叠"

    def test_iter_windows_respects_max(self):
        now = _dt(2026, 9, 25, 12, 0)
        windows = iter_windows(_dt(2026, 9, 25, 10, 0), now, "1min", max_windows=10)
        assert len(windows) == 10

    def test_windows_between(self):
        ws = windows_between(_dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 3), "1min")
        assert len(ws) == 3
        assert ws[0][0] == _dt(2026, 9, 25, 10, 0)
        assert ws[-1][1] == _dt(2026, 9, 25, 10, 3)

    def test_windows_between_same_bucket_still_returns_it(self):
        """区间落在同一个粗粒度桶内时必须仍返回该桶

        回归：初版用 `cur < bucket_start_for(end)` 作终止条件，
        30 分钟区间 × 1h 粒度会得到 0 个窗口 → 回填静默不产生任何数据。
        """
        ws = windows_between(_dt(2026, 9, 25, 10, 10), _dt(2026, 9, 25, 10, 40), "1h")
        assert len(ws) == 1
        assert ws[0] == (_dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 11, 0))

    def test_windows_between_boundary_excludes_next_bucket(self):
        """end 正好落在桶边界时，不得把下一个桶算进来"""
        ws = windows_between(_dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 11, 0), "1h")
        assert len(ws) == 1
        assert ws[0][1] == _dt(2026, 9, 25, 11, 0)


# ============================================================
# Part B：取值与度量算子（纯函数）
# ============================================================

class TestFieldExtraction:

    def test_json_get_nested(self):
        assert json_get({"a": {"b": {"c": 7}}}, "a.b.c") == 7

    def test_json_get_missing_returns_none(self):
        assert json_get({"a": 1}, "a.b") is None
        assert json_get(None, "a") is None

    def test_get_field_prefers_top_level_column(self):
        row = {"status": "completed", "data": {"status": "other"}}
        assert get_field(row, "status") == "completed"

    def test_get_field_falls_back_to_json_data(self):
        row = {"status": "completed", "data": {"category": "A", "info": {"region": "华东"}}}
        assert get_field(row, "category") == "A"
        assert get_field(row, "info.region") == "华东"

    def test_get_field_unknown_returns_none(self):
        assert get_field({"data": {"a": 1}}, "nope") is None


class TestMeasureOperators:

    ROWS = [
        {"v": 1, "status": "completed"},
        {"v": 2, "status": "completed"},
        {"v": 3, "status": "failed"},
        {"v": 4, "status": "completed"},
        {"v": 5, "status": "completed"},
    ]

    def test_count(self):
        assert compute_measure(self.ROWS, {"key": "c", "agg": "count"}) == 5

    def test_filtered_count(self):
        m = {"key": "ok", "agg": "count", "filter": {"status": "completed"}}
        assert compute_measure(self.ROWS, m) == 4

    def test_sum_avg_min_max(self):
        assert compute_measure(self.ROWS, {"key": "s", "agg": "sum", "field": "v"}) == 15
        assert compute_measure(self.ROWS, {"key": "a", "agg": "avg", "field": "v"}) == 3
        assert compute_measure(self.ROWS, {"key": "mn", "agg": "min", "field": "v"}) == 1
        assert compute_measure(self.ROWS, {"key": "mx", "agg": "max", "field": "v"}) == 5

    def test_percentiles(self):
        assert compute_measure(self.ROWS, {"key": "p50", "agg": "p50", "field": "v"}) == 3
        assert compute_measure(self.ROWS, {"key": "p95", "agg": "p95", "field": "v"}) == pytest.approx(4.8)
        assert compute_measure(self.ROWS, {"key": "p99", "agg": "p99", "field": "v"}) == pytest.approx(4.96)

    def test_count_distinct(self):
        rows = [{"k": "a"}, {"k": "b"}, {"k": "a"}, {"k": None}]
        assert compute_measure(rows, {"key": "d", "agg": "count_distinct", "field": "k"}) == 2

    def test_numeric_agg_skips_non_numeric(self):
        """不可转数值的行被跳过，而非算成 0"""
        rows = [{"v": 2}, {"v": "abc"}, {"v": None}, {"v": "3"}]
        assert compute_measure(rows, {"key": "s", "agg": "sum", "field": "v"}) == 5

    def test_empty_input_returns_none_for_numeric(self):
        assert compute_measure([], {"key": "s", "agg": "sum", "field": "v"}) is None
        assert compute_measure([], {"key": "c", "agg": "count"}) == 0

    def test_numeric_string_coerced(self):
        assert compute_measure([{"v": "10"}], {"key": "s", "agg": "sum", "field": "v"}) == 10


class TestDimensionExtraction:

    def test_plain_field(self):
        row = {"data": {"category": "A"}}
        assert dim_value(row, {"name": "c", "field": "category"}, {}) == "A"

    def test_missing_becomes_unknown(self):
        assert dim_value({"data": {}}, {"name": "c", "field": "category"}, {}) == "未知"

    def test_error_normalization(self):
        row = {"error_message": "timeout after 30s"}
        assert dim_value(row, {"name": "e", "field": "error_message",
                               "normalize": "error_message"}, {}) == "timeout after <n>s"

    def test_resolver_lookup(self):
        resolvers = {"task_connection": {"t1": {"pair": "A → B"}}}
        row = {"task_id": "t1"}
        dim = {"name": "flow", "resolver": "task_connection", "field": "pair"}
        assert dim_value(row, dim, resolvers) == "A → B"

    def test_resolver_missing_task(self):
        dim = {"name": "flow", "resolver": "task_connection", "field": "pair"}
        assert dim_value({"task_id": "nope"}, dim, {"task_connection": {}}) == "未知"


# ============================================================
# Part C：分组聚合与 Top N（纯函数）
# ============================================================

class TestAggregateRows:

    DEF = {
        "metric_key": "m", "source_type": "execution_logs",
        "granularity": "1h", "chart_type": "bar", "top_n": 0,
        "dimensions": [{"name": "task", "field": "task_id"}],
        "measures": [{"key": "c", "agg": "count"}],
    }

    def test_groups_by_dimension(self):
        rows = [{"task_id": "a"}, {"task_id": "a"}, {"task_id": "b"}]
        out = aggregate_rows(rows, self.DEF)
        by = {e["dimensions"]["task"]: e["values"]["c"] for e in out}
        assert by == {"a": 2, "b": 1}

    def test_no_dimensions_yields_single_summary(self):
        defn = {**self.DEF, "dimensions": []}
        out = aggregate_rows([{"x": 1}, {"x": 2}], defn)
        assert len(out) == 1 and out[0]["dimensions"] == {}
        assert out[0]["values"]["c"] == 2

    def test_definition_level_filter_applied(self):
        defn = {**self.DEF, "dimensions": [], "filters": {"status": "failed"}}
        rows = [{"status": "failed"}, {"status": "success"}]
        assert aggregate_rows(rows, defn)[0]["values"]["c"] == 1

    def test_top_n_truncates_and_merges_others(self):
        defn = {**self.DEF, "top_n": 2,
                "measures": [{"key": "c", "agg": "count"}]}
        rows = [{"task_id": "a"}, {"task_id": "a"}, {"task_id": "a"},
                {"task_id": "b"}, {"task_id": "b"}, {"task_id": "c"}]
        out = aggregate_rows(rows, defn)
        assert len(out) == 3  # 2 kept + 1 others
        others = [e for e in out if e.get("is_others")]
        assert len(others) == 1
        assert others[0]["dimensions"]["task"] == OTHERS_KEY  # 存储哨兵；展示层经 display_dims 还原
        assert others[0]["values"]["c"] == 1  # c 的计数被归并
        # 总数守恒
        assert sum(e["values"]["c"] for e in out) == 6

    def test_top_n_no_truncation_when_under_limit(self):
        defn = {**self.DEF, "top_n": 10}
        out = aggregate_rows([{"task_id": "a"}, {"task_id": "b"}], defn)
        assert len(out) == 2
        assert not any(e.get("is_others") for e in out)

    def test_others_marks_non_additive_as_none(self):
        """分位数不可跨组合并 → 「其他」项应为 None（不编造）"""
        defn = {**self.DEF, "top_n": 1,
                "measures": [{"key": "c", "agg": "count"},
                             {"key": "p95", "agg": "p95", "field": "duration_ms"}]}
        rows = [{"task_id": "a", "duration_ms": 10},
                {"task_id": "b", "duration_ms": 20}]
        out = aggregate_rows(rows, defn)
        others = next(e for e in out if e.get("is_others"))
        assert others["values"]["c"] == 1
        assert others["values"]["p95"] is None

    def test_top_n_ordered_by_primary_measure_desc(self):
        defn = {**self.DEF, "top_n": 1}
        rows = [{"task_id": "small"}] + [{"task_id": "big"}] * 5
        out = aggregate_rows(rows, defn)
        assert out[0]["dimensions"]["task"] == "big"


# ============================================================
# Part D：数据库 —— 扫描 / 幂等 / 水位线 / 回填 / 解析器
# ============================================================

@pytest.fixture
def bi_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/agg.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    asyncio.run(engine.dispose())


async def _add_buffers(maker, items):
    """items: [(created_at, status, transformed_data)]"""
    async with maker() as s:
        for ts, status, data in items:
            s.add(DataBuffer(task_id=uuid.uuid4(), source_data=data,
                             transformed_data=data, status=status, created_at=ts))
        await s.commit()


async def _add_logs(maker, items):
    """items: [(created_at, status, duration_ms, error_message)]"""
    async with maker() as s:
        for ts, status, dur, err in items:
            s.add(ExecutionLog(task_id=uuid.uuid4(), action="push", status=status,
                               duration_ms=dur, error_message=err, created_at=ts))
        await s.commit()


class TestScanWindow:

    async def test_scan_data_buffer_window_bounded(self, bi_db):
        await _add_buffers(bi_db, [
            (_dt(2026, 9, 25, 10, 0, 10), "completed", {"category": "A"}),
            (_dt(2026, 9, 25, 10, 0, 50), "completed", {"category": "B"}),
            (_dt(2026, 9, 25, 10, 1, 10), "completed", {"category": "C"}),  # 窗口外
        ])
        defn = {"source_type": "data_buffer"}
        async with bi_db() as s:
            rows = await scan_window(s, defn, _dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 1))
        assert len(rows) == 2
        assert {r["data"]["category"] for r in rows} == {"A", "B"}

    async def test_scan_execution_logs_window(self, bi_db):
        await _add_logs(bi_db, [
            (_dt(2026, 9, 25, 10, 0, 5), "success", 100, None),
            (_dt(2026, 9, 25, 10, 0, 6), "failed", 200, "boom"),
        ])
        async with bi_db() as s:
            rows = await scan_window(s, {"source_type": "execution_logs"},
                                     _dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 1))
        assert len(rows) == 2
        assert rows[0]["status"] in ("success", "failed")

    async def test_scan_custom_sql_read_only(self, bi_db):
        defn = {"source_type": "custom_sql",
                "source_ref": "SELECT status, duration_ms FROM execution_logs "
                              "WHERE created_at >= :start AND created_at < :end"}
        await _add_logs(bi_db, [(_dt(2026, 9, 25, 10, 0, 5), "success", 100, None)])
        async with bi_db() as s:
            rows = await scan_window(s, defn, _dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 1))
        assert len(rows) == 1

    async def test_scan_custom_sql_rejects_write(self, bi_db):
        defn = {"source_type": "custom_sql", "source_ref": "DELETE FROM data_buffer"}
        async with bi_db() as s:
            with pytest.raises(SourceError):
                await scan_window(s, defn, _dt(2026, 9, 25), _dt(2026, 9, 26))

    async def test_scan_unknown_source_type(self, bi_db):
        async with bi_db() as s:
            with pytest.raises(SourceError):
                await scan_window(s, {"source_type": "wat"}, _dt(2026, 9, 25), _dt(2026, 9, 26))

    async def test_data_buffer_sharding_matches_whole(self, bi_db):
        """分片扫描与不分片结果一致（游标分片无重复无遗漏）"""
        await _add_buffers(bi_db, [
            (_dt(2026, 9, 25, 10, 0, i), "completed", {"n": i})
            for i in range(1, 11)])
        defn = {"source_type": "data_buffer"}
        win = (_dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 1))
        async with bi_db() as s:
            whole = await scan_window(s, defn, *win, shard_threshold=1000)
        async with bi_db() as s:
            sharded = await scan_window(s, defn, *win, shard_threshold=3)
        assert len(whole) == len(sharded) == 10
        assert {r["id"] for r in whole} == {r["id"] for r in sharded}
        assert sorted(r["data"]["n"] for r in sharded) == list(range(1, 11))

    async def test_execution_logs_sharding_matches_whole(self, bi_db):
        await _add_logs(bi_db, [
            (_dt(2026, 9, 25, 10, 0, i), "success", i, None)
            for i in range(1, 8)])
        defn = {"source_type": "execution_logs"}
        win = (_dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 1))
        async with bi_db() as s:
            whole = await scan_window(s, defn, *win, shard_threshold=1000)
        async with bi_db() as s:
            sharded = await scan_window(s, defn, *win, shard_threshold=2)
        assert len(whole) == len(sharded) == 7
        assert {r["id"] for r in whole} == {r["id"] for r in sharded}


class TestResolver:

    async def test_task_connection_resolver(self, bi_db):
        import uuid
        src_id, tgt_id, map_id, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        async with bi_db() as s:
            s.add(ApiConnection(id=src_id, name="MES库", connection_type="source",
                                protocol="oracle", db_type="oracle"))
            s.add(ApiConnection(id=tgt_id, name="明道云", connection_type="target",
                                protocol="hap"))
            s.add(FieldMapping(id=map_id, name="m", source_connection_id=src_id,
                               target_connection_id=tgt_id, mapping_config={}))
            s.add(SyncTask(id=task_id, mapping_id=map_id, task_type="manual"))
            await s.commit()

        defn = {"dimensions": [{"name": "flow", "resolver": "task_connection",
                                "field": "pair"}]}
        async with bi_db() as s:
            resolvers = await load_resolvers(s, defn)
        assert resolvers["task_connection"][str(task_id)]["pair"] == "MES库 → 明道云"


class TestWriteWindowIdempotency:

    async def test_same_window_twice_identical(self, bi_db):
        """同窗口算两次，结果与行数都不翻倍（幂等，D2）"""
        ts = _dt(2026, 9, 25, 10, 0)
        entries = [{"dimensions": {"task": "a"}, "values": {"c": 3}},
                   {"dimensions": {"task": "b"}, "values": {"c": 1}}]
        async with bi_db() as s:
            await write_window(s, "m", ts, entries)
        async with bi_db() as s:
            await write_window(s, "m", ts, entries)
        async with bi_db() as s:
            rows = (await s.execute(select(BiMetricBucket))).scalars().all()
        assert len(rows) == 2
        assert sum(r.values["c"] for r in rows) == 4  # 不是 8

    async def test_rewrite_removes_stale_dimension_rows(self, bi_db):
        """上轮存在的维度组合本轮消失时，旧桶必须被清掉（覆盖而非追加）"""
        ts = _dt(2026, 9, 25, 10, 0)
        async with bi_db() as s:
            await write_window(s, "m", ts, [
                {"dimensions": {"task": "a"}, "values": {"c": 1}},
                {"dimensions": {"task": "gone"}, "values": {"c": 9}},
            ])
        async with bi_db() as s:
            await write_window(s, "m", ts, [
                {"dimensions": {"task": "a"}, "values": {"c": 2}},
            ])
        async with bi_db() as s:
            rows = (await s.execute(select(BiMetricBucket))).scalars().all()
        assert len(rows) == 1 and rows[0].dimensions["task"] == "a"


class TestRunMetricAndWatermark:

    DEF = {
        "metric_key": "throughput", "source_type": "data_buffer",
        "granularity": "1min", "chart_type": "line", "top_n": 0,
        "dimensions": [], "measures": [{"key": "c", "agg": "count"}],
    }

    async def test_run_advances_watermark_and_writes_buckets(self, bi_db):
        """首次运行只算最近一个完整窗口；多窗口路径由回填验证"""
        await _add_buffers(bi_db, [
            (_dt(2026, 9, 25, 10, 0, 10), "completed", {}),
            (_dt(2026, 9, 25, 10, 0, 20), "completed", {}),
            (_dt(2026, 9, 25, 10, 1, 10), "completed", {}),
        ])
        # 无水位线：从上一个完整窗口（10:01）开始，不回填历史
        async with bi_db() as s:
            result = await run_metric(s, self.DEF, now=_dt(2026, 9, 25, 10, 2, 30))
        assert result["windows"] == 1

        async with bi_db() as s:
            wm = await s.get(BiAggregationWatermark, "throughput")
            assert bucket_start_for(wm.last_bucket, "1min") == _dt(2026, 9, 25, 10, 1)

        # 回填 10:00 窗口，验证多窗口写桶与取值
        async with bi_db() as s:
            await backfill(s, self.DEF, _dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 1))
        async with bi_db() as s:
            buckets = (await s.execute(
                select(BiMetricBucket).order_by(BiMetricBucket.bucket_start))
            ).scalars().all()
        assert [b.values["c"] for b in buckets] == [2, 1]

    async def test_rerun_is_idempotent(self, bi_db):
        """重复 run 不产生重复桶（水位线已推进，无新窗口）"""
        await _add_buffers(bi_db, [(_dt(2026, 9, 25, 10, 0, 10), "completed", {})])
        async with bi_db() as s:
            await run_metric(s, self.DEF, now=_dt(2026, 9, 25, 10, 1, 30))
        async with bi_db() as s:
            second = await run_metric(s, self.DEF, now=_dt(2026, 9, 25, 10, 1, 40))
        assert second["windows"] == 0
        async with bi_db() as s:
            n = (await s.execute(select(func.count(BiMetricBucket.id)))).scalar()
        assert n == 1

    async def test_resume_after_interruption_no_gap(self, bi_db):
        """中断后从水位线续跑，窗口连续不跳（D3）"""
        for minute in range(5):
            await _add_buffers(bi_db, [
                (_dt(2026, 9, 25, 10, minute, 10), "completed", {})])

        # 首次运行建立水位线在 10:00（只算最近一个完整窗口）
        async with bi_db() as s:
            await run_metric(s, self.DEF, now=_dt(2026, 9, 25, 10, 1, 30))

        # 时间推进到 10:05，先只补 1 个窗口（模拟中断）
        async with bi_db() as s:
            first = await run_metric(s, self.DEF, now=_dt(2026, 9, 25, 10, 5, 30),
                                     max_windows=1)
        assert first["windows"] == 1
        async with bi_db() as s:
            wm = await s.get(BiAggregationWatermark, self.DEF["metric_key"])
        assert bucket_start_for(wm.last_bucket, "1min") == _dt(2026, 9, 25, 10, 1)

        # 续跑补齐剩余窗口（10:02 / 10:03 / 10:04）
        async with bi_db() as s:
            rest = await run_metric(s, self.DEF, now=_dt(2026, 9, 25, 10, 5, 30))
        assert rest["windows"] == 3

        async with bi_db() as s:
            starts = sorted(b.bucket_start for b in (await s.execute(
                select(BiMetricBucket))).scalars().all())
        # 10:00 ~ 10:04 五窗口连续齐全
        assert len(starts) == 5
        for prev, nxt in zip(starts, starts[1:]):
            assert bucket_start_for(nxt, "1min") - bucket_start_for(prev, "1min") \
                == timedelta(minutes=1), "窗口必须连续，不跳窗"

    async def test_lagged_flag(self, bi_db):
        defn = {**self.DEF, "metric_key": "lag"}
        async with bi_db() as s:
            result = await run_metric(s, defn, now=_dt(2026, 9, 25, 12, 0),
                                      max_windows=2)
        # 无水位线时只从上个完整窗口开始，不会滞后
        assert result["lagged"] is False

    async def test_backfill_overwrites(self, bi_db):
        await _add_buffers(bi_db, [
            (_dt(2026, 9, 25, 10, 0, 10), "completed", {}),
            (_dt(2026, 9, 25, 10, 1, 10), "completed", {}),
        ])
        async with bi_db() as s:
            r1 = await backfill(s, self.DEF, _dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 2))
        assert r1["windows"] == 2
        # 再回填同一区间：覆盖而非追加
        async with bi_db() as s:
            await backfill(s, self.DEF, _dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 2))
        async with bi_db() as s:
            n = (await s.execute(select(func.count(BiMetricBucket.id)))).scalar()
        assert n == 2


class TestAggregationOnJsonData:

    async def test_json_dimension_grouping_end_to_end(self, bi_db):
        """data_buffer 中间数据按 JSON 维度分组（业务指标路径）"""
        await _add_buffers(bi_db, [
            (_dt(2026, 9, 25, 10, 0, 1), "completed", {"category": "A", "amount": 10}),
            (_dt(2026, 9, 25, 10, 0, 2), "completed", {"category": "A", "amount": 20}),
            (_dt(2026, 9, 25, 10, 0, 3), "completed", {"category": "B", "amount": 5}),
        ])
        defn = {
            "metric_key": "biz", "source_type": "data_buffer",
            "granularity": "1min", "chart_type": "pie", "top_n": 0,
            "dimensions": [{"name": "category", "field": "category"}],
            "measures": [{"key": "n", "agg": "count"},
                         {"key": "amt", "agg": "sum", "field": "amount"}],
        }
        async with bi_db() as s:
            await run_metric(s, defn, now=_dt(2026, 9, 25, 10, 1, 30))
        async with bi_db() as s:
            buckets = (await s.execute(select(BiMetricBucket))).scalars().all()
        by = {b.dimensions["category"]: b.values for b in buckets}
        assert by["A"] == {"n": 2, "amt": 30.0}
        assert by["B"] == {"n": 1, "amt": 5.0}

    async def test_cardinality_estimate(self, bi_db):
        await _add_buffers(bi_db, [
            (_dt(2026, 9, 25, 10, 0, 1), "completed", {"c": "x"}),
            (_dt(2026, 9, 25, 10, 0, 2), "completed", {"c": "y"}),
        ])
        defn = {"source_type": "data_buffer", "granularity": "1min",
                "dimensions": [{"name": "c", "field": "c"}]}
        async with bi_db() as s:
            est = await estimate_cardinality(s, defn, now=_dt(2026, 9, 25, 10, 1, 30))
        assert est["combinations"] == 2
        assert est["threshold_exceeded"] is False


# ============================================================
# 2026-09-25 筛选设置：类型感知的结构化规则（field + op + value + field_type）
# ============================================================

_ROW = {"data": {"人名": "巴神", "能力1": 5, "日期": "2026-01-01"}}


class TestFilterRules:

    def test_number_between(self):
        rule = {"field": "能力1", "op": "between", "value": [1, 5],
                "field_type": "number"}
        assert _match_filter(_ROW, [rule]) is True
        rule2 = {"field": "能力1", "op": "between", "value": [6, 9],
                 "field_type": "number"}
        assert _match_filter(_ROW, [rule2]) is False
        rule3 = {"field": "能力1", "op": "not_between", "value": [6, 9],
                 "field_type": "number"}
        assert _match_filter(_ROW, [rule3]) is True

    def test_time_between_string_order(self):
        rule = {"field": "日期", "op": "between",
                "value": ["2026-01-01", "2026-06-01"], "field_type": "time"}
        assert _match_filter(_ROW, [rule]) is True
        rule2 = {"field": "日期", "op": "between",
                 "value": ["2026-02-01", "2026-06-01"], "field_type": "time"}
        assert _match_filter(_ROW, [rule2]) is False

    def test_text_contains_and_eq(self):
        assert _match_filter(_ROW, [{"field": "人名", "op": "contains",
                                     "value": "巴"}]) is True
        assert _match_filter(_ROW, [{"field": "人名", "op": "not_contains",
                                     "value": "巴"}]) is False
        assert _match_filter(_ROW, [{"field": "人名", "op": "eq",
                                     "value": "巴神"}]) is True
        assert _match_filter(_ROW, [{"field": "人名", "op": "ne",
                                     "value": "巴神"}]) is False

    def test_in_and_not_in(self):
        rule = {"field": "人名", "op": "in", "value": ["巴神", "莉莉娅"]}
        assert _match_filter(_ROW, [rule]) is True
        rule2 = {"field": "人名", "op": "not_in", "value": ["巴神"]}
        assert _match_filter(_ROW, [rule2]) is False

    def test_empty_semantics(self):
        assert _match_filter({"data": {"备注": ""}},
                             [{"field": "备注", "op": "empty"}]) is True
        assert _match_filter(_ROW,
                             [{"field": "人名", "op": "not_empty"}]) is True

    def test_rules_combined_with_and(self):
        rules = [{"field": "人名", "op": "eq", "value": "巴神"},
                 {"field": "能力1", "op": "ge", "value": 9,
                  "field_type": "number"}]
        assert _match_filter(_ROW, rules) is False  # 5 < 9

    def test_legacy_dict_equality_still_works(self):
        assert _match_filter({"data": {"status": "failed"}},
                             {"status": "failed"}) is True
        assert _match_filter({"data": {"status": "ok"}},
                             {"status": "failed"}) is False

    def test_op_type_mismatch_defensive(self):
        # 文本字段配置区间：按字符串序比较兜底，不抛异常
        # （中文按 Unicode 排在 ASCII 之后，"巴神" > "z" → False）
        rule = {"field": "人名", "op": "between",
                "value": ["a", "z"], "field_type": "dimension"}
        assert _rule_match(_ROW, rule) is False
        rule2 = {"field": "人名", "op": "between",
                 "value": ["a", "龿"], "field_type": "dimension"}
        assert _rule_match(_ROW, rule2) is True


class TestComputedAndRatio:
    """派生列 / 聚合比 / 维度分组归一（add-bi-calc-fields）"""

    ROWS = [
        {"line": "A线", "passed": 90.0, "inspected": 100.0, "plan": 50.0, "actual": 45.0},
        {"line": "B线", "passed": 80.0, "inspected": 100.0, "plan": 50.0, "actual": 40.0},
        {"line": "C线", "passed": 95.0, "inspected": 100.0, "plan": 0.0, "actual": 0.0},
    ]

    def test_apply_computed_row_level(self):
        rows = [dict(r) for r in self.ROWS]
        apply_computed(rows, [
            {"key": "rate", "name": "合格率",
             "expr": "[passed] / [inspected] * 100", "field_type": "number"}])
        assert rows[0]["rate"] == pytest.approx(90.0)
        assert rows[2]["rate"] == pytest.approx(95.0)

    def test_apply_computed_missing_value_is_null(self):
        rows = [{"passed": None, "inspected": 100.0}]
        apply_computed(rows, [
            {"key": "rate", "expr": "[passed] / [inspected]", "field_type": "number"}])
        assert rows[0]["rate"] is None

    def test_apply_computed_topological_order(self):
        """派生列引用另一派生列：被引用者先算"""
        rows = [{"a": 2.0}]
        apply_computed(rows, [
            {"key": "second", "name": "二", "expr": "[first] * 10",
             "field_type": "number"},
            {"key": "first", "name": "一", "expr": "[a] + 1",
             "field_type": "number"}])
        assert rows[0]["second"] == 30.0

    def test_measure_over_computed(self):
        rows = apply_computed([dict(r) for r in self.ROWS],
                              [{"key": "rate", "expr": "[passed] / [inspected] * 100",
                                "field_type": "number"}])
        val = compute_measure(rows, {"key": "avg_rate", "agg": "avg", "field": "rate"})
        assert val == pytest.approx(88.333333, rel=1e-4)

    def test_ratio_measure(self):
        val = compute_measure(self.ROWS, {
            "key": "attain", "agg": "ratio",
            "numerator": {"field": "actual", "agg": "sum"},
            "denominator": {"field": "plan", "agg": "sum"},
            "scale": 100})
        assert val == pytest.approx(85.0)  # (45+40+0)/(50+50+0)*100

    def test_ratio_zero_denominator_is_null(self):
        rows = [{"a": 0.0, "b": 0.0}]
        val = compute_measure(rows, {"key": "r", "agg": "ratio",
                                     "numerator": {"field": "a", "agg": "sum"},
                                     "denominator": {"field": "b", "agg": "sum"}})
        assert val is None

    def test_dim_groups_normalization(self):
        dim = {"name": "车间", "field": "line",
               "groups": [{"name": "一车间", "values": ["A线", "B线"]}],
               "other": "其他"}
        assert dim_value({"line": "A线"}, dim, {}) == "一车间"
        assert dim_value({"line": "B线"}, dim, {}) == "一车间"
        assert dim_value({"line": "C线"}, dim, {}) == "其他"

    def test_dim_groups_without_other_keeps_value(self):
        dim = {"name": "车间", "field": "line",
               "groups": [{"name": "一车间", "values": ["A线"]}]}
        assert dim_value({"line": "D线"}, dim, {}) == "D线"

    def test_aggregate_rows_with_computed_and_groups(self):
        defn = {
            "computed": [{"key": "rate", "expr": "[passed] / [inspected] * 100",
                          "field_type": "number"}],
            "dimensions": [{"name": "车间", "field": "line",
                            "groups": [{"name": "主线", "values": ["A线", "B线"]}],
                            "other": "其他"}],
            "measures": [{"key": "avg_rate", "agg": "avg", "field": "rate"}],
            "top_n": 0,
        }
        entries = aggregate_rows([dict(r) for r in self.ROWS], defn)
        by_dim = {e["dimensions"]["车间"]: e["values"]["avg_rate"] for e in entries}
        assert by_dim["主线"] == pytest.approx(85.0)   # (90+80)/2
        assert by_dim["其他"] == pytest.approx(95.0)


class TestCaseInsensitiveKeys:
    """Oracle 大小写存量修复（add-bi-sql-pushdown）：键小写返回 vs 大写元数据"""

    def test_get_field_case_insensitive_top_level(self):
        row = {"work_date": "2026-09-27", "actual_qty": 42.0}
        assert get_field(row, "WORK_DATE") == "2026-09-27"
        assert get_field(row, "Actual_Qty") == 42.0

    def test_get_field_case_insensitive_in_data_wrapper(self):
        row = {"data": {"line": "A线"}}
        assert get_field(row, "LINE") == "A线"

    def test_json_path_still_wins(self):
        row = {"data": {"info": {"region": "华东"}}}
        assert get_field(row, "info.region") == "华东"
        # 点分路径按精确段匹配（大小写互认只覆盖顶层列——Oracle 场景所需；
        # buffer/logs 的嵌套键由写入方控制大小写）
        assert get_field(row, "info.REGION") is None

    def test_aggregate_rows_with_uppercase_metadata(self):
        rows = [{"data": {"line": "A线", "actual_qty": 10.0}},
                {"data": {"line": "A线", "actual_qty": 5.0}}]
        defn = {
            "dimensions": [{"name": "产线", "field": "LINE"}],
            "measures": [{"key": "产量", "agg": "sum", "field": "ACTUAL_QTY"}],
            "top_n": 0,
        }
        entries = aggregate_rows([dict(r) for r in rows], defn)
        assert entries[0]["dimensions"]["产线"] == "A线"
        assert entries[0]["values"]["产量"] == 15.0


class TestHeavyQueryHardening:
    """并发硬化：内存聚合经 to_thread（结果不变）"""

    def test_compute_window_uses_thread_and_semaphore(self):
        import asyncio as _asyncio
        from app.services import bi_aggregator as agg

        rows = [{"data": {"line": "A线", "qty": 3.0}}]
        defn = {"dimensions": [{"name": "产线", "field": "line"}],
                "measures": [{"key": "q", "agg": "sum", "field": "qty"}],
                "top_n": 0}

        async def _fake_scan(session, d, start, end):
            return rows

        async def _run():
            orig = agg.scan_window
            agg.scan_window = _fake_scan
            try:
                out = await agg.compute_window(None, defn,
                                               datetime(2026, 9, 27, tzinfo=UTC),
                                               datetime(2026, 9, 27, 1, tzinfo=UTC))
            finally:
                agg.scan_window = orig
            return out

        out = _asyncio.run(_run())
        assert out[0]["values"]["q"] == 3.0
# -*- coding: utf-8 -*-
"""BI 聚合引擎 阶段2 测试：数据源 / 度量算子 / 幂等 / 水位线 / 回填 / Top N

覆盖 openspec/changes/add-bi-aggregation-engine 的 tasks 2.1–2.10。
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.bi import BiAggregationWatermark, BiMetricBucket
from app.models.sync import (
    ApiConnection,
    Base,
    DataBuffer,
    ExecutionLog,
    FieldMapping,
    SyncTask,
)
from app.services.bi_aggregator import (
    OTHERS_KEY,
    OTHERS_LABEL,
    aggregate_rows,
    apply_computed,
    backfill,
    bucket_start_for,
    compute_measure,
    dim_value,
    estimate_cardinality,
    get_field,
    iter_windows,
    json_get,
    run_metric,
    windows_between,
    write_window,
    _match_filter,
    _rule_match,
)
from app.services.bi_sources import SourceError, load_resolvers, scan_window

UTC = timezone.utc


def _dt(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


# ============================================================
# Part A：时间工具
# ============================================================

class TestTimeUtils:

    def test_bucket_start_1min(self):
        assert bucket_start_for(_dt(2026, 9, 25, 10, 3, 42), "1min") == _dt(2026, 9, 25, 10, 3)

    def test_bucket_start_5min(self):
        assert bucket_start_for(_dt(2026, 9, 25, 10, 7, 5), "5min") == _dt(2026, 9, 25, 10, 5)

    def test_bucket_start_1h(self):
        assert bucket_start_for(_dt(2026, 9, 25, 10, 59, 59), "1h") == _dt(2026, 9, 25, 10, 0)

    def test_bucket_start_1d(self):
        assert bucket_start_for(_dt(2026, 9, 25, 23, 30), "1d") == _dt(2026, 9, 25)

    def test_naive_datetime_treated_as_utc(self):
        assert bucket_start_for(datetime(2026, 9, 25, 10, 3, 42), "1min") == _dt(2026, 9, 25, 10, 3)

    def test_iter_windows_without_watermark_starts_previous_complete(self):
        """无水位线：只算上一个完整窗口（当前桶仍在累积，不碰）"""
        now = _dt(2026, 9, 25, 10, 1, 30)
        windows = iter_windows(None, now, "1min")
        assert windows == [(_dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 1))]

    def test_iter_windows_continues_from_watermark(self):
        now = _dt(2026, 9, 25, 10, 5, 10)
        windows = iter_windows(_dt(2026, 9, 25, 10, 1), now, "1min")
        assert [w[0] for w in windows] == [
            _dt(2026, 9, 25, 10, 2), _dt(2026, 9, 25, 10, 3), _dt(2026, 9, 25, 10, 4)]

    def test_iter_windows_no_gap_and_no_overlap(self):
        now = _dt(2026, 9, 25, 11, 0)
        windows = iter_windows(_dt(2026, 9, 25, 10, 0), now, "5min", max_windows=100)
        for (s1, e1), (s2, e2) in zip(windows, windows[1:]):
            assert e1 == s2, "窗口必须首尾相接，不跳窗不重叠"

    def test_iter_windows_respects_max(self):
        now = _dt(2026, 9, 25, 12, 0)
        windows = iter_windows(_dt(2026, 9, 25, 10, 0), now, "1min", max_windows=10)
        assert len(windows) == 10

    def test_windows_between(self):
        ws = windows_between(_dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 3), "1min")
        assert len(ws) == 3
        assert ws[0][0] == _dt(2026, 9, 25, 10, 0)
        assert ws[-1][1] == _dt(2026, 9, 25, 10, 3)

    def test_windows_between_same_bucket_still_returns_it(self):
        """区间落在同一个粗粒度桶内时必须仍返回该桶

        回归：初版用 `cur < bucket_start_for(end)` 作终止条件，
        30 分钟区间 × 1h 粒度会得到 0 个窗口 → 回填静默不产生任何数据。
        """
        ws = windows_between(_dt(2026, 9, 25, 10, 10), _dt(2026, 9, 25, 10, 40), "1h")
        assert len(ws) == 1
        assert ws[0] == (_dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 11, 0))

    def test_windows_between_boundary_excludes_next_bucket(self):
        """end 正好落在桶边界时，不得把下一个桶算进来"""
        ws = windows_between(_dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 11, 0), "1h")
        assert len(ws) == 1
        assert ws[0][1] == _dt(2026, 9, 25, 11, 0)


# ============================================================
# Part B：取值与度量算子（纯函数）
# ============================================================

class TestFieldExtraction:

    def test_json_get_nested(self):
        assert json_get({"a": {"b": {"c": 7}}}, "a.b.c") == 7

    def test_json_get_missing_returns_none(self):
        assert json_get({"a": 1}, "a.b") is None
        assert json_get(None, "a") is None

    def test_get_field_prefers_top_level_column(self):
        row = {"status": "completed", "data": {"status": "other"}}
        assert get_field(row, "status") == "completed"

    def test_get_field_falls_back_to_json_data(self):
        row = {"status": "completed", "data": {"category": "A", "info": {"region": "华东"}}}
        assert get_field(row, "category") == "A"
        assert get_field(row, "info.region") == "华东"

    def test_get_field_unknown_returns_none(self):
        assert get_field({"data": {"a": 1}}, "nope") is None


class TestMeasureOperators:

    ROWS = [
        {"v": 1, "status": "completed"},
        {"v": 2, "status": "completed"},
        {"v": 3, "status": "failed"},
        {"v": 4, "status": "completed"},
        {"v": 5, "status": "completed"},
    ]

    def test_count(self):
        assert compute_measure(self.ROWS, {"key": "c", "agg": "count"}) == 5

    def test_filtered_count(self):
        m = {"key": "ok", "agg": "count", "filter": {"status": "completed"}}
        assert compute_measure(self.ROWS, m) == 4

    def test_sum_avg_min_max(self):
        assert compute_measure(self.ROWS, {"key": "s", "agg": "sum", "field": "v"}) == 15
        assert compute_measure(self.ROWS, {"key": "a", "agg": "avg", "field": "v"}) == 3
        assert compute_measure(self.ROWS, {"key": "mn", "agg": "min", "field": "v"}) == 1
        assert compute_measure(self.ROWS, {"key": "mx", "agg": "max", "field": "v"}) == 5

    def test_percentiles(self):
        assert compute_measure(self.ROWS, {"key": "p50", "agg": "p50", "field": "v"}) == 3
        assert compute_measure(self.ROWS, {"key": "p95", "agg": "p95", "field": "v"}) == pytest.approx(4.8)
        assert compute_measure(self.ROWS, {"key": "p99", "agg": "p99", "field": "v"}) == pytest.approx(4.96)

    def test_count_distinct(self):
        rows = [{"k": "a"}, {"k": "b"}, {"k": "a"}, {"k": None}]
        assert compute_measure(rows, {"key": "d", "agg": "count_distinct", "field": "k"}) == 2

    def test_numeric_agg_skips_non_numeric(self):
        """不可转数值的行被跳过，而非算成 0"""
        rows = [{"v": 2}, {"v": "abc"}, {"v": None}, {"v": "3"}]
        assert compute_measure(rows, {"key": "s", "agg": "sum", "field": "v"}) == 5

    def test_empty_input_returns_none_for_numeric(self):
        assert compute_measure([], {"key": "s", "agg": "sum", "field": "v"}) is None
        assert compute_measure([], {"key": "c", "agg": "count"}) == 0

    def test_numeric_string_coerced(self):
        assert compute_measure([{"v": "10"}], {"key": "s", "agg": "sum", "field": "v"}) == 10


class TestDimensionExtraction:

    def test_plain_field(self):
        row = {"data": {"category": "A"}}
        assert dim_value(row, {"name": "c", "field": "category"}, {}) == "A"

    def test_missing_becomes_unknown(self):
        assert dim_value({"data": {}}, {"name": "c", "field": "category"}, {}) == "未知"

    def test_error_normalization(self):
        row = {"error_message": "timeout after 30s"}
        assert dim_value(row, {"name": "e", "field": "error_message",
                               "normalize": "error_message"}, {}) == "timeout after <n>s"

    def test_resolver_lookup(self):
        resolvers = {"task_connection": {"t1": {"pair": "A → B"}}}
        row = {"task_id": "t1"}
        dim = {"name": "flow", "resolver": "task_connection", "field": "pair"}
        assert dim_value(row, dim, resolvers) == "A → B"

    def test_resolver_missing_task(self):
        dim = {"name": "flow", "resolver": "task_connection", "field": "pair"}
        assert dim_value({"task_id": "nope"}, dim, {"task_connection": {}}) == "未知"


# ============================================================
# Part C：分组聚合与 Top N（纯函数）
# ============================================================

class TestAggregateRows:

    DEF = {
        "metric_key": "m", "source_type": "execution_logs",
        "granularity": "1h", "chart_type": "bar", "top_n": 0,
        "dimensions": [{"name": "task", "field": "task_id"}],
        "measures": [{"key": "c", "agg": "count"}],
    }

    def test_groups_by_dimension(self):
        rows = [{"task_id": "a"}, {"task_id": "a"}, {"task_id": "b"}]
        out = aggregate_rows(rows, self.DEF)
        by = {e["dimensions"]["task"]: e["values"]["c"] for e in out}
        assert by == {"a": 2, "b": 1}

    def test_no_dimensions_yields_single_summary(self):
        defn = {**self.DEF, "dimensions": []}
        out = aggregate_rows([{"x": 1}, {"x": 2}], defn)
        assert len(out) == 1 and out[0]["dimensions"] == {}
        assert out[0]["values"]["c"] == 2

    def test_definition_level_filter_applied(self):
        defn = {**self.DEF, "dimensions": [], "filters": {"status": "failed"}}
        rows = [{"status": "failed"}, {"status": "success"}]
        assert aggregate_rows(rows, defn)[0]["values"]["c"] == 1

    def test_top_n_truncates_and_merges_others(self):
        defn = {**self.DEF, "top_n": 2,
                "measures": [{"key": "c", "agg": "count"}]}
        rows = [{"task_id": "a"}, {"task_id": "a"}, {"task_id": "a"},
                {"task_id": "b"}, {"task_id": "b"}, {"task_id": "c"}]
        out = aggregate_rows(rows, defn)
        assert len(out) == 3  # 2 kept + 1 others
        others = [e for e in out if e.get("is_others")]
        assert len(others) == 1
        assert others[0]["dimensions"]["task"] == OTHERS_KEY  # 存储哨兵；展示层经 display_dims 还原
        assert others[0]["values"]["c"] == 1  # c 的计数被归并
        # 总数守恒
        assert sum(e["values"]["c"] for e in out) == 6

    def test_top_n_no_truncation_when_under_limit(self):
        defn = {**self.DEF, "top_n": 10}
        out = aggregate_rows([{"task_id": "a"}, {"task_id": "b"}], defn)
        assert len(out) == 2
        assert not any(e.get("is_others") for e in out)

    def test_others_marks_non_additive_as_none(self):
        """分位数不可跨组合并 → 「其他」项应为 None（不编造）"""
        defn = {**self.DEF, "top_n": 1,
                "measures": [{"key": "c", "agg": "count"},
                             {"key": "p95", "agg": "p95", "field": "duration_ms"}]}
        rows = [{"task_id": "a", "duration_ms": 10},
                {"task_id": "b", "duration_ms": 20}]
        out = aggregate_rows(rows, defn)
        others = next(e for e in out if e.get("is_others"))
        assert others["values"]["c"] == 1
        assert others["values"]["p95"] is None

    def test_top_n_ordered_by_primary_measure_desc(self):
        defn = {**self.DEF, "top_n": 1}
        rows = [{"task_id": "small"}] + [{"task_id": "big"}] * 5
        out = aggregate_rows(rows, defn)
        assert out[0]["dimensions"]["task"] == "big"


# ============================================================
# Part D：数据库 —— 扫描 / 幂等 / 水位线 / 回填 / 解析器
# ============================================================

@pytest.fixture
def bi_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/agg.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    asyncio.run(engine.dispose())


async def _add_buffers(maker, items):
    """items: [(created_at, status, transformed_data)]"""
    async with maker() as s:
        for ts, status, data in items:
            s.add(DataBuffer(task_id=uuid.uuid4(), source_data=data,
                             transformed_data=data, status=status, created_at=ts))
        await s.commit()


async def _add_logs(maker, items):
    """items: [(created_at, status, duration_ms, error_message)]"""
    async with maker() as s:
        for ts, status, dur, err in items:
            s.add(ExecutionLog(task_id=uuid.uuid4(), action="push", status=status,
                               duration_ms=dur, error_message=err, created_at=ts))
        await s.commit()


class TestScanWindow:

    async def test_scan_data_buffer_window_bounded(self, bi_db):
        await _add_buffers(bi_db, [
            (_dt(2026, 9, 25, 10, 0, 10), "completed", {"category": "A"}),
            (_dt(2026, 9, 25, 10, 0, 50), "completed", {"category": "B"}),
            (_dt(2026, 9, 25, 10, 1, 10), "completed", {"category": "C"}),  # 窗口外
        ])
        defn = {"source_type": "data_buffer"}
        async with bi_db() as s:
            rows = await scan_window(s, defn, _dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 1))
        assert len(rows) == 2
        assert {r["data"]["category"] for r in rows} == {"A", "B"}

    async def test_scan_execution_logs_window(self, bi_db):
        await _add_logs(bi_db, [
            (_dt(2026, 9, 25, 10, 0, 5), "success", 100, None),
            (_dt(2026, 9, 25, 10, 0, 6), "failed", 200, "boom"),
        ])
        async with bi_db() as s:
            rows = await scan_window(s, {"source_type": "execution_logs"},
                                     _dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 1))
        assert len(rows) == 2
        assert rows[0]["status"] in ("success", "failed")

    async def test_scan_custom_sql_read_only(self, bi_db):
        defn = {"source_type": "custom_sql",
                "source_ref": "SELECT status, duration_ms FROM execution_logs "
                              "WHERE created_at >= :start AND created_at < :end"}
        await _add_logs(bi_db, [(_dt(2026, 9, 25, 10, 0, 5), "success", 100, None)])
        async with bi_db() as s:
            rows = await scan_window(s, defn, _dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 1))
        assert len(rows) == 1

    async def test_scan_custom_sql_rejects_write(self, bi_db):
        defn = {"source_type": "custom_sql", "source_ref": "DELETE FROM data_buffer"}
        async with bi_db() as s:
            with pytest.raises(SourceError):
                await scan_window(s, defn, _dt(2026, 9, 25), _dt(2026, 9, 26))

    async def test_scan_unknown_source_type(self, bi_db):
        async with bi_db() as s:
            with pytest.raises(SourceError):
                await scan_window(s, {"source_type": "wat"}, _dt(2026, 9, 25), _dt(2026, 9, 26))

    async def test_data_buffer_sharding_matches_whole(self, bi_db):
        """分片扫描与不分片结果一致（游标分片无重复无遗漏）"""
        await _add_buffers(bi_db, [
            (_dt(2026, 9, 25, 10, 0, i), "completed", {"n": i})
            for i in range(1, 11)])
        defn = {"source_type": "data_buffer"}
        win = (_dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 1))
        async with bi_db() as s:
            whole = await scan_window(s, defn, *win, shard_threshold=1000)
        async with bi_db() as s:
            sharded = await scan_window(s, defn, *win, shard_threshold=3)
        assert len(whole) == len(sharded) == 10
        assert {r["id"] for r in whole} == {r["id"] for r in sharded}
        assert sorted(r["data"]["n"] for r in sharded) == list(range(1, 11))

    async def test_execution_logs_sharding_matches_whole(self, bi_db):
        await _add_logs(bi_db, [
            (_dt(2026, 9, 25, 10, 0, i), "success", i, None)
            for i in range(1, 8)])
        defn = {"source_type": "execution_logs"}
        win = (_dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 1))
        async with bi_db() as s:
            whole = await scan_window(s, defn, *win, shard_threshold=1000)
        async with bi_db() as s:
            sharded = await scan_window(s, defn, *win, shard_threshold=2)
        assert len(whole) == len(sharded) == 7
        assert {r["id"] for r in whole} == {r["id"] for r in sharded}


class TestResolver:

    async def test_task_connection_resolver(self, bi_db):
        import uuid
        src_id, tgt_id, map_id, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        async with bi_db() as s:
            s.add(ApiConnection(id=src_id, name="MES库", connection_type="source",
                                protocol="oracle", db_type="oracle"))
            s.add(ApiConnection(id=tgt_id, name="明道云", connection_type="target",
                                protocol="hap"))
            s.add(FieldMapping(id=map_id, name="m", source_connection_id=src_id,
                               target_connection_id=tgt_id, mapping_config={}))
            s.add(SyncTask(id=task_id, mapping_id=map_id, task_type="manual"))
            await s.commit()

        defn = {"dimensions": [{"name": "flow", "resolver": "task_connection",
                                "field": "pair"}]}
        async with bi_db() as s:
            resolvers = await load_resolvers(s, defn)
        assert resolvers["task_connection"][str(task_id)]["pair"] == "MES库 → 明道云"


class TestWriteWindowIdempotency:

    async def test_same_window_twice_identical(self, bi_db):
        """同窗口算两次，结果与行数都不翻倍（幂等，D2）"""
        ts = _dt(2026, 9, 25, 10, 0)
        entries = [{"dimensions": {"task": "a"}, "values": {"c": 3}},
                   {"dimensions": {"task": "b"}, "values": {"c": 1}}]
        async with bi_db() as s:
            await write_window(s, "m", ts, entries)
        async with bi_db() as s:
            await write_window(s, "m", ts, entries)
        async with bi_db() as s:
            rows = (await s.execute(select(BiMetricBucket))).scalars().all()
        assert len(rows) == 2
        assert sum(r.values["c"] for r in rows) == 4  # 不是 8

    async def test_rewrite_removes_stale_dimension_rows(self, bi_db):
        """上轮存在的维度组合本轮消失时，旧桶必须被清掉（覆盖而非追加）"""
        ts = _dt(2026, 9, 25, 10, 0)
        async with bi_db() as s:
            await write_window(s, "m", ts, [
                {"dimensions": {"task": "a"}, "values": {"c": 1}},
                {"dimensions": {"task": "gone"}, "values": {"c": 9}},
            ])
        async with bi_db() as s:
            await write_window(s, "m", ts, [
                {"dimensions": {"task": "a"}, "values": {"c": 2}},
            ])
        async with bi_db() as s:
            rows = (await s.execute(select(BiMetricBucket))).scalars().all()
        assert len(rows) == 1 and rows[0].dimensions["task"] == "a"


class TestRunMetricAndWatermark:

    DEF = {
        "metric_key": "throughput", "source_type": "data_buffer",
        "granularity": "1min", "chart_type": "line", "top_n": 0,
        "dimensions": [], "measures": [{"key": "c", "agg": "count"}],
    }

    async def test_run_advances_watermark_and_writes_buckets(self, bi_db):
        """首次运行只算最近一个完整窗口；多窗口路径由回填验证"""
        await _add_buffers(bi_db, [
            (_dt(2026, 9, 25, 10, 0, 10), "completed", {}),
            (_dt(2026, 9, 25, 10, 0, 20), "completed", {}),
            (_dt(2026, 9, 25, 10, 1, 10), "completed", {}),
        ])
        # 无水位线：从上一个完整窗口（10:01）开始，不回填历史
        async with bi_db() as s:
            result = await run_metric(s, self.DEF, now=_dt(2026, 9, 25, 10, 2, 30))
        assert result["windows"] == 1

        async with bi_db() as s:
            wm = await s.get(BiAggregationWatermark, "throughput")
            assert bucket_start_for(wm.last_bucket, "1min") == _dt(2026, 9, 25, 10, 1)

        # 回填 10:00 窗口，验证多窗口写桶与取值
        async with bi_db() as s:
            await backfill(s, self.DEF, _dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 1))
        async with bi_db() as s:
            buckets = (await s.execute(
                select(BiMetricBucket).order_by(BiMetricBucket.bucket_start))
            ).scalars().all()
        assert [b.values["c"] for b in buckets] == [2, 1]

    async def test_rerun_is_idempotent(self, bi_db):
        """重复 run 不产生重复桶（水位线已推进，无新窗口）"""
        await _add_buffers(bi_db, [(_dt(2026, 9, 25, 10, 0, 10), "completed", {})])
        async with bi_db() as s:
            await run_metric(s, self.DEF, now=_dt(2026, 9, 25, 10, 1, 30))
        async with bi_db() as s:
            second = await run_metric(s, self.DEF, now=_dt(2026, 9, 25, 10, 1, 40))
        assert second["windows"] == 0
        async with bi_db() as s:
            n = (await s.execute(select(func.count(BiMetricBucket.id)))).scalar()
        assert n == 1

    async def test_resume_after_interruption_no_gap(self, bi_db):
        """中断后从水位线续跑，窗口连续不跳（D3）"""
        for minute in range(5):
            await _add_buffers(bi_db, [
                (_dt(2026, 9, 25, 10, minute, 10), "completed", {})])

        # 首次运行建立水位线在 10:00（只算最近一个完整窗口）
        async with bi_db() as s:
            await run_metric(s, self.DEF, now=_dt(2026, 9, 25, 10, 1, 30))

        # 时间推进到 10:05，先只补 1 个窗口（模拟中断）
        async with bi_db() as s:
            first = await run_metric(s, self.DEF, now=_dt(2026, 9, 25, 10, 5, 30),
                                     max_windows=1)
        assert first["windows"] == 1
        async with bi_db() as s:
            wm = await s.get(BiAggregationWatermark, self.DEF["metric_key"])
        assert bucket_start_for(wm.last_bucket, "1min") == _dt(2026, 9, 25, 10, 1)

        # 续跑补齐剩余窗口（10:02 / 10:03 / 10:04）
        async with bi_db() as s:
            rest = await run_metric(s, self.DEF, now=_dt(2026, 9, 25, 10, 5, 30))
        assert rest["windows"] == 3

        async with bi_db() as s:
            starts = sorted(b.bucket_start for b in (await s.execute(
                select(BiMetricBucket))).scalars().all())
        # 10:00 ~ 10:04 五窗口连续齐全
        assert len(starts) == 5
        for prev, nxt in zip(starts, starts[1:]):
            assert bucket_start_for(nxt, "1min") - bucket_start_for(prev, "1min") \
                == timedelta(minutes=1), "窗口必须连续，不跳窗"

    async def test_lagged_flag(self, bi_db):
        defn = {**self.DEF, "metric_key": "lag"}
        async with bi_db() as s:
            result = await run_metric(s, defn, now=_dt(2026, 9, 25, 12, 0),
                                      max_windows=2)
        # 无水位线时只从上个完整窗口开始，不会滞后
        assert result["lagged"] is False

    async def test_backfill_overwrites(self, bi_db):
        await _add_buffers(bi_db, [
            (_dt(2026, 9, 25, 10, 0, 10), "completed", {}),
            (_dt(2026, 9, 25, 10, 1, 10), "completed", {}),
        ])
        async with bi_db() as s:
            r1 = await backfill(s, self.DEF, _dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 2))
        assert r1["windows"] == 2
        # 再回填同一区间：覆盖而非追加
        async with bi_db() as s:
            await backfill(s, self.DEF, _dt(2026, 9, 25, 10, 0), _dt(2026, 9, 25, 10, 2))
        async with bi_db() as s:
            n = (await s.execute(select(func.count(BiMetricBucket.id)))).scalar()
        assert n == 2


class TestAggregationOnJsonData:

    async def test_json_dimension_grouping_end_to_end(self, bi_db):
        """data_buffer 中间数据按 JSON 维度分组（业务指标路径）"""
        await _add_buffers(bi_db, [
            (_dt(2026, 9, 25, 10, 0, 1), "completed", {"category": "A", "amount": 10}),
            (_dt(2026, 9, 25, 10, 0, 2), "completed", {"category": "A", "amount": 20}),
            (_dt(2026, 9, 25, 10, 0, 3), "completed", {"category": "B", "amount": 5}),
        ])
        defn = {
            "metric_key": "biz", "source_type": "data_buffer",
            "granularity": "1min", "chart_type": "pie", "top_n": 0,
            "dimensions": [{"name": "category", "field": "category"}],
            "measures": [{"key": "n", "agg": "count"},
                         {"key": "amt", "agg": "sum", "field": "amount"}],
        }
        async with bi_db() as s:
            await run_metric(s, defn, now=_dt(2026, 9, 25, 10, 1, 30))
        async with bi_db() as s:
            buckets = (await s.execute(select(BiMetricBucket))).scalars().all()
        by = {b.dimensions["category"]: b.values for b in buckets}
        assert by["A"] == {"n": 2, "amt": 30.0}
        assert by["B"] == {"n": 1, "amt": 5.0}

    async def test_cardinality_estimate(self, bi_db):
        await _add_buffers(bi_db, [
            (_dt(2026, 9, 25, 10, 0, 1), "completed", {"c": "x"}),
            (_dt(2026, 9, 25, 10, 0, 2), "completed", {"c": "y"}),
        ])
        defn = {"source_type": "data_buffer", "granularity": "1min",
                "dimensions": [{"name": "c", "field": "c"}]}
        async with bi_db() as s:
            est = await estimate_cardinality(s, defn, now=_dt(2026, 9, 25, 10, 1, 30))
        assert est["combinations"] == 2
        assert est["threshold_exceeded"] is False


# ============================================================
# 2026-09-25 筛选设置：类型感知的结构化规则（field + op + value + field_type）
# ============================================================

_ROW = {"data": {"人名": "巴神", "能力1": 5, "日期": "2026-01-01"}}


class TestFilterRules:

    def test_number_between(self):
        rule = {"field": "能力1", "op": "between", "value": [1, 5],
                "field_type": "number"}
        assert _match_filter(_ROW, [rule]) is True
        rule2 = {"field": "能力1", "op": "between", "value": [6, 9],
                 "field_type": "number"}
        assert _match_filter(_ROW, [rule2]) is False
        rule3 = {"field": "能力1", "op": "not_between", "value": [6, 9],
                 "field_type": "number"}
        assert _match_filter(_ROW, [rule3]) is True

    def test_time_between_string_order(self):
        rule = {"field": "日期", "op": "between",
                "value": ["2026-01-01", "2026-06-01"], "field_type": "time"}
        assert _match_filter(_ROW, [rule]) is True
        rule2 = {"field": "日期", "op": "between",
                 "value": ["2026-02-01", "2026-06-01"], "field_type": "time"}
        assert _match_filter(_ROW, [rule2]) is False

    def test_text_contains_and_eq(self):
        assert _match_filter(_ROW, [{"field": "人名", "op": "contains",
                                     "value": "巴"}]) is True
        assert _match_filter(_ROW, [{"field": "人名", "op": "not_contains",
                                     "value": "巴"}]) is False
        assert _match_filter(_ROW, [{"field": "人名", "op": "eq",
                                     "value": "巴神"}]) is True
        assert _match_filter(_ROW, [{"field": "人名", "op": "ne",
                                     "value": "巴神"}]) is False

    def test_in_and_not_in(self):
        rule = {"field": "人名", "op": "in", "value": ["巴神", "莉莉娅"]}
        assert _match_filter(_ROW, [rule]) is True
        rule2 = {"field": "人名", "op": "not_in", "value": ["巴神"]}
        assert _match_filter(_ROW, [rule2]) is False

    def test_empty_semantics(self):
        assert _match_filter({"data": {"备注": ""}},
                             [{"field": "备注", "op": "empty"}]) is True
        assert _match_filter(_ROW,
                             [{"field": "人名", "op": "not_empty"}]) is True

    def test_rules_combined_with_and(self):
        rules = [{"field": "人名", "op": "eq", "value": "巴神"},
                 {"field": "能力1", "op": "ge", "value": 9,
                  "field_type": "number"}]
        assert _match_filter(_ROW, rules) is False  # 5 < 9

    def test_legacy_dict_equality_still_works(self):
        assert _match_filter({"data": {"status": "failed"}},
                             {"status": "failed"}) is True
        assert _match_filter({"data": {"status": "ok"}},
                             {"status": "failed"}) is False

    def test_op_type_mismatch_defensive(self):
        # 文本字段配置区间：按字符串序比较兜底，不抛异常
        # （中文按 Unicode 排在 ASCII 之后，"巴神" > "z" → False）
        rule = {"field": "人名", "op": "between",
                "value": ["a", "z"], "field_type": "dimension"}
        assert _rule_match(_ROW, rule) is False
        rule2 = {"field": "人名", "op": "between",
                 "value": ["a", "龿"], "field_type": "dimension"}
        assert _rule_match(_ROW, rule2) is True


class TestComputedAndRatio:
    """派生列 / 聚合比 / 维度分组归一（add-bi-calc-fields）"""

    ROWS = [
        {"line": "A线", "passed": 90.0, "inspected": 100.0, "plan": 50.0, "actual": 45.0},
        {"line": "B线", "passed": 80.0, "inspected": 100.0, "plan": 50.0, "actual": 40.0},
        {"line": "C线", "passed": 95.0, "inspected": 100.0, "plan": 0.0, "actual": 0.0},
    ]

    def test_apply_computed_row_level(self):
        rows = [dict(r) for r in self.ROWS]
        apply_computed(rows, [
            {"key": "rate", "name": "合格率",
             "expr": "[passed] / [inspected] * 100", "field_type": "number"}])
        assert rows[0]["rate"] == pytest.approx(90.0)
        assert rows[2]["rate"] == pytest.approx(95.0)

    def test_apply_computed_missing_value_is_null(self):
        rows = [{"passed": None, "inspected": 100.0}]
        apply_computed(rows, [
            {"key": "rate", "expr": "[passed] / [inspected]", "field_type": "number"}])
        assert rows[0]["rate"] is None

    def test_apply_computed_topological_order(self):
        """派生列引用另一派生列：被引用者先算"""
        rows = [{"a": 2.0}]
        apply_computed(rows, [
            {"key": "second", "name": "二", "expr": "[first] * 10",
             "field_type": "number"},
            {"key": "first", "name": "一", "expr": "[a] + 1",
             "field_type": "number"}])
        assert rows[0]["second"] == 30.0

    def test_measure_over_computed(self):
        rows = apply_computed([dict(r) for r in self.ROWS],
                              [{"key": "rate", "expr": "[passed] / [inspected] * 100",
                                "field_type": "number"}])
        val = compute_measure(rows, {"key": "avg_rate", "agg": "avg", "field": "rate"})
        assert val == pytest.approx(88.333333, rel=1e-4)

    def test_ratio_measure(self):
        val = compute_measure(self.ROWS, {
            "key": "attain", "agg": "ratio",
            "numerator": {"field": "actual", "agg": "sum"},
            "denominator": {"field": "plan", "agg": "sum"},
            "scale": 100})
        assert val == pytest.approx(85.0)  # (45+40+0)/(50+50+0)*100

    def test_ratio_zero_denominator_is_null(self):
        rows = [{"a": 0.0, "b": 0.0}]
        val = compute_measure(rows, {"key": "r", "agg": "ratio",
                                     "numerator": {"field": "a", "agg": "sum"},
                                     "denominator": {"field": "b", "agg": "sum"}})
        assert val is None

    def test_dim_groups_normalization(self):
        dim = {"name": "车间", "field": "line",
               "groups": [{"name": "一车间", "values": ["A线", "B线"]}],
               "other": "其他"}
        assert dim_value({"line": "A线"}, dim, {}) == "一车间"
        assert dim_value({"line": "B线"}, dim, {}) == "一车间"
        assert dim_value({"line": "C线"}, dim, {}) == "其他"

    def test_dim_groups_without_other_keeps_value(self):
        dim = {"name": "车间", "field": "line",
               "groups": [{"name": "一车间", "values": ["A线"]}]}
        assert dim_value({"line": "D线"}, dim, {}) == "D线"

    def test_aggregate_rows_with_computed_and_groups(self):
        defn = {
            "computed": [{"key": "rate", "expr": "[passed] / [inspected] * 100",
                          "field_type": "number"}],
            "dimensions": [{"name": "车间", "field": "line",
                            "groups": [{"name": "主线", "values": ["A线", "B线"]}],
                            "other": "其他"}],
            "measures": [{"key": "avg_rate", "agg": "avg", "field": "rate"}],
            "top_n": 0,
        }
        entries = aggregate_rows([dict(r) for r in self.ROWS], defn)
        by_dim = {e["dimensions"]["车间"]: e["values"]["avg_rate"] for e in entries}
        assert by_dim["主线"] == pytest.approx(85.0)   # (90+80)/2
        assert by_dim["其他"] == pytest.approx(95.0)


class TestCaseInsensitiveKeys:
    """Oracle 大小写存量修复（add-bi-sql-pushdown）：键小写返回 vs 大写元数据"""

    def test_get_field_case_insensitive_top_level(self):
        row = {"work_date": "2026-09-27", "actual_qty": 42.0}
        assert get_field(row, "WORK_DATE") == "2026-09-27"
        assert get_field(row, "Actual_Qty") == 42.0

    def test_get_field_case_insensitive_in_data_wrapper(self):
        row = {"data": {"line": "A线"}}
        assert get_field(row, "LINE") == "A线"

    def test_json_path_still_wins(self):
        row = {"data": {"info": {"region": "华东"}}}
        assert get_field(row, "info.region") == "华东"
        # 点分路径按精确段匹配（大小写互认只覆盖顶层列——Oracle 场景所需；
        # buffer/logs 的嵌套键由写入方控制大小写）
        assert get_field(row, "info.REGION") is None

    def test_aggregate_rows_with_uppercase_metadata(self):
        rows = [{"data": {"line": "A线", "actual_qty": 10.0}},
                {"data": {"line": "A线", "actual_qty": 5.0}}]
        defn = {
            "dimensions": [{"name": "产线", "field": "LINE"}],
            "measures": [{"key": "产量", "agg": "sum", "field": "ACTUAL_QTY"}],
            "top_n": 0,
        }
        entries = aggregate_rows([dict(r) for r in rows], defn)
        assert entries[0]["dimensions"]["产线"] == "A线"
        assert entries[0]["values"]["产量"] == 15.0


class TestHeavyQueryHardening:
    """并发硬化：内存聚合经 to_thread（结果不变）"""

    def test_compute_window_uses_thread_and_semaphore(self):
        import asyncio as _asyncio
        from app.services import bi_aggregator as agg

        rows = [{"data": {"line": "A线", "qty": 3.0}}]
        defn = {"dimensions": [{"name": "产线", "field": "line"}],
                "measures": [{"key": "q", "agg": "sum", "field": "qty"}],
                "top_n": 0}

        async def _fake_scan(session, d, start, end):
            return rows

        async def _run():
            orig = agg.scan_window
            agg.scan_window = _fake_scan
            try:
                out = await agg.compute_window(None, defn,
                                               datetime(2026, 9, 27, tzinfo=UTC),
                                               datetime(2026, 9, 27, 1, tzinfo=UTC))
            finally:
                agg.scan_window = orig
            return out

        out = _asyncio.run(_run())
        assert out[0]["values"]["q"] == 3.0


def test_top_n_preserves_user_sort_order():
    """P1 回归：带用户 sort 时 Top N 只截断不改序；无 sort 保持主度量降序"""
    dims = [{"name": "产线", "field": "line"}]
    measures = [{"key": "行数", "agg": "count"}]
    mk = lambda name, v: {"dimensions": {"产线": name}, "values": {"行数": v}}
    entries = [mk("A", 1), mk("B", 9), mk("C", 5)]
    # 无 sort：主度量降序 B(9) C(5) + 其他(1)
    from app.services import bi_aggregator
    out = bi_aggregator._apply_top_n([dict(e) for e in entries], dims,
                                     measures, 2)
    assert [e["dimensions"]["产线"] for e in out] == ["B", "C", bi_aggregator.OTHERS_KEY]
    # 用户 sort 升序：选中仍是度量为大的 B(9) C(5)，但展示顺序保持升序 C→B，
    # 「其他」殿后（选择看度量，顺序看用户）
    defn = {"sort": {"by": "行数", "order": "asc"},
            "dimensions": dims, "measures": measures}
    sorted_entries = bi_aggregator._apply_sort([dict(e) for e in entries], defn)
    out2 = bi_aggregator._apply_top_n(sorted_entries, dims, measures, 2,
                                      preserve_order=True)
    assert [e["dimensions"]["产线"] for e in out2] == ["C", "B", bi_aggregator.OTHERS_KEY]
