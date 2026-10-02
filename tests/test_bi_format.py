# -*- coding: utf-8 -*-
"""BI 格式转换层测试（task 4.4）

覆盖统一中间格式 → 7 种输出的结构与语义。
"""
from datetime import datetime, timezone

import pytest

from app.services.bi_format import (
    FORMATS,
    build_intermediate,
    convert,
    to_datav_number,
    to_echarts,
    to_table,
)

UTC = timezone.utc


def _dt(*args):
    return datetime(*args, tzinfo=UTC)


def _b(ts, dims=None, values=None):
    return {"bucket_start": ts, "dimensions": dims or {}, "values": values or {}}


LINE_DEF = {
    "metric_key": "sync_throughput",
    "name": "同步吞吐",
    "chart_type": "line",
    "dimensions": [],
    "measures": [{"key": "total", "label": "总量"},
                 {"key": "failed", "label": "失败"}],
}

BUCKETS_LINE = [
    _b(_dt(2026, 9, 25, 10, 0), values={"total": 10, "failed": 1}),
    _b(_dt(2026, 9, 25, 10, 1), values={"total": 20, "failed": 0}),
]


class TestTimeseries:

    def test_build_intermediate_shape(self):
        i = build_intermediate(LINE_DEF, BUCKETS_LINE)
        assert i["type"] == "timeseries"
        assert i["x"] == ["2026-09-25T10:00:00+00:00", "2026-09-25T10:01:00+00:00"]
        assert [s["key"] for s in i["series"]] == ["total", "failed"]
        assert i["series"][0]["data"] == [10, 20]
        assert i["series"][0]["label"] == "总量"

    def test_missing_bucket_value_becomes_none(self):
        """某桶缺该度量的值 → None（不是 0，避免谎报）"""
        buckets = [
            _b(_dt(2026, 9, 25, 10, 0), values={"total": 10}),
            _b(_dt(2026, 9, 25, 10, 1), values={"total": 20, "failed": 3}),
        ]
        i = build_intermediate(LINE_DEF, buckets)
        failed = next(s for s in i["series"] if s["key"] == "failed")
        assert failed["data"] == [None, 3]

    def test_echarts_line_structure(self):
        out = convert(LINE_DEF, BUCKETS_LINE, "echarts-line")
        assert out["xAxis"]["type"] == "category"
        assert len(out["xAxis"]["data"]) == 2
        assert out["series"][0]["type"] == "line"
        assert out["series"][0]["name"] == "总量"

    def test_echarts_bar_structure(self):
        out = convert({**LINE_DEF, "chart_type": "bar"}, BUCKETS_LINE, "echarts-bar")
        assert out["series"][0]["type"] == "bar"

    def test_empty_buckets(self):
        out = convert(LINE_DEF, [], "echarts-line")
        assert out["xAxis"]["data"] == []
        assert out["series"] == []


class TestDimensionSeries:

    DEF = {
        "metric_key": "by_dim", "name": "按维度", "chart_type": "line",
        "dimensions": [{"name": "category"}],
        "measures": [{"key": "n", "label": "数量"}],
    }

    def test_series_expanded_per_dimension(self):
        buckets = [
            _b(_dt(2026, 9, 25, 10, 0), {"category": "A"}, {"n": 1}),
            _b(_dt(2026, 9, 25, 10, 0), {"category": "B"}, {"n": 2}),
            _b(_dt(2026, 9, 25, 10, 1), {"category": "A"}, {"n": 3}),
            _b(_dt(2026, 9, 25, 10, 1), {"category": "B"}, {"n": 4}),
        ]
        i = build_intermediate(self.DEF, buckets)
        assert set(s["key"] for s in i["series"]) == {"A·n", "B·n"}
        a = next(s for s in i["series"] if s["dimension"] == "A")
        assert a["data"] == [1, 3]
        assert a["label"] == "A 数量"


class TestDistribution:

    DEF = {
        "metric_key": "by_cat", "name": "类别分布", "chart_type": "pie",
        "dimensions": [{"name": "category"}],
        "measures": [{"key": "n", "agg": "count"}],
    }

    def test_additive_measure_sums_across_range(self):
        """可加算子（count/sum）在整个区间求和（"最近 24h 各类别占比"）"""
        buckets = [
            _b(_dt(2026, 9, 25, 10, 0), {"category": "A"}, {"n": 99}),
            _b(_dt(2026, 9, 25, 10, 1), {"category": "A"}, {"n": 1}),
            _b(_dt(2026, 9, 25, 10, 1), {"category": "B"}, {"n": 2}),
        ]
        i = build_intermediate(self.DEF, buckets)
        assert i["type"] == "distribution"
        by = {x["name"]: x["value"] for x in i["items"]}
        assert by == {"A": 100, "B": 2}

    def test_non_additive_falls_back_to_latest_bucket(self):
        """不可加算子（avg/p95）无法跨桶合并 → 退化为取最新桶（不编造）"""
        defn = {**self.DEF, "measures": [{"key": "avg", "agg": "avg", "field": "v"}]}
        buckets = [
            _b(_dt(2026, 9, 25, 10, 0), {"category": "A"}, {"avg": 10}),
            _b(_dt(2026, 9, 25, 10, 1), {"category": "A"}, {"avg": 20}),
        ]
        i = build_intermediate(defn, buckets)
        assert i["items"] == [{"name": "A", "value": 20}], \
            "不可加算子应取最新桶而非相加（10+20=30 是错的）"

    def test_echarts_pie_structure(self):
        buckets = [_b(_dt(2026, 9, 25, 10, 1), {"category": "A"}, {"n": 7})]
        out = convert(self.DEF, buckets, "echarts-pie")
        assert out["series"][0]["type"] == "pie"
        assert out["series"][0]["data"] == [{"name": "A", "value": 7}]


class TestMatrix:

    DEF = {
        "metric_key": "heat", "name": "热力", "chart_type": "heatmap",
        "dimensions": [{"name": "task"}],
        "measures": [{"key": "success", "label": "成功"}],
    }

    def test_cells_and_axes(self):
        buckets = [
            _b(_dt(2026, 9, 25, 10, 0), {"task": "t1"}, {"success": 3}),
            _b(_dt(2026, 9, 25, 10, 0), {"task": "t2"}, {"success": 1}),
            _b(_dt(2026, 9, 25, 10, 1), {"task": "t1"}, {"success": 5}),
        ]
        i = build_intermediate(self.DEF, buckets)
        assert i["type"] == "matrix"
        assert len(i["x"]) == 2
        assert set(i["y"]) == {"t1", "t2"}
        assert len(i["cells"]) == 3
        assert all(len(c) == 3 for c in i["cells"])

    def test_echarts_heatmap_structure(self):
        buckets = [_b(_dt(2026, 9, 25, 10, 0), {"task": "t1"}, {"success": 3})]
        out = convert(self.DEF, buckets, "echarts-heatmap")
        assert out["series"][0]["type"] == "heatmap"
        assert out["yAxis"]["data"] == ["t1"]


class TestRecords:

    DEF = {
        "metric_key": "health", "name": "健康明细", "chart_type": "table",
        "dimensions": [{"name": "task"}],
        "measures": [{"key": "success", "label": "成功"},
                     {"key": "failed", "label": "失败"}],
    }

    def test_columns_and_rows(self):
        buckets = [
            _b(_dt(2026, 9, 25, 10, 0), {"task": "t1"},
               {"success": 8, "failed": 2}),
        ]
        out = convert(self.DEF, buckets, "table")
        titles = [c["title"] for c in out["columns"]]
        assert titles == ["task", "时间", "成功", "失败"]
        assert out["rows"][0]["task"] == "t1"
        assert out["rows"][0]["success"] == 8

    def test_to_table_falls_back_for_timeseries(self):
        out = to_table(build_intermediate(LINE_DEF, BUCKETS_LINE))
        assert [c["title"] for c in out["columns"]] == ["时间", "总量", "失败"]
        assert out["rows"][0]["total"] == 10


class TestScalar:

    DEF = {
        "metric_key": "kpi", "name": "KPI", "chart_type": "number",
        "dimensions": [],
        "measures": [{"key": "total", "label": "总量"}],
        "unit": "条",
    }

    def test_latest_bucket_value(self):
        buckets = [
            _b(_dt(2026, 9, 25, 10, 0), values={"total": 1}),
            _b(_dt(2026, 9, 25, 10, 1), values={"total": 42}),
        ]
        out = convert(self.DEF, buckets, "datav-number")
        assert out["value"] == 42
        assert out["unit"] == "条"

    def test_empty_is_none(self):
        out = to_datav_number(build_intermediate(self.DEF, []))
        assert out["value"] is None


class TestConvertDispatch:

    def test_raw_returns_intermediate(self):
        out = convert(LINE_DEF, BUCKETS_LINE, "raw")
        assert out["type"] == "timeseries"

    def test_unknown_format_rejected(self):
        with pytest.raises(ValueError, match="不支持的格式"):
            convert(LINE_DEF, BUCKETS_LINE, "echarts-nope")

    def test_all_declared_formats_callable(self):
        defs = {
            "line": LINE_DEF, "bar": {**LINE_DEF, "chart_type": "bar"},
            "pie": TestDistribution.DEF, "heatmap": TestMatrix.DEF,
            "table": TestRecords.DEF, "number": TestScalar.DEF,
            # 2026-09-25 扩充（kind 与 fmt 必须同族）
            "scatter": {**LINE_DEF, "chart_type": "scatter",
                        "measures": [{"key": "total"}, {"key": "failed"}]},
            "radar": {**LINE_DEF, "chart_type": "radar"},
            "sankey": DIM2_DEF, "graph": {**DIM2_DEF, "chart_type": "graph"},
            "flow": {**DIM2_DEF, "chart_type": "flow"},
            "tree": {**DIM2_DEF, "chart_type": "tree"},
            "funnel": TestDistribution.DEF, "gauge": TestScalar.DEF,
            "time_line": LINE_DEF, "bar3d": TestMatrix.DEF,
        }
        for fmt in FORMATS:
            if fmt in ("raw", "bi-detail"):
                # bi-detail 是表单明细行透传格式，由 API 层直出，不经桶转换
                continue
            _, chart = fmt.split("-", 1) if "-" in fmt else ("", "")
            target = "number" if fmt == "datav-number" else (
                "table" if fmt == "table" else chart)
            out = convert(defs[target], [], fmt)
            assert out is not None


# ============================================================
# 2026-09-25 图表库扩充：scatter / radar / links 族 / tree / funnel / gauge / bar3d
# ============================================================

DIM2_DEF = {
    "metric_key": "rel", "name": "关系", "chart_type": "sankey",
    "dimensions": [{"name": "人名", "field": "p"}, {"name": "日期", "field": "d"}],
    "measures": [{"key": "次数", "agg": "count"}],
}
BUCKETS_DIM2 = [
    _b(_dt(2026, 9, 25, 10, 0), {"人名": "巴神", "日期": "01-01"}, {"次数": 2}),
    _b(_dt(2026, 9, 25, 10, 0), {"人名": "巴神", "日期": "06-01"}, {"次数": 1}),
    _b(_dt(2026, 9, 25, 10, 0), {"人名": "莉莉娅", "日期": "01-01"}, {"次数": 3}),
]


class TestPairs:

    def test_scatter_pairs_by_dim(self):
        defn = {"metric_key": "s", "name": "散点", "chart_type": "scatter",
                "dimensions": [{"name": "人名", "field": "p"}],
                "measures": [{"key": "x1"}, {"key": "y1"}]}
        buckets = [
            _b(_dt(2026, 9, 25, 10, 0), {"人名": "A"}, {"x1": 1, "y1": 2}),
            _b(_dt(2026, 9, 25, 10, 1), {"人名": "A"}, {"x1": 3, "y1": 4}),
            _b(_dt(2026, 9, 25, 10, 0), {"人名": "B"}, {"x1": 5, "y1": 6}),
        ]
        out = convert(defn, buckets, "echarts-scatter")
        by_name = {s["name"]: s["data"] for s in out["series"]}
        assert by_name["A"] == [[1, 2], [3, 4]]
        assert by_name["B"] == [[5, 6]]


class TestRadar:

    def test_latest_bucket_series(self):
        defn = {"metric_key": "r", "name": "雷达", "chart_type": "radar",
                "dimensions": [{"name": "人名", "field": "p"}],
                "measures": [{"key": "a", "label": "能力1"},
                             {"key": "b", "label": "能力2"},
                             {"key": "c", "label": "能力3"}]}
        buckets = [
            _b(_dt(2026, 9, 25, 10, 0), {"人名": "A"}, {"a": 1, "b": 2, "c": 3}),
            _b(_dt(2026, 9, 25, 10, 1), {"人名": "A"}, {"a": 4, "b": 5, "c": 6}),
            _b(_dt(2026, 9, 25, 10, 1), {"人名": "B"}, {"a": 7, "b": 8, "c": 9}),
        ]
        out = convert(defn, buckets, "echarts-radar")
        assert [i["name"] for i in out["indicators"]] == ["能力1", "能力2", "能力3"]
        names = {s["name"]: s["values"] for s in out["series"]}
        assert names["A"] == [4, 5, 6]   # 只取最新桶
        assert names["B"] == [7, 8, 9]


class TestLinks:

    def test_sankey_additive_sum(self):
        out = convert(DIM2_DEF, BUCKETS_DIM2, "echarts-sankey")
        names = {n["name"] for n in out["nodes"]}
        assert {"巴神", "莉莉娅", "01-01", "06-01"} <= names
        links = {(l["source"], l["target"]): l["value"] for l in out["links"]}
        assert links[("巴神", "01-01")] == 2
        assert links[("莉莉娅", "01-01")] == 3

    def test_flow_non_additive_latest_bucket(self):
        defn = {**DIM2_DEF,
                "measures": [{"key": "均值", "agg": "avg"}]}
        buckets = [
            _b(_dt(2026, 9, 25, 10, 0), {"人名": "A", "日期": "d1"}, {"均值": 1}),
            _b(_dt(2026, 9, 25, 10, 1), {"人名": "A", "日期": "d1"}, {"均值": 9}),
        ]
        out = convert(defn, buckets, "echarts-flow")
        assert out["links"][0]["value"] == 9  # 不可加 → 最新桶


class TestTree:

    def test_hierarchy_leaf_values(self):
        out = convert({**DIM2_DEF, "chart_type": "tree"},
                      BUCKETS_DIM2, "echarts-tree")
        root = out["tree"]
        assert root["name"] == "关系"
        by_name = {c["name"]: c for c in root["children"]}
        assert by_name["巴神"]["children"] == [
            {"name": "01-01", "value": 2}, {"name": "06-01", "value": 1}]


class TestFunnelGaugeBar3d:

    def test_funnel_sorted_desc(self):
        defn = {"metric_key": "f", "name": "漏斗", "chart_type": "funnel",
                "dimensions": [{"name": "人名", "field": "p"}],
                "measures": [{"key": "次数", "agg": "count"}]}
        buckets = [
            _b(_dt(2026, 9, 25, 10, 0), {"人名": "A"}, {"次数": 1}),
            _b(_dt(2026, 9, 25, 10, 0), {"人名": "B"}, {"次数": 5}),
        ]
        out = convert(defn, buckets, "echarts-funnel")
        assert [i["name"] for i in out["items"]] == ["B", "A"]

    def test_gauge_scalar(self):
        defn = {"metric_key": "g", "name": "仪表盘", "chart_type": "gauge",
                "dimensions": [],
                "measures": [{"key": "total"}, {"key": "failed"}]}
        out = convert(defn, BUCKETS_LINE, "echarts-gauge")
        assert out["value"] == 20  # 最新桶主度量
        assert out["values"]["failed"] == 0

    def test_bar3d_matrix_triplets(self):
        out = convert({**DIM2_DEF, "chart_type": "bar3d"},
                      BUCKETS_DIM2, "echarts-bar3d")
        assert len(out["cells"]) == 3
        assert out["cells"][0][2] == 2
        assert "2026-09-25T10:00:00+00:00" in out["x"]


class TestDisplayModifiers:
    """展示层修饰：占比 / 累计（add-bi-calc-fields）"""

    DEF = {
        "metric_key": "t", "name": "T", "chart_type": "bar", "granularity": "1h",
        "dimensions": [{"name": "产线", "field": "line"}],
        "measures": [{"key": "产量", "agg": "sum", "field": "qty"}],
        "top_n": 100,
    }
    BUCKETS = [
        {"bucket_start": "2026-09-25T10:00:00+00:00",
         "dimensions": {"产线": "A线"}, "values": {"产量": 60}},
        {"bucket_start": "2026-09-25T10:00:00+00:00",
         "dimensions": {"产线": "B线"}, "values": {"产量": 30}},
        {"bucket_start": "2026-09-25T10:00:00+00:00",
         "dimensions": {"产线": "C线"}, "values": {"产量": 10}},
    ]

    def test_percent_modifier(self):
        buckets = [
            {"bucket_start": "2026-09-25T10:00:00+00:00",
             "dimensions": {"产线": "A线"}, "values": {"产量": 30}},
            {"bucket_start": "2026-09-25T10:00:00+00:00",
             "dimensions": {"产线": "B线"}, "values": {"产量": 20}},
            {"bucket_start": "2026-09-25T10:00:00+00:00",
             "dimensions": {"产线": "C线"}, "values": {"产量": 10}},
        ]
        out = convert({**self.DEF, "display": {"percent": True}},
                      buckets, "echarts-bar")
        data = out["series"][0]["data"]
        assert data == [50.0, 33.3, 16.7]  # 30/60, 20/60, 10/60

    def test_cumulative_modifier(self):
        buckets = [
            {"bucket_start": "2026-09-25T10:00:00+00:00",
             "dimensions": {"产线": "A线"}, "values": {"产量": 10}},
            {"bucket_start": "2026-09-25T11:00:00+00:00",
             "dimensions": {"产线": "A线"}, "values": {"产量": 5}},
            {"bucket_start": "2026-09-25T12:00:00+00:00",
             "dimensions": {"产线": "A线"}, "values": {"产量": 7}},
        ]
        out = convert({**self.DEF, "chart_type": "line", "display": {"cumulative": True}},
                      buckets, "echarts-line")
        assert out["series"][0]["data"] == [10, 15, 22]

    def test_no_display_unchanged(self):
        out = convert(self.DEF, self.BUCKETS, "echarts-bar")
        assert out["series"][0]["data"] == [60, 30, 10]
