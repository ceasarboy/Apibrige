# -*- coding: utf-8 -*-
"""计算字段表达式引擎测试（add-bi-calc-fields）

覆盖：基础运算 / 引用解析（中文名·key 回落·未知名）/ 函数白名单（数值·字符串·日期）/
安全边界（属性调用·未知函数·关键字参数·未知标识符）/ 行级容错 / 循环检测。
"""
import pytest

from app.services.bi_expr import (
    ExprError, FUNCTION_NAMES,
    compile_expr, detect_cycle, eval_compiled, extract_refs,
)

# 字段字典：中文显示名 + 行键（path）
FIELDS = {
    "合格数": "passed",
    "检验数": "inspected",
    "实际产量": "actual_qty",
    "计划产量": "plan_qty",
    "产线": "line",
    "日期": "date",
    "备注": "remark",
}


def _resolve(name):
    """调用方契约：中文显示名优先，回落字段行键（path）"""
    if name in FIELDS:
        return FIELDS[name]
    return name if name in FIELDS.values() else None


def run(expr, row):
    return eval_compiled(compile_expr(expr, _resolve), row)


class TestBasicMath:
    def test_ratio(self):
        assert run("[合格数] / [检验数] * 100",
                   {"passed": 921, "inspected": 1000}) == pytest.approx(92.1)

    def test_parentheses_and_unary(self):
        assert run("-[实际产量] + ([计划产量] * 2)",
                   {"actual_qty": 400, "plan_qty": 500}) == 600

    def test_mod(self):
        assert run("[实际产量] % 7", {"actual_qty": 15}) == 1

    def test_div_by_zero_is_null(self):
        assert run("[实际产量] / [计划产量]",
                   {"actual_qty": 100, "plan_qty": 0}) is None

    def test_missing_value_row_is_null(self):
        assert run("[实际产量] + 1", {}) is None

    def test_numeric_string_coerced(self):
        assert run("[实际产量] * 2", {"actual_qty": "120.5"}) == 241


class TestRefs:
    def test_chinese_name_resolved(self):
        c = compile_expr("[合格数] + 1", _resolve)
        assert c.refs == {"__r0": "passed"}

    def test_key_fallback(self):
        c = compile_expr("[passed] + 1", _resolve)
        assert c.refs == {"__r0": "passed"}

    def test_unknown_ref_raises(self):
        with pytest.raises(ExprError, match="不存在的字段"):
            compile_expr("[不存在的列] + 1", _resolve)

    def test_extract_refs_dedup_order(self):
        assert extract_refs("[A] + [B] * [A]") == ["A", "B"]

    def test_empty_expr_raises(self):
        with pytest.raises(ExprError, match="不能为空"):
            compile_expr("   ", _resolve)


class TestFunctions:
    def test_round_with_digits(self):
        assert run("round([合格数] / [检验数] * 100, 1)",
                   {"passed": 1, "inspected": 3}) == pytest.approx(33.3)

    def test_abs_ceil_floor(self):
        assert run("abs(0 - [实际产量])", {"actual_qty": 7}) == 7
        assert run("ceil([实际产量])", {"actual_qty": 1.2}) == 2
        assert run("floor([实际产量])", {"actual_qty": 1.8}) == 1

    def test_min_max(self):
        assert run("min([合格数], [检验数])", {"passed": 3, "inspected": 9}) == 3
        assert run("max([合格数], [检验数])", {"passed": 3, "inspected": 9}) == 9

    def test_coalesce(self):
        assert run("coalesce([实际产量], 0)", {}) == 0
        assert run("coalesce([实际产量], [计划产量], 0)",
                   {"plan_qty": 5}) == 5

    def test_substring_sql_one_based(self):
        assert run("substring([日期], 6, 2)", {"date": "2026-09-27"}) == "09"
        assert run("substring([日期], 6)", {"date": "2026-09-27"}) == "09-27"

    def test_text_functions(self):
        assert run("upper([产线])", {"line": "a线"}) == "A线"
        assert run("lower(trim([产线]))", {"line": " A线 "}) == "a线"
        assert run("concat([产线], '-', [日期])",
                   {"line": "A线", "date": "2026-09-27"}) == "A线-2026-09-27"

    def test_plus_between_text_is_concat(self):
        assert run("[产线] + [日期]", {"line": "B线", "date": "09-27"}) == "B线09-27"

    def test_date_part(self):
        assert run("date_part([日期], 'year')", {"date": "2026-09-27"}) == 2026
        assert run("date_part([日期], 'month')", {"date": "2026-09-27"}) == 9
        assert run("date_part([日期], 'day')", {"date": "2026-09-27"}) == 27
        assert run("date_part([日期], 'year')", {"date": "not-a-date"}) is None

    def test_case_insensitive_function(self):
        assert run("ROUND([实际产量], 0)", {"actual_qty": 2.4}) == 2

    def test_function_names_exposed(self):
        assert "round" in FUNCTION_NAMES and "date_part" in FUNCTION_NAMES


class TestSafety:
    def test_attribute_access_rejected(self):
        with pytest.raises(ExprError):
            compile_expr("[实际产量].__class__", _resolve)
        with pytest.raises(ExprError):
            compile_expr("'abc'.upper()", _resolve)

    def test_unknown_function_rejected(self):
        with pytest.raises(ExprError, match="不支持的函数"):
            compile_expr("eval([实际产量])", _resolve)

    def test_keyword_args_rejected(self):
        with pytest.raises(ExprError, match="关键字参数"):
            compile_expr("round([实际产量], d=1)", _resolve)

    def test_unknown_identifier_rejected(self):
        with pytest.raises(ExprError, match="未知标识符"):
            compile_expr("[实际产量] + os", _resolve)

    def test_lambda_and_comprehension_rejected(self):
        with pytest.raises(ExprError):
            compile_expr("min(x for x in [1])", _resolve)

    def test_bad_syntax(self):
        with pytest.raises(ExprError, match="语法错误"):
            compile_expr("[实际产量] +", _resolve)


class TestCycle:
    def test_no_cycle(self):
        assert detect_cycle({"a": {"b"}, "b": {"c"}, "c": set()}) is None

    def test_self_cycle(self):
        found = detect_cycle({"a": {"a"}})
        assert found == ["a", "a"]

    def test_two_node_cycle(self):
        found = detect_cycle({"a": {"b"}, "b": {"a"}})
        assert found is not None and found[0] == found[-1] and len(found) == 3

    def test_ignores_outside_deps(self):
        # 依赖不在本批次派生列（如普通源字段）不算环
        assert detect_cycle({"a": {"src_col"}, "b": {"a"}}) is None
