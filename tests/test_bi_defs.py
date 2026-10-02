# -*- coding: utf-8 -*-
"""BI 聚合引擎 阶段1 测试：模型 / 内置定义 / 配置校验 / 幂等种子

覆盖 openspec/changes/add-bi-aggregation-engine 的 tasks 1.1–1.7：
- 1.1 三张表建表 + (metric_key, bucket_start, dim_hash) 唯一约束
- 1.2 内置定义幂等种子（首次写入、重复不写、不覆盖用户修改）
- 1.3 算子白名单
- 1.4 维度数量上限（时间维不计入）
- 1.6 维度候选 8 个 + 基数分级 + 高基数告警
- 1.7 度量校验（field 必填、时间点/时长区分）
"""
import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.bi import BiAggregationWatermark, BiMetricBucket, BiMetricDef
from app.models.sync import Base
from app.services.bi_defs import (
    BUILTIN_METRICS,
    BUSINESS_METRIC_KEYS,
    DEFAULT_TOP_N,
    DIMENSION_CANDIDATES,
    GRANULARITY_SECONDS,
    MAX_DIMENSIONS,
    SUPPORTED_AGGS,
    dim_hash,
    estimate_combination_count,
    has_errors,
    is_read_only_sql,
    normalize_error,
    validate_metric_def,
)
from app.services.bi_seed import seed_builtin_metrics


# ============================================================
# Part A：纯函数 —— 配置校验
# ============================================================

def _base_def(**over):
    d = {
        "metric_key": "t1",
        "name": "测试指标",
        "source_type": "execution_logs",
        "granularity": "1min",
        "chart_type": "line",
        "dimensions": [],
        "measures": [{"key": "c", "agg": "count"}],
    }
    d.update(over)
    return d


class TestValidateMetricDef:

    def test_valid_def_passes(self):
        assert has_errors(validate_metric_def(_base_def())) is False

    def test_missing_metric_key(self):
        issues = validate_metric_def(_base_def(metric_key=""))
        assert any(i["code"] == "missing_metric_key" for i in issues)

    def test_invalid_source_type(self):
        issues = validate_metric_def(_base_def(source_type="nope"))
        assert any(i["code"] == "invalid_source_type" for i in issues)

    def test_invalid_granularity(self):
        issues = validate_metric_def(_base_def(granularity="7min"))
        assert any(i["code"] == "invalid_granularity" for i in issues)

    def test_invalid_chart_type(self):
        issues = validate_metric_def(_base_def(chart_type="donut"))
        assert any(i["code"] == "invalid_chart_type" for i in issues)

    def test_supported_aggs_all_pass(self):
        """白名单内每个算子都应通过（count 无需 field；ratio 需分子/分母）"""
        for agg in SUPPORTED_AGGS:
            m = {"key": "x", "agg": agg}
            if agg == "ratio":
                m["numerator"] = {"field": "a", "agg": "sum"}
                m["denominator"] = {"field": "b", "agg": "sum"}
            elif agg != "count":
                m["field"] = "some_col"
            issues = validate_metric_def(_base_def(measures=[m]))
            assert not has_errors(issues), f"{agg} 应通过，实际: {issues}"

    def test_unsupported_agg_rejected_with_available_list(self):
        issues = validate_metric_def(
            _base_def(measures=[{"key": "x", "agg": "median", "field": "a"}]))
        hit = next(i for i in issues if i["code"] == "unsupported_agg")
        assert "count" in hit["available"] and "p99" in hit["available"]

    def test_numeric_agg_requires_field(self):
        issues = validate_metric_def(
            _base_def(measures=[{"key": "s", "agg": "sum"}]))
        assert any(i["code"] == "missing_measure_field" for i in issues)

    def test_count_does_not_require_field(self):
        issues = validate_metric_def(
            _base_def(measures=[{"key": "c", "agg": "count"}]))
        assert not has_errors(issues)


class TestDimensionLimits:

    def test_max_three_explicit_dimensions(self):
        dims = [{"name": f"d{i}", "field": f"f{i}"} for i in range(MAX_DIMENSIONS)]
        assert not has_errors(validate_metric_def(_base_def(dimensions=dims)))

    def test_four_dimensions_rejected(self):
        dims = [{"name": f"d{i}", "field": f"f{i}"} for i in range(4)]
        issues = validate_metric_def(_base_def(dimensions=dims))
        hit = next(i for i in issues if i["code"] == "too_many_dimensions")
        assert hit["limit"] == MAX_DIMENSIONS and hit["actual"] == 4

    def test_time_dimension_not_counted(self):
        """is_time=True 的隐式时间维不占配额"""
        dims = [{"name": f"d{i}", "field": f"f{i}"} for i in range(3)]
        dims.append({"name": "bucket", "is_time": True})
        assert not has_errors(validate_metric_def(_base_def(dimensions=dims)))

    def test_dimension_requires_field_or_resolver(self):
        issues = validate_metric_def(_base_def(dimensions=[{"name": "bad"}]))
        assert any(i["code"] == "invalid_dimension" for i in issues)

    def test_high_cardinality_dimension_warns(self):
        """物料（高基数）作分组维度 → warning（非 error）"""
        dims = [{"name": "物料", "code": "material", "field": "material"}]
        issues = validate_metric_def(_base_def(dimensions=dims))
        assert any(i["code"] == "high_cardinality_dimension" for i in issues)
        assert not has_errors(issues), "高基数是提示而非阻断"

    def test_low_cardinality_dimension_no_warning(self):
        dims = [{"name": "类别", "code": "category", "field": "category"}]
        issues = validate_metric_def(_base_def(dimensions=dims))
        assert not any(i["code"] == "high_cardinality_dimension" for i in issues)

    def test_dimension_candidates_has_eight_confirmed(self):
        """用户确认的 8 个候选维度齐全"""
        names = {c["name"] for c in DIMENSION_CANDIDATES}
        assert names == {"人", "设备", "物料", "计划号", "客户", "类别", "状态", "地区"}

    def test_candidate_cardinality_grading(self):
        by = {c["name"]: c for c in DIMENSION_CANDIDATES}
        assert by["类别"]["cardinality"] == "low"
        assert by["客户"]["cardinality"] == "medium"
        assert by["物料"]["cardinality"] == "high"
        assert by["物料"]["usable_for_group"] is False
        assert by["类别"]["usable_for_group"] is True


class TestFieldTypeSemantics:

    def test_point_in_time_forbids_sum(self):
        issues = validate_metric_def(_base_def(measures=[
            {"key": "t", "agg": "sum", "field": "order_time",
             "field_type": "point_in_time"}]))
        hit = next(i for i in issues if i["code"] == "point_in_time_agg_forbidden")
        assert hit["allowed"] == ["min", "max"]

    def test_point_in_time_forbids_avg(self):
        issues = validate_metric_def(_base_def(measures=[
            {"key": "t", "agg": "avg", "field": "order_time",
             "field_type": "point_in_time"}]))
        assert any(i["code"] == "point_in_time_agg_forbidden" for i in issues)

    def test_point_in_time_allows_min_max(self):
        for agg in ("min", "max"):
            issues = validate_metric_def(_base_def(measures=[
                {"key": "t", "agg": agg, "field": "order_time",
                 "field_type": "point_in_time"}]))
            assert not has_errors(issues), f"{agg} 应被允许"

    def test_duration_allows_sum_and_avg(self):
        for agg in ("sum", "avg"):
            issues = validate_metric_def(_base_def(measures=[
                {"key": "h", "agg": agg, "field": "work_hours",
                 "field_type": "duration"}]))
            assert not has_errors(issues)

    def test_invalid_field_type_rejected(self):
        issues = validate_metric_def(_base_def(measures=[
            {"key": "x", "agg": "sum", "field": "a", "field_type": "blob"}]))
        assert any(i["code"] == "invalid_field_type" for i in issues)


class TestCustomSqlReadOnly:

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM data_buffer",
        "select id from t where x = 1",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "  SELECT 1;  ",
    ])
    def test_read_only_accepted(self, sql):
        assert is_read_only_sql(sql) is True

    @pytest.mark.parametrize("sql", [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a=1",
        "DELETE FROM t",
        "DROP TABLE t",
        "SELECT 1; DROP TABLE t",
        "ALTER TABLE t ADD COLUMN c int",
        "TRUNCATE t",
        "",
        "   ",
    ])
    def test_write_statements_rejected(self, sql):
        assert is_read_only_sql(sql) is False

    def test_custom_sql_def_validated(self):
        issues = validate_metric_def(_base_def(
            source_type="custom_sql", source_ref="UPDATE t SET a=1"))
        assert any(i["code"] == "sql_not_read_only" for i in issues)


class TestHelpers:

    def test_dim_hash_order_independent(self):
        assert dim_hash({"a": "1", "b": "2"}) == dim_hash({"b": "2", "a": "1"})

    def test_dim_hash_differs_on_value(self):
        assert dim_hash({"a": "1"}) != dim_hash({"a": "2"})

    def test_dim_hash_empty_is_stable(self):
        h = dim_hash({})
        assert isinstance(h, str) and len(h) == 40 and h == dim_hash(None)

    def test_normalize_error_masks_volatile_parts(self):
        s = normalize_error(
            "连接失败 550e8400-e29b-41d4-a716-446655440000 "
            "at 2026-09-25T10:01:02Z code=1234")
        assert "<id>" in s and "<time>" in s and "<n>" in s
        assert "550e8400" not in s

    def test_normalize_error_same_shape_same_key(self):
        a = normalize_error("timeout after 30s on row 111")
        b = normalize_error("timeout after 45s on row 999")
        assert a == b

    def test_normalize_error_empty(self):
        assert normalize_error(None) == "未知错误"
        assert normalize_error("   ") == "未知错误"

    def test_estimate_combination_count(self):
        assert estimate_combination_count([]) == 1
        assert estimate_combination_count([50, 50]) == 2500
        assert estimate_combination_count([50, 50, 50]) == 125000


class TestBuiltinDefinitions:

    def test_has_ten_builtin_metrics(self):
        assert len(BUILTIN_METRICS) == 10

    def test_every_builtin_def_passes_validation(self):
        for d in BUILTIN_METRICS:
            issues = validate_metric_def(d)
            errors = [i for i in issues if i["level"] == "error"]
            assert not errors, f"{d['metric_key']} 有错误: {errors}"

    def test_builtin_keys_unique(self):
        keys = [d["metric_key"] for d in BUILTIN_METRICS]
        assert len(keys) == len(set(keys))

    def test_business_metrics_disabled_by_default(self):
        """业务指标默认停用（用户确认：需要自己设置，不开箱可用）"""
        by = {d["metric_key"]: d for d in BUILTIN_METRICS}
        for k in BUSINESS_METRIC_KEYS:
            assert by[k]["enabled"] is False
            assert by[k]["measures"] == []

    def test_ops_metrics_disabled_by_default(self):
        """2026-09-25 重定位拍板：BI 大屏 = 表单业务数据；
        平台运营类内置指标默认停用撤屏，引擎保留，可手动启用"""
        by = {d["metric_key"]: d for d in BUILTIN_METRICS}
        for d in BUILTIN_METRICS:
            assert d["enabled"] is False


class TestChartShapeRules:
    """图表类型的数据形状校验（2026-09-25 图表库扩充）"""

    @staticmethod
    def _def(chart_type, dims, measures):
        return {"metric_key": "m", "name": "m", "source_type": "form",
                "source_ref": '{"connection_id":"c","form_key":"f"}',
                "chart_type": chart_type, "dimensions": dims,
                "measures": measures}

    def test_scatter_needs_two_measures(self):
        issues = validate_metric_def(self._def(
            "scatter", [], [{"key": "x", "agg": "sum", "field": "a"}]))
        assert any(i["code"] == "scatter_needs_two_measures" for i in issues)

    def test_sankey_needs_two_dims(self):
        issues = validate_metric_def(self._def(
            "sankey", [{"name": "人名", "field": "p"}],
            [{"key": "n", "agg": "count"}]))
        assert any(i["code"] == "relation_needs_two_dims" for i in issues)

    def test_graph_two_dims_ok(self):
        issues = validate_metric_def(self._def(
            "graph",
            [{"name": "人名", "field": "p"}, {"name": "日期", "field": "d"}],
            [{"key": "n", "agg": "count"}]))
        assert not any(i["code"] == "relation_needs_two_dims" for i in issues)

    def test_gauge_dims_limit(self):
        issues = validate_metric_def(self._def(
            "gauge",
            [{"name": "人名", "field": "p"}, {"name": "日期", "field": "d"}],
            [{"key": "n", "agg": "count"}]))
        assert any(i["code"] == "gauge_dims_limit" for i in issues)

    def test_radar_hint_is_warning_only(self):
        issues = validate_metric_def(self._def(
            "radar", [{"name": "人名", "field": "p"}],
            [{"key": "a", "agg": "sum", "field": "x"}]))
        hits = [i for i in issues if i["code"] == "radar_measures_hint"]
        assert hits and hits[0]["level"] == "warning"

    def test_granularity_values_known(self):
        for d in BUILTIN_METRICS:
            assert d["granularity"] in GRANULARITY_SECONDS


# ============================================================
# Part B：数据库 —— 建表 / 唯一约束 / 幂等种子
# ============================================================

@pytest.fixture
def bi_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/bi.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    asyncio.run(engine.dispose())


class TestModelsAndSeed:

    async def test_bi_tables_created(self, bi_db):
        from sqlalchemy import inspect
        async with bi_db() as session:
            conn = await session.connection()

            def _tables(sync_conn):
                return inspect(sync_conn).get_table_names()

            names = await conn.run_sync(_tables)
        assert {"bi_metric_defs", "bi_metric_buckets",
                "bi_aggregation_watermark"} <= set(names)

    async def test_bucket_unique_constraint(self, bi_db):
        """同一 (metric_key, bucket_start, dim_hash) 不得重复"""
        ts = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
        async with bi_db() as session:
            session.add(BiMetricBucket(metric_key="m", bucket_start=ts,
                                       dim_hash="h" * 40, values={"c": 1}))
            await session.commit()
        with pytest.raises(IntegrityError):
            async with bi_db() as session:
                session.add(BiMetricBucket(metric_key="m", bucket_start=ts,
                                           dim_hash="h" * 40, values={"c": 2}))
                await session.commit()

    async def test_seed_writes_builtin(self, bi_db):
        async with bi_db() as session:
            result = await seed_builtin_metrics(session)
        assert len(result["added"]) == len(BUILTIN_METRICS)
        async with bi_db() as session:
            rows = (await session.execute(select(BiMetricDef))).scalars().all()
        assert len(rows) == len(BUILTIN_METRICS)
        assert all(r.builtin for r in rows)
        assert all(r.top_n == DEFAULT_TOP_N for r in rows)

    async def test_seed_is_idempotent(self, bi_db):
        async with bi_db() as session:
            await seed_builtin_metrics(session)
        async with bi_db() as session:
            second = await seed_builtin_metrics(session)
        assert second["added"] == []
        async with bi_db() as session:
            rows = (await session.execute(select(BiMetricDef))).scalars().all()
        assert len(rows) == len(BUILTIN_METRICS)

    async def test_seed_does_not_overwrite_user_edit(self, bi_db):
        """用户改过名称/启停后，重新种子不得覆盖"""
        async with bi_db() as session:
            await seed_builtin_metrics(session)
        async with bi_db() as session:
            row = (await session.execute(
                select(BiMetricDef).where(
                    BiMetricDef.metric_key == "sync_throughput"))).scalar_one()
            row.name = "我改过的名字"
            row.enabled = False
            await session.commit()
        async with bi_db() as session:
            await seed_builtin_metrics(session)
        async with bi_db() as session:
            row = (await session.execute(
                select(BiMetricDef).where(
                    BiMetricDef.metric_key == "sync_throughput"))).scalar_one()
        assert row.name == "我改过的名字"
        assert row.enabled is False

    async def test_seed_adds_only_missing(self, bi_db):
        """表中已有一条时，只补缺失项"""
        async with bi_db() as session:
            session.add(BiMetricDef(
                metric_key="sync_throughput", name="手写", source_type="data_buffer"))
            await session.commit()
        async with bi_db() as session:
            result = await seed_builtin_metrics(session)
        assert "sync_throughput" not in result["added"]
        assert len(result["added"]) == len(BUILTIN_METRICS) - 1

    async def test_watermark_roundtrip(self, bi_db):
        ts = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
        async with bi_db() as session:
            session.add(BiAggregationWatermark(metric_key="m", last_bucket=ts))
            await session.commit()
        async with bi_db() as session:
            row = await session.get(BiAggregationWatermark, "m")
        assert row is not None
        assert row.last_bucket.replace(tzinfo=timezone.utc) == ts


class TestCalcFieldValidation:
    """计算字段 / 聚合比 / 自定义分组 / 展示修饰 校验（add-bi-calc-fields）"""

    def test_computed_valid_passes(self):
        issues = validate_metric_def(_base_def(computed=[
            {"key": "rate", "name": "合格率", "expr": "[合格数] / [检验数] * 100",
             "field_type": "number"}]))
        assert not has_errors(issues), issues

    def test_computed_bad_expr_rejected(self):
        issues = validate_metric_def(_base_def(computed=[
            {"key": "x", "name": "X", "expr": "[a] +", "field_type": "number"}]))
        assert any(i["code"] == "invalid_computed_expr" for i in issues)

    def test_computed_unknown_function_rejected(self):
        issues = validate_metric_def(_base_def(computed=[
            {"key": "x", "name": "X", "expr": "not_a_function([a])",
             "field_type": "number"}]))
        assert any(i["code"] == "invalid_computed_expr" for i in issues)

    def test_computed_cycle_rejected(self):
        issues = validate_metric_def(_base_def(computed=[
            {"key": "a", "name": "A", "expr": "[B] + 1", "field_type": "number"},
            {"key": "b", "name": "B", "expr": "[A] + 1", "field_type": "number"}]))
        assert any(i["code"] == "computed_circular_ref" for i in issues)

    def test_computed_ref_names_strict(self):
        """精确引用集合：引用不存在的字段被拒"""
        issues = validate_metric_def(
            _base_def(computed=[{"key": "x", "name": "X", "expr": "[不存在] + 1",
                                 "field_type": "number"}]),
            ref_names={"合格数", "检验数"})
        assert any(i["code"] == "invalid_computed_expr" for i in issues)

    def test_dimension_requires_name(self):
        # P1 回归：缺 name 的维度在聚合行里键为 None/被丢弃
        issues = validate_metric_def(_base_def(
            dimensions=[{"field": "region"}]))
        codes = [i["code"] for i in issues]
        assert "invalid_dimension" in codes

    def test_measure_key_conflicts_dimension(self):
        # P1 回归：度量 key 与维度名同名 → 展平时度量覆盖维度值
        issues = validate_metric_def(_base_def(
            source_type="form",
            dimensions=[{"name": "产线", "field": "line"}],
            measures=[{"key": "产线", "agg": "count"}]))
        assert [i["code"] for i in issues
                if i["code"] == "measure_key_conflicts_dimension"]

    def test_ratio_valid_and_invalid(self):
        ok = validate_metric_def(_base_def(measures=[
            {"key": "r", "agg": "ratio",
             "numerator": {"field": "a", "agg": "sum"},
             "denominator": {"field": "b", "agg": "sum"}, "scale": 100}]))
        assert not has_errors(ok), ok
        bad = validate_metric_def(_base_def(measures=[
            {"key": "r", "agg": "ratio", "numerator": {"field": "a"}}]))
        assert any(i["code"] == "invalid_ratio" for i in bad)

    def test_dim_groups_valid_and_invalid(self):
        ok = validate_metric_def(_base_def(dimensions=[
            {"name": "车间", "field": "line",
             "groups": [{"name": "一车间", "values": ["A线", "B线"]}],
             "other": "其他"}]))
        assert not has_errors(ok), ok
        bad = validate_metric_def(_base_def(dimensions=[
            {"name": "车间", "field": "line",
             "groups": [{"name": "一车间", "values": []}]}]))
        assert any(i["code"] == "invalid_dim_groups" for i in bad)

    def test_cumulative_rejected_for_form_source(self):
        issues = validate_metric_def(_base_def(
            source_type="form",
            source_ref='{"connection_id":"c1","form_key":"f1"}',
            display={"cumulative": True}))
        assert any(i["code"] == "cumulative_requires_rolling_source" for i in issues)

    def test_cumulative_allowed_for_rolling_source(self):
        issues = validate_metric_def(
            _base_def(source_type="data_buffer", display={"cumulative": True}))
        assert not has_errors(issues), issues

    def test_percent_valid(self):
        issues = validate_metric_def(_base_def(display={"percent": True}))
        assert not has_errors(issues), issues
