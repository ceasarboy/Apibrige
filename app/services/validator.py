"""数据校验引擎 — 字段级必填、类型、范围、正则校验

与 transformer.py 协同工作：transformer 先转换类型，validator 再校验结果。
集成到 sync_handler 的转换→校验→推送流程中。
"""
import re
from typing import Any


class ValidationError(Exception):
    """单字段校验错误"""

    def __init__(self, field: str, rule: str, message: str):
        self.field = field
        self.rule = rule
        self.message = message
        super().__init__(f"[{field}] {message}")


def validate_record(record: dict, validations: dict[str, dict]) -> list[ValidationError]:
    """校验单条记录

    Args:
        record: 转换后的数据记录
        validations: {"field_name": {"required": true, "type": "int", ...}}

    Returns:
        校验错误列表（空列表表示通过）
    """
    errors: list[ValidationError] = []

    for field_name, rules in validations.items():
        value = record.get(field_name)

        # ---- 必填 ----
        if rules.get("required") and (value is None or (isinstance(value, str) and value.strip() == "")):
            errors.append(ValidationError(field_name, "required", f"{field_name} 为必填字段"))
            continue  # 必填失败则跳过后续校验

        if value is None:
            continue  # 非必填且为 None，跳过

        # ---- 类型 ----
        expected_type = rules.get("type")
        if expected_type:
            type_map = {"int": int, "float": (int, float), "str": str, "string": str, "bool": bool}
            py_type = type_map.get(expected_type)
            if py_type and not isinstance(value, py_type):
                errors.append(ValidationError(field_name, "type", f"期望类型 {expected_type}，实际 {type(value).__name__}"))
                continue

        # ---- 最小值/最大值 ----
        if isinstance(value, (int, float)):
            if "min" in rules and value < rules["min"]:
                errors.append(ValidationError(field_name, "min", f"值 {value} 小于最小值 {rules['min']}"))
            if "max" in rules and value > rules["max"]:
                errors.append(ValidationError(field_name, "max", f"值 {value} 大于最大值 {rules['max']}"))

        # ---- 字符串长度 ----
        if isinstance(value, str):
            if "min_length" in rules and len(value) < rules["min_length"]:
                errors.append(ValidationError(field_name, "min_length", f"长度 {len(value)} 小于最小长度 {rules['min_length']}"))
            if "max_length" in rules and len(value) > rules["max_length"]:
                errors.append(ValidationError(field_name, "max_length", f"长度 {len(value)} 超过最大长度 {rules['max_length']}"))

        # ---- 正则 ----
        pattern = rules.get("pattern")
        if pattern and isinstance(value, str):
            if not re.match(pattern, value):
                errors.append(ValidationError(field_name, "pattern", f"值 '{value}' 不匹配模式 {pattern}"))

        # ---- 枚举 ----
        allowed = rules.get("enum")
        if allowed and value not in allowed:
            errors.append(ValidationError(field_name, "enum", f"值 '{value}' 不在允许值 {allowed} 中"))

    return errors


def validate_batch(records: list[dict], validations: dict[str, dict]) -> dict:
    """批量校验

    Returns:
        {
            "valid": bool,
            "total": int,
            "passed": int,
            "failed": int,
            "errors": [{"index": 0, "field": "age", "rule": "min", "message": "..."}, ...]
        }
    """
    all_errors = []
    passed = 0

    for i, record in enumerate(records):
        record_errors = validate_record(record, validations)
        if record_errors:
            for err in record_errors:
                all_errors.append({
                    "index": i,
                    "field": err.field,
                    "rule": err.rule,
                    "message": err.message,
                })
        else:
            passed += 1

    return {
        "valid": len(all_errors) == 0,
        "total": len(records),
        "passed": passed,
        "failed": len(records) - passed,
        "errors": all_errors,
    }
