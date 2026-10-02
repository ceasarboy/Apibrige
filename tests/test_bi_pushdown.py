# -*- coding: utf-8 -*-
"""聚合下推（add-bi-sql-pushdown 阶段三）测试

三层：
1. 构造器扩展（新算子 / NULL 等价补偿 / 方言分派）——纯函数；
2. 判定矩阵 `can_pushdown`——纯函数（假 conn）；
3. **内存/下推差分对照**——同一份数据分别走两条路径，entries 逐项一致
   （含 NULL 行、多维度、各算子、ratio、Top N「其他」归并）；
4. `compute_window` 接入（下推命中不扫源；异常/超限/不可下推回退内存）。
"""
import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.connectors.database import (
    _build_grouped_sql,
    is_safe_identifier,
    supports_group_pushdown,
)
from app.connectors.grouped_pushdown import (
    GROUP_ROW_CAP,
    PushdownTooLarge,
    PushdownUnsupported,
    fetch_grouped_rows,
)
from app.models.sync import ApiConnection, Base
from app.services import bi_sources
from app.services.bi_aggregator import (
    aggregate_rows,
    entries_from_grouped,
    estimate_cardinality,
    compute_window,
)
from app.services.bi_sources import (
    can_pushdown,
    pushdown_aggs,
    pushdown_filters,
    try_pushdown_rows,
)

UTC = timezone.utc
TABLE = "shift_metrics"


# ============================================================
# 1. 构造器扩展
# ============================================================

def build(**over):
    args = dict(
        table="orders", group_fields=["region"],
        aggs=[{"key": "金额", "func": "sum", "field": "amount"}],
        filters=None, order_by=None, limit=None, offset=None,
    )
    args.update(over)
    return _build_grouped_sql(**args)


class TestBuilderExtras:

    def test_not_in_and_not_between(self):
        sql, params, _ = build(filters=[
            {"field": "line", "op": "not_in", "value": ["A线", "B线"]},
            {"field": "qty", "op": "not_between", "value": [1, 9]},
        ])
        assert "orders.line != ?" in sql and "orders.qty < ?" in sql
        assert "orders.qty > ?" in sql
        assert len(params) == 4

    def test_null_match_wraps_or_is_null(self):
        sql, _, _ = build(filters=[
            {"field": "line", "op": "ne", "value": "A线", "null_match": True},
            {"field": "qty", "op": "gt", "value": 1},
        ])
        assert "(orders.line != ? OR orders.line IS NULL)" in sql
        # 未标记的算子不加补偿（NULL 行内存侧本就不匹配）
        assert "orders.qty IS NULL" not in sql

    def test_offset_without_limit_uses_sqlite_sentinel(self):
        sql, params, _ = build(limit=5, offset=20)
        assert "LIMIT ? OFFSET ?" in sql and params == [5, 20]
        # SQLite 的 OFFSET 必须配 LIMIT：无 limit 时 SQLAlchemy 补 -1（= 不限制）
        sql, params, _ = build(limit=None, offset=20)
        assert "LIMIT ? OFFSET ?" in sql and params == [-1, 20]

    def test_unknown_dialect_rejected(self):
        with pytest.raises(ValueError, match="不支持的下推方言"):
            build(dialect="oracle")

    def test_postgresql_compiles_to_numeric_dollar(self):
        # 运行时驱动是 asyncpg：必须是 $n 占位（psycopg2 风格会让执行失败）
        sql, params, _ = build(dialect="postgresql", filters=[
            {"field": "status", "op": "eq", "value": "SECRET_VALUE"}])
        assert "orders.status = $1" in sql or "orders.status = $1::" in sql
        assert params == ["SECRET_VALUE"]

    def test_safe_identifier(self):
        assert is_safe_identifier("plan_qty") and is_safe_identifier("列_1") is False
        assert is_safe_identifier("a b") is False
        assert is_safe_identifier(5) is False


# ============================================================
# 2. 判定矩阵（纯函数）
# ============================================================

def sqlite_conn():
    return SimpleNamespace(db_type="sqlite")


def defn(**over):
    base = {
        "metric_key": "m", "source_type": "form",
        "source_ref": json.dumps({"connection_id": "x", "form_key": TABLE}),
        "dimensions": [{"name": "产线", "field": "line"}],
        "measures": [{"key": "产量", "agg": "sum", "field": "qty",
                      "field_type": "number"}],
        "filters": None, "computed": None,
    }
    base.update(over)
    return base


class TestCanPushdown:

    def test_pushable_baseline(self):
        ok, reason = can_pushdown(defn(), sqlite_conn())
        assert ok and reason == "ok"

    @pytest.mark.parametrize("over,reason", [
        ({"source_type": "execution_logs"}, "source_type:execution_logs"),
        ({"computed": [{"key": "r", "expr": "[qty] + 1"}]}, "computed_fields"),
        ({"dimensions": [{"name": "线", "field": "line", "resolver": "task_connection"}]},
         "dimension_resolver"),
        ({"dimensions": [{"name": "线", "field": "line", "normalize": "error_message"}]},
         "dimension_normalize"),
        ({"dimensions": [{"name": "线", "field": "line", "groups": [{"name": "一", "values": ["A"]}]}]},
         "dimension_groups"),
        ({"dimensions": [{"name": "线", "field": "中文列"}]}, "identifier:中文列"),
        ({"measures": [{"key": "p", "agg": "p95", "field": "qty"}]}, "agg:p95"),
        ({"measures": [{"key": "s", "agg": "sum", "field": "note", "field_type": "string"}]},
         "measure_field_type:string"),
        ({"measures": [{"key": "c", "agg": "count", "filter": {"line": "A"}}]},
         "measure_filter"),
        ({"filters": [{"field": "note", "op": "contains", "value": "x"}]},
         "filter_op:contains"),
        ({"filters": [{"field": "note", "op": "empty"}]}, "filter_op:empty"),
        ({"filters": [{"field": "qty", "op": "gt", "value": 1, "field_type": "string"}]},
         "filter_field_type:string"),
        ({"filters": [{"field": "qty", "op": "between", "value": [1], "field_type": "number"}]},
         "filter_value_shape"),
        ({"filters": [{"field": "qty", "op": "eq", "value": None}]}, "filter_null_value"),
        ({"filters": "bad"}, "invalid_filters"),
        ({"measures": [{"key": "r", "agg": "ratio", "field_type": "number",
                        "numerator": {"agg": "p95", "field": "qty"},
                        "denominator": {"agg": "sum", "field": "plan"}}]},
         "ratio_agg:p95"),
    ])
    def test_fallback_matrix(self, over, reason):
        ok, got = can_pushdown(defn(**over), sqlite_conn())
        assert not ok and got == reason

    def test_hap_source_and_oracle_driver(self):
        # HAP：无 db_type；Oracle：驱动能力标志默认关
        ok, reason = can_pushdown(defn(), SimpleNamespace(db_type=None))
        assert not ok and reason == "source_not_db"
        ok, reason = can_pushdown(defn(), SimpleNamespace(db_type="oracle"))
        assert not ok and reason == "driver:oracle"

    def test_ratio_and_count_and_extra_ops_ok(self):
        d = defn(
            measures=[
                {"key": "行数", "agg": "count"},
                {"key": "去重", "agg": "count_distinct", "field": "line"},
                {"key": "达成", "agg": "ratio", "numerator": {"agg": "sum", "field": "qty"},
                 "denominator": {"agg": "sum", "field": "plan"}, "scale": 100},
            ],
            filters=[{"field": "line", "op": "ne", "value": "A线"},
                     {"field": "qty", "op": "between", "value": [1, 9],
                      "field_type": "number"}],
        )
        ok, reason = can_pushdown(d, sqlite_conn())
        assert ok and reason == "ok"

    def test_legacy_dict_filter_ok(self):
        ok, _ = can_pushdown(defn(filters={"line": "A线"}), sqlite_conn())
        assert ok
        # 空值等值过滤不可绑定参数 → 回退
        ok, reason = can_pushdown(defn(filters={"line": None}), sqlite_conn())
        assert not ok and reason == "filter_null_value"


class TestPushdownRequestBuilders:

    def test_aggs_and_ratio_keys(self):
        d = defn(measures=[
            {"key": "产量", "agg": "sum", "field": "qty", "field_type": "number"},
            {"key": "达成", "agg": "ratio", "numerator": {"agg": "sum", "field": "qty"},
             "denominator": {"agg": "count"}, "scale": 100},
        ])
        aggs, ratio_keys = pushdown_aggs(d)
        assert [a["key"] for a in aggs] == ["产量", "__ratio1__num", "__ratio1__den"]
        assert aggs[2]["func"] == "count" and aggs[2]["field"] is None
        assert ratio_keys["达成"] == {"numerator": "__ratio1__num",
                                      "denominator": "__ratio1__den"}

    def test_filters_null_match_from_memory_matcher(self):
        d = defn(filters=[
            {"field": "line", "op": "ne", "value": "A线"},
            {"field": "line", "op": "eq", "value": "A线"},
            {"field": "line", "op": "in", "value": ["A线"]},
            {"field": "line", "op": "not_in", "value": ["A线"]},
            {"field": "qty", "op": "between", "value": [1, 9], "field_type": "number"},
            {"field": "qty", "op": "not_between", "value": [1, 9], "field_type": "number"},
        ])
        by_op = {r["op"]: r["null_match"] for r in pushdown_filters(d)}
        assert by_op == {"ne": True, "eq": False, "in": False, "not_in": True,
                         "between": False, "not_between": True}

    def test_legacy_dict_filter_becomes_eq(self):
        rules = pushdown_filters(defn(filters={"line": "A线"}))
        assert rules == [{"field": "line", "op": "eq", "value": "A线",
                          "null_match": False}]


# ============================================================
# 3. 差分对照（真实 SQLite 文件 + 内存路径）
# ============================================================

ROWS = [
    # (id, line, shift, qty, plan, defect, note)
    (1, "A线", "早班", 10.0, 12.0, 1.0, "n1"),
    (2, "A线", "早班", 20.0, 20.0, 0.0, "n2"),
    (3, "A线", "中班", 15.5, 10.0, 2.0, "n3"),
    (4, "B线", "早班", 30.0, 30.0, None, "n4"),
    (5, "B线", "中班", 5.0, 40.0, 4.0, "n5"),
    (6, "C线", "晚班", None, 10.0, 1.0, "n6"),
    (7, None, "早班", 40.0, 50.0, 3.0, None),
    (8, None, None, 7.0, 7.0, 0.0, "n8"),
    (9, "C线", "晚班", 25.0, 25.0, None, "n9"),
    (10, "B线", "晚班", 8.0, 9.0, 1.0, "n1"),
]


@pytest.fixture
def src_db(tmp_path):
    """数据源库：真表 + NULL 行（NULL 是等价性最容易翻车的地方）"""
    path = tmp_path / "src.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        f"CREATE TABLE {TABLE} (id INTEGER PRIMARY KEY, line TEXT, shift TEXT, "
        "qty REAL, plan REAL, defect REAL, note TEXT)")
    conn.executemany(
        f"INSERT INTO {TABLE} (id, line, shift, qty, plan, defect, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", ROWS)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def app_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/app.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def _clear_caches():
    bi_sources._form_cache.clear()
    bi_sources._pushdown_cache.clear()
    yield
    bi_sources._form_cache.clear()
    bi_sources._pushdown_cache.clear()


def _make_defn(conn_id, **over):
    base = {
        "metric_key": "diff_case", "source_type": "form",
        "source_ref": json.dumps({"connection_id": str(conn_id), "form_key": TABLE}),
        "dimensions": [], "measures": [], "filters": None, "computed": None,
        "top_n": 100,
    }
    base.update(over)
    return base


async def _add_sqlite_connection(maker, src_path):
    async with maker() as s:
        conn = ApiConnection(
            name="差分源库", connection_type="target", protocol="sqlite",
            db_type="sqlite", db_config={"database": str(src_path)},
            status="active",
        )
        s.add(conn)
        await s.commit()
        await s.refresh(conn)
        return conn.id


async def _memory_entries(maker, d, conn_id):
    """内存路径：扫源（fetch_form_rows）→ aggregate_rows"""
    async with maker() as s:
        rows = await bi_sources.fetch_form_rows(s, str(conn_id), TABLE)
        scanned = [{"data": r, "fetched_at": datetime.now(UTC)} for r in rows]
        return aggregate_rows(scanned, d)


async def _pushdown_entries(maker, d):
    """下推路径：try_pushdown_rows → entries_from_grouped"""
    async with maker() as s:
        grouped = await try_pushdown_rows(s, d)
        assert grouped is not None, "本用例应命中下推"
        return entries_from_grouped(d, grouped)


DIFF_CASES = {
    "单维度-全区聚合算子": dict(
        dimensions=[{"name": "产线", "field": "line"}],
        measures=[
            {"key": "行数", "agg": "count"},
            {"key": "产量", "agg": "sum", "field": "qty", "field_type": "number"},
            {"key": "均值", "agg": "avg", "field": "qty", "field_type": "number"},
            {"key": "最小", "agg": "min", "field": "qty", "field_type": "number"},
            {"key": "最大", "agg": "max", "field": "qty", "field_type": "number"},
            {"key": "去重备注", "agg": "count_distinct", "field": "note"},
        ]),
    "双维度": dict(
        dimensions=[{"name": "产线", "field": "line"}, {"name": "班次", "field": "shift"}],
        measures=[{"key": "行数", "agg": "count"},
                  {"key": "不良", "agg": "sum", "field": "defect", "field_type": "number"}]),
    "无维度汇总": dict(
        dimensions=[],
        measures=[{"key": "行数", "agg": "count"},
                  {"key": "计划", "agg": "sum", "field": "plan", "field_type": "number"}]),
    "ratio": dict(
        dimensions=[{"name": "产线", "field": "line"}],
        measures=[{"key": "达成率", "agg": "ratio",
                   "numerator": {"agg": "sum", "field": "qty"},
                   "denominator": {"agg": "sum", "field": "plan"},
                   "scale": 100}]),
    "ratio-计数分母": dict(
        dimensions=[{"name": "班次", "field": "shift"}],
        measures=[{"key": "人均", "agg": "ratio",
                   "numerator": {"agg": "sum", "field": "qty"},
                   "denominator": {"agg": "count"}}]),
    "过滤-eq": dict(
        dimensions=[{"name": "班次", "field": "shift"}],
        measures=[{"key": "行数", "agg": "count"}],
        filters=[{"field": "line", "op": "eq", "value": "A线"}]),
    "过滤-ne含NULL": dict(
        dimensions=[{"name": "产线", "field": "line"}],
        measures=[{"key": "行数", "agg": "count"}],
        filters=[{"field": "shift", "op": "ne", "value": "早班"}]),
    "过滤-in": dict(
        dimensions=[{"name": "产线", "field": "line"}],
        measures=[{"key": "行数", "agg": "count"}],
        filters=[{"field": "shift", "op": "in", "value": ["早班", "中班"]}]),
    "过滤-not_in含NULL": dict(
        dimensions=[{"name": "产线", "field": "line"}],
        measures=[{"key": "行数", "agg": "count"}],
        filters=[{"field": "shift", "op": "not_in", "value": ["早班"]}]),
    "过滤-数值区间": dict(
        dimensions=[{"name": "产线", "field": "line"}],
        measures=[{"key": "产量", "agg": "sum", "field": "qty", "field_type": "number"}],
        filters=[{"field": "qty", "op": "between", "value": [6, 26],
                  "field_type": "number"}]),
    "过滤-数值比较": dict(
        dimensions=[{"name": "班次", "field": "shift"}],
        measures=[{"key": "产量", "agg": "sum", "field": "qty", "field_type": "number"}],
        filters=[{"field": "qty", "op": "gt", "value": 9, "field_type": "number"}]),
    "过滤-旧等值形态": dict(
        dimensions=[{"name": "产线", "field": "line"}],
        measures=[{"key": "行数", "agg": "count"}],
        filters={"shift": "早班"}),
    "Top N归并": dict(
        dimensions=[{"name": "班次", "field": "shift"}],
        measures=[{"key": "产量", "agg": "sum", "field": "qty", "field_type": "number"}],
        top_n=1),
}


class TestMemoryPushdownDifferential:
    """同一份数据两条路径的 entries 必须逐项一致（含 NULL 行与类型）"""

    @pytest.mark.parametrize("case", sorted(DIFF_CASES))
    def test_entries_identical(self, app_db, src_db, case):
        over = DIFF_CASES[case]
        conn_id = asyncio.run(_add_sqlite_connection(app_db, src_db))
        d = _make_defn(conn_id, **over)
        ok, reason = can_pushdown(d, SimpleNamespace(db_type="sqlite"))
        assert ok, f"{case} 应可下推，实际回退原因: {reason}"

        memory = asyncio.run(_memory_entries(app_db, d, conn_id))
        pushed = asyncio.run(_pushdown_entries(app_db, d))

        assert sorted(memory, key=lambda e: json.dumps(
            e["dimensions"], sort_keys=True, ensure_ascii=False, default=str)) == \
            sorted(pushed, key=lambda e: json.dumps(
                e["dimensions"], sort_keys=True, ensure_ascii=False, default=str))

    def test_null_dimension_becomes_unknown(self, app_db, src_db):
        conn_id = asyncio.run(_add_sqlite_connection(app_db, src_db))
        d = _make_defn(conn_id,
                       dimensions=[{"name": "产线", "field": "line"}],
                       measures=[{"key": "行数", "agg": "count"}])
        pushed = asyncio.run(_pushdown_entries(app_db, d))
        by_dim = {e["dimensions"]["产线"]: e["values"]["行数"] for e in pushed}
        assert by_dim["未知"] == 2           # id=7、8 两行 line 为 NULL
        assert by_dim["A线"] == 3

    def test_compute_window_uses_pushdown(self, app_db, src_db, monkeypatch):
        """compute_window 命中下推：不扫源（scan_window 被替换为炸点）"""
        conn_id = asyncio.run(_add_sqlite_connection(app_db, src_db))
        d = _make_defn(conn_id,
                       dimensions=[{"name": "产线", "field": "line"}],
                       measures=[{"key": "行数", "agg": "count"}])

        def _boom(*a, **kw):
            raise AssertionError("下推命中时不应扫源")

        monkeypatch.setattr(bi_sources, "scan_window", _boom)

        async def _run():
            async with app_db() as s:
                return await compute_window(s, d, datetime.now(UTC),
                                            datetime.now(UTC))

        entries = asyncio.run(_run())
        assert {e["dimensions"]["产线"] for e in entries} == {"A线", "B线", "C线", "未知"}

    def test_estimate_cardinality_uses_pushdown(self, app_db, src_db, monkeypatch):
        conn_id = asyncio.run(_add_sqlite_connection(app_db, src_db))
        d = _make_defn(conn_id,
                       dimensions=[{"name": "产线", "field": "line"}],
                       measures=[{"key": "行数", "agg": "count"}])

        def _boom(*a, **kw):
            raise AssertionError("下推命中时不应扫源")

        monkeypatch.setattr(bi_sources, "scan_window", _boom)

        async def _run():
            async with app_db() as s:
                return await estimate_cardinality(s, d)

        out = asyncio.run(_run())
        assert out["combinations"] == 4      # A线/B线/C线/NULL 四种


# ============================================================
# 4. 执行器边界与回退
# ============================================================

class TestExecutorBoundaries:

    def test_oracle_connector_unsupported(self):
        from app.connectors.database import OracleConnector

        connector = OracleConnector({})
        with pytest.raises(PushdownUnsupported):
            asyncio.run(fetch_grouped_rows(connector, TABLE, ["line"], []))

    def test_unsupported_flags(self):
        assert supports_group_pushdown("sqlite") is True
        assert supports_group_pushdown("postgres") is True      # 别名归一
        assert supports_group_pushdown("oracle") is False
        assert supports_group_pushdown("hap") is False

    def test_group_row_cap_falls_back(self, app_db, src_db, monkeypatch):
        monkeypatch.setattr("app.connectors.grouped_pushdown.GROUP_ROW_CAP", 3)
        conn_id = asyncio.run(_add_sqlite_connection(app_db, src_db))
        d = _make_defn(conn_id,
                       dimensions=[{"name": "产线", "field": "line"}],
                       measures=[{"key": "行数", "agg": "count"}])

        async def _run():
            async with app_db() as s:
                return await try_pushdown_rows(s, d)

        assert asyncio.run(_run()) is None
        # 失败标记已缓存（同一窗口内不重试）
        assert any(not v[1] and v[2].startswith("pushdown_failed")
                   for v in bi_sources._pushdown_cache.values())

    def test_execution_error_falls_back(self, app_db, src_db, monkeypatch):
        from app.connectors import grouped_pushdown

        conn_id = asyncio.run(_add_sqlite_connection(app_db, src_db))
        d = _make_defn(conn_id,
                       dimensions=[{"name": "产线", "field": "line"}],
                       measures=[{"key": "行数", "agg": "count"}])

        async def _boom(*a, **kw):
            raise RuntimeError("源库挂了")

        monkeypatch.setitem(grouped_pushdown._EXECUTORS, "sqlite", _boom)

        async def _run():
            async with app_db() as s:
                return await try_pushdown_rows(s, d)

        assert asyncio.run(_run()) is None
        reasons = [v[2] for v in bi_sources._pushdown_cache.values()]
        assert any("pushdown_failed:RuntimeError" in r for r in reasons)

    def test_missing_table_falls_back(self, app_db, src_db):
        conn_id = asyncio.run(_add_sqlite_connection(app_db, src_db))
        d = _make_defn(conn_id,
                       dimensions=[{"name": "产线", "field": "line"}],
                       measures=[{"key": "行数", "agg": "count"}])
        d["source_ref"] = json.dumps({"connection_id": str(conn_id),
                                      "form_key": "not_exist_table"})

        async def _run():
            async with app_db() as s:
                return await try_pushdown_rows(s, d)

        assert asyncio.run(_run()) is None

    def test_decision_cached_not_repeated(self, app_db, src_db, monkeypatch):
        """判定只跑一次（同一指纹）：第二次直接命中缓存"""
        conn_id = asyncio.run(_add_sqlite_connection(app_db, src_db))
        d = _make_defn(conn_id, dimensions=[{"name": "线", "field": "中文列"}],
                       measures=[{"key": "行数", "agg": "count"}])
        calls = {"n": 0}
        real = bi_sources.can_pushdown

        def _counted(*a, **kw):
            calls["n"] += 1
            return real(*a, **kw)

        monkeypatch.setattr(bi_sources, "can_pushdown", _counted)

        async def _run():
            async with app_db() as s:
                first = await try_pushdown_rows(s, d)
                second = await try_pushdown_rows(s, d)
                return first, second

        first, second = asyncio.run(_run())
        assert first is None and second is None
        assert calls["n"] == 1

    def test_cap_and_limit_interaction(self, app_db, src_db):
        """limit 由执行器收敛为 cap+1（不因调用方限行而误判超限）"""
        from app.connectors.database import get_database_connector

        conn_id = asyncio.run(_add_sqlite_connection(app_db, src_db))
        assert conn_id is not None
        connector = get_database_connector("sqlite", {"database": str(src_db)})

        async def _run():
            return await fetch_grouped_rows(
                connector, TABLE, ["line"], [{"key": "n", "func": "count"}])

        rows = asyncio.run(_run())
        assert len(rows) == 4 and all(isinstance(r["n"], int) for r in rows)

    def test_too_large_raises(self, app_db, src_db, monkeypatch):
        from app.connectors.database import get_database_connector

        connector = get_database_connector("sqlite", {"database": str(src_db)})
        monkeypatch.setattr("app.connectors.grouped_pushdown.GROUP_ROW_CAP", 1)

        async def _run():
            return await fetch_grouped_rows(
                connector, TABLE, ["line"], [{"key": "n", "func": "count"}])

        with pytest.raises(PushdownTooLarge):
            asyncio.run(_run())

    def test_aggs_numeric_normalization(self, app_db, src_db):
        """归一：count→int、sum→float（与 compute_measure 返回类型一致）"""
        from app.connectors.database import get_database_connector

        connector = get_database_connector("sqlite", {"database": str(src_db)})

        async def _run():
            return await fetch_grouped_rows(
                connector, TABLE, ["shift"],
                [{"key": "n", "func": "count"},
                 {"key": "q", "func": "sum", "field": "qty"}])

        rows = asyncio.run(_run())
        assert rows and all(isinstance(r["n"], int) and isinstance(r["q"], float)
                            for r in rows)


class TestHapStillMemoryPath:
    """HAP 源与不可下推定义维持现状（内存路径 + 10 万闸）"""

    def test_hap_connection_returns_none(self, app_db):
        async def _add():
            async with app_db() as s:
                conn = ApiConnection(name="HAP", connection_type="target",
                                     protocol="hap", auth_type="custom",
                                     auth_config={}, status="active")
                s.add(conn)
                await s.commit()
                await s.refresh(conn)
                return conn.id

        conn_id = asyncio.run(_add())
        d = _make_defn(conn_id, dimensions=[{"name": "线", "field": "line"}],
                       measures=[{"key": "行数", "agg": "count"}])

        async def _run():
            async with app_db() as s:
                return await try_pushdown_rows(s, d)

        assert asyncio.run(_run()) is None


class TestEmptyAggregationGuard:
    """无维度且无度量（detail 卡形态）不得进下推：空选择会生成非法 SQL

    2026-09-29 浏览器实测抓到：abnormal_detail/demo_quality_detail/perf_detail 每轮聚合
    都在刷「聚合下推执行失败（回退内存路径）: OperationalError: near "LIMIT": syntax error」。
    """

    def test_can_pushdown_rejects_empty_aggregation(self):
        d = {"metric_key": "d", "source_type": "form",
             "source_ref": json.dumps({"connection_id": "x", "form_key": TABLE}),
             "chart_type": "detail", "dimensions": [], "measures": []}
        ok, reason = can_pushdown(d, sqlite_conn())
        assert not ok and reason == "empty_aggregation"

    def test_builder_fails_fast_on_empty_select(self):
        with pytest.raises(ValueError, match="至少一个分组列或聚合列"):
            _build_grouped_sql("orders", [], [], None, None, None, None)

    def test_dims_only_still_pushable(self):
        """仅维度（无度量）仍可下推——基数预估走这条路"""
        d = {"metric_key": "d", "source_type": "form",
             "source_ref": json.dumps({"connection_id": "x", "form_key": TABLE}),
             "dimensions": [{"name": "产线", "field": "line"}], "measures": []}
        assert can_pushdown(d, sqlite_conn())[0] is True


# ============================================================
# 5. P0-4 回归：下推不得绕过字段可读性与脱敏口径
# ============================================================

class TestPushdownColumnSafety:
    """下推 SELECT 直读源表列——列名命中脱敏提示词或字段字典禁读 → 拒绝下推"""

    def test_sensitive_dimension_column(self):
        d = _make_defn(uuid.uuid4(),
                       dimensions=[{"name": "客户电话", "field": "customer_phone"}],
                       measures=[{"key": "行数", "agg": "count"}])
        ok, reason = can_pushdown(d, SimpleNamespace(db_type="sqlite"))
        assert not ok and reason == "sensitive_column:customer_phone"

    def test_sensitive_measure_column(self):
        d = _make_defn(uuid.uuid4(),
                       dimensions=[{"name": "产线", "field": "line"}],
                       measures=[{"key": "密钥长度", "agg": "sum",
                                  "field": "api_key_len", "field_type": "number"}])
        ok, reason = can_pushdown(d, SimpleNamespace(db_type="sqlite"))
        assert not ok and reason == "sensitive_column:api_key_len"

    def test_separator_variant_normalized(self):
        """app_key 归一化后命中 appkey 提示词（旧子串匹配会漏掉分隔符变体）"""
        d = _make_defn(uuid.uuid4(),
                       dimensions=[{"name": "应用键", "field": "app_key"}],
                       measures=[{"key": "行数", "agg": "count"}])
        ok, reason = can_pushdown(d, SimpleNamespace(db_type="sqlite"))
        assert not ok and reason == "sensitive_column:app_key"

    def test_unreadable_column_blocks_and_recovers(self, app_db, src_db):
        """字段字典把分组列标 readable=False → 下推拒绝回退内存；未登记的列不受影响"""
        from app.models.sync import ConnectionField
        conn_id = asyncio.run(_add_sqlite_connection(app_db, src_db))
        d = _make_defn(conn_id,
                       dimensions=[{"name": "产线", "field": "line"}],
                       measures=[{"key": "行数", "agg": "count"}])

        async def _seed(maker, cid):
            async with maker() as s:
                s.add(ConnectionField(connection_id=cid, scope=TABLE, path="line",
                                      name="产线", readable=False))
                await s.commit()

        asyncio.run(_seed(app_db, conn_id))

        async def _run(defn):
            async with app_db() as s:
                return await try_pushdown_rows(s, defn)

        assert asyncio.run(_run(d)) is None
        # 对照 1：未登记进字典的分组列不拦
        bi_sources._pushdown_cache.clear()
        d2 = _make_defn(conn_id,
                        dimensions=[{"name": "班次", "field": "shift"}],
                        measures=[{"key": "行数", "agg": "count"}])
        assert asyncio.run(_run(d2)) is not None
        # 对照 2：字典登记为可读 → 不拦
        bi_sources._pushdown_cache.clear()

        async def _mark_readable(maker, cid):
            from sqlalchemy import update
            async with maker() as s:
                await s.execute(update(ConnectionField)
                                .where(ConnectionField.connection_id == cid)
                                .values(readable=True))
                await s.commit()

        asyncio.run(_mark_readable(app_db, conn_id))
        bi_sources._pushdown_cache.clear()
        assert asyncio.run(_run(d)) is not None
