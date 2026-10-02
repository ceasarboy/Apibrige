"""数据比对引擎 — 字段级数据比对、类型转换验证、容差比对"""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FieldDiff:
    """字段差异"""
    field: str
    expected: Any
    actual: Any
    reason: str = ""


@dataclass
class CompareResult:
    """比对结果"""
    matched: bool = False
    total_fields: int = 0
    matched_fields: int = 0
    mismatched_fields: int = 0
    missing_fields: int = 0
    extra_fields: int = 0
    diffs: list[FieldDiff] = field(default_factory=list)
    source_data: Any = None
    target_data: Any = None

    @property
    def match_rate(self) -> float:
        return self.matched_fields / self.total_fields if self.total_fields > 0 else 1.0

    def summary(self) -> str:
        parts = [
            f"匹配: {'是' if self.matched else '否'}",
            f"总字段: {self.total_fields}",
            f"匹配: {self.matched_fields}",
            f"不匹配: {self.mismatched_fields}",
            f"缺失: {self.missing_fields}",
            f"多余: {self.extra_fields}",
        ]
        return ", ".join(parts)


class DataComparator:
    """数据比对引擎

    功能：
    - 字段级比对（精确匹配）
    - 类型转换验证（int 与 "string" 数字兼容比对）
    - 容差比对（浮点数容差）
    - 嵌套字段比对（json 字符串解析后比对）
    """

    def __init__(self, tolerance: float = 1e-9):
        self.tolerance = tolerance

    def compare(self, source: dict, target: dict,
                field_mapping: dict[str, str] | None = None) -> CompareResult:
        """字段级比对

        Args:
            source: 源数据
            target: 目标数据
            field_mapping: 源字段→目标字段映射（不传则直接比对同名字段）

        Returns:
            CompareResult 包含所有差异详情
        """
        result = CompareResult(source_data=source, target_data=target)
        diffs = []

        if field_mapping:
            # 有映射：按映射关系比对
            result.total_fields = len(field_mapping)
            for src_field, tgt_field in field_mapping.items():
                expected = source.get(src_field)
                actual = target.get(tgt_field)
                diff = self._compare_value(src_field, tgt_field, expected, actual)
                if diff:
                    diffs.append(diff)
                    result.mismatched_fields += 1
                else:
                    result.matched_fields += 1
        else:
            # 无映射：比对所有同名字段
            all_keys = set(source.keys()) | set(target.keys())
            result.total_fields = len(all_keys)
            for key in sorted(all_keys):
                expected = source.get(key)
                actual = target.get(key)
                if key not in source:
                    diffs.append(FieldDiff(field=key, expected=None, actual=actual, reason="多余字段"))
                    result.extra_fields += 1
                elif key not in target:
                    diffs.append(FieldDiff(field=key, expected=expected, actual=None, reason="缺失字段"))
                    result.missing_fields += 1
                else:
                    diff = self._compare_value(key, key, expected, actual)
                    if diff:
                        diffs.append(diff)
                        result.mismatched_fields += 1
                    else:
                        result.matched_fields += 1

        result.diffs = diffs
        result.matched = len(diffs) == 0
        return result

    def compare_batch(self, sources: list[dict], targets: list[dict],
                      field_mapping: dict[str, str] | None = None,
                      key_field: str | None = None) -> list[CompareResult]:
        """批量比对

        Args:
            sources: 源数据列表
            targets: 目标数据列表
            field_mapping: 字段映射
            key_field: 关联字段（用于对不齐的记录比对）

        Returns:
            每条源记录的比对结果列表
        """
        if key_field and targets:
            # 按 key_field 建立目标索引
            target_map = {t.get(key_field): t for t in targets if key_field in t}
            results = []
            for src in sources:
                target = target_map.get(src.get(key_field))
                if target:
                    results.append(self.compare(src, target, field_mapping))
                else:
                    results.append(CompareResult(
                        matched=False, source_data=src, target_data=None,
                        total_fields=len(src), mismatched_fields=len(src),
                        diffs=[FieldDiff(field="__missing_target__",
                                         expected=src.get(key_field),
                                         actual=None,
                                         reason="目标中未找到匹配记录")],
                    ))
            return results

        # 按位置一一比对
        results = []
        for i, src in enumerate(sources):
            target = targets[i] if i < len(targets) else None
            if target is None:
                results.append(CompareResult(
                    matched=False, source_data=src, target_data=None,
                    total_fields=len(src), mismatched_fields=len(src),
                    diffs=[FieldDiff(field="__missing_target__",
                                     expected=i, actual=None,
                                     reason="目标记录不足")],
                ))
            else:
                results.append(self.compare(src, target, field_mapping))
        return results

    def _compare_value(self, src_field: str, tgt_field: str,
                       expected: Any, actual: Any) -> FieldDiff | None:
        """比对单个值"""
        if expected is None and actual is None:
            return None
        if expected is None and actual is not None:
            return FieldDiff(field=src_field, expected=None, actual=actual, reason="目标多出值")
        if expected is not None and actual is None:
            return FieldDiff(field=src_field, expected=expected, actual=None, reason="目标缺失值")

        # 类型转换兼容比对
        if type(expected) != type(actual):
            compatible = self._try_compatible_compare(expected, actual)
            if compatible is not None:
                return None if compatible else FieldDiff(
                    field=src_field, expected=expected, actual=actual,
                    reason=f"类型转换后不匹配: {type(expected).__name__} vs {type(actual).__name__}")

        # 直接比对
        if expected == actual:
            return None

        # 浮点数容差比对
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            if abs(float(expected) - float(actual)) <= self.tolerance:
                return None

        return FieldDiff(field=src_field, expected=expected, actual=actual, reason="值不匹配")

    def _try_compatible_compare(self, expected: Any, actual: Any) -> bool | None:
        """类型转换兼容比对，返回 None 表示无法兼容比对"""
        try:
            # str ↔ bool (must check before int/float since bool is subclass of int)
            if isinstance(expected, str) and isinstance(actual, bool):
                return expected.lower() in ("true", "1", "yes") and actual is True
            if isinstance(expected, bool) and isinstance(actual, str):
                return actual.lower() in ("true", "1", "yes") == expected
            # str ↔ number
            if isinstance(expected, str) and isinstance(actual, (int, float)):
                return abs(float(expected) - float(actual)) <= self.tolerance
            if isinstance(expected, (int, float)) and isinstance(actual, str):
                return abs(float(expected) - float(actual)) <= self.tolerance
        except (ValueError, TypeError):
            pass
        return None
