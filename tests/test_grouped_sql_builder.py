# -*- coding: utf-8 -*-
"""聚合下推构造器测试（add-bi-sql-pushdown 阶段一）

覆盖 app/connectors/database.py 的 `_build_grouped_sql`（SQLAlchemy 表达式编译）：
SQL 结构、标识符白名单拒绝、别名映射、过滤/排序/分页、参数不内联（命名绑定）。

注：本文件只固化"构造器"（纯编译路径）；执行点接入（app/connectors/grouped_pushdown.py
单一入口）与内存/下推差分对照见 tests/test_bi_pushdown.py。
"""
import pytest

from app.connectors.database import _build_grouped_sql


def build(**over):
    args = dict(
        table="orders",
        group_fields=["region"],
        aggs=[{"key": "金额", "func": "sum", "field": "amount"}],
        filters=None, order_by=None, limit=None, offset=None,
    )
    args.update(over)
    return _build_grouped_sql(**args)


class TestGroupedSqlBuilder:
    def test_basic_group_by_sqlite(self):
        sql, params, alias_map = build()
        assert "FROM orders" in sql
        assert "sum(orders.amount) AS a0" in sql
        assert "GROUP BY orders.region" in sql
        assert alias_map == {"region": "region", "a0": "金额"}
        assert params == []                       # 值不内联（无过滤时无参数）

    def test_filter_values_bound_not_inlined(self):
        sql, params, _ = build(filters=[
            {"field": "status", "op": "eq", "value": "SECRET_VALUE"},
            {"field": "qty", "op": "ge", "value": 10},
        ])
        assert "SECRET_VALUE" not in sql          # 真参数化：值不在语句文本里
        assert "orders.status = ?" in sql and "orders.qty >= ?" in sql
        assert sorted(params, key=str) == sorted(["SECRET_VALUE", 10], key=str)

    def test_in_and_between(self):
        sql, params, _ = build(filters=[
            {"field": "line", "op": "in", "value": ["A线", "B线"]},
            {"field": "qty", "op": "between", "value": [1, 9]},
        ])
        # IN 用 OR 等值元组表达（规避 SQLAlchemy POSTCOMPILE 占位，raw 驱动可执行）
        assert "orders.line = ?" in sql and "BETWEEN" in sql
        assert len(params) == 4

    def test_empty_value_rejected(self):
        with pytest.raises(ValueError):
            build(filters=[{"field": "x", "op": "eq", "value": None}])

    def test_illegal_identifier_rejected(self):
        with pytest.raises(ValueError, match="非法"):
            build(table="orders; DROP TABLE users")
        with pytest.raises(ValueError, match="非法"):
            build(group_fields=["中文 列"])
        with pytest.raises(ValueError, match="非法"):
            build(filters=[{"field": "a b", "op": "eq", "value": 1}])

    def test_unknown_op_and_agg_rejected(self):
        with pytest.raises(ValueError, match="不可下推的过滤"):
            build(filters=[{"field": "x", "op": "contains", "value": "a"}])
        with pytest.raises(ValueError, match="不支持的聚合"):
            build(aggs=[{"key": "k", "func": "p95", "field": "amount"}])

    def test_count_and_distinct(self):
        sql, _, alias_map = build(aggs=[
            {"key": "行数", "func": "count", "field": None},
            {"key": "去重线", "func": "count_distinct", "field": "line"},
        ], group_fields=["region"])
        assert "count(*) AS a0" in sql
        assert "count(DISTINCT orders.line) AS a1" in sql
        assert alias_map["a0"] == "行数" and alias_map["a1"] == "去重线"

    def test_order_and_pagination(self):
        sql, params, _ = build(order_by=[("a0", "desc"), ("region", "asc")],
                               limit=10, offset=20)
        assert "ORDER BY sum(orders.amount) DESC" in sql
        # 分页参数同样参数化（不内联）
        assert "LIMIT ? OFFSET ?" in sql
        assert params == [10, 20]

    def test_no_group_fields_is_scalar_agg(self):
        sql, _, alias_map = build(group_fields=[])
        assert "GROUP BY" not in sql
        assert alias_map == {"a0": "金额"}

    def test_pg_and_mysql_dialects_compile(self):
        for dialect in ("postgresql", "mysql"):
            sql, _, _ = build(dialect=dialect)
            assert "sum(orders.amount) AS a0" in sql
