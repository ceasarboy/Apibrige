"""源样本 Schema 推断（task 3.4）

输出结构: [{"path": "data.order.name", "type": "string", "nullable": bool,
            "is_array": bool, "sample_value": ...}]
数组元素以 items[].field 形式表达路径（如 data.items[].name）。
多元素数组按键并集合并，nullable = 存在缺失/None 的字段。
"""
from __future__ import annotations

from typing import Any

_ARRAY_MARKER = "[]."


def _type_of(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if value is None:
        return "null"
    return "unknown"


def _merge_list_paths(entries: list[dict], prefix: str, out: list[dict]) -> None:
    """合并数组内多个对象元素的键并集，路径加 items[]. 前缀"""
    merged: dict[str, dict] = {}
    for entry in entries:
        if isinstance(entry, dict):
            for path, info in _flatten_object(entry).items():
                if path not in merged:
                    merged[path] = {"types": set(), "null_seen": False,
                                    "sample": info["sample_value"], "is_array": info["is_array"]}
                merged[path]["types"].add(info["type"])
                if info["nullable"]:
                    merged[path]["null_seen"] = True
    for path, info in sorted(merged.items()):
        types = {t for t in info["types"] if t != "null"}
        out.append({
            "path": f"{prefix}{_ARRAY_MARKER}{path}",
            "type": types.pop() if len(types) == 1 else ("number" if types <= {"integer", "number"} else "mixed"),
            "nullable": info["null_seen"] or len(info["types"]) > 1 or not types,
            "is_array": info["is_array"],
            "sample_value": info["sample"],
        })


def _flatten_object(obj: dict, prefix: str = "") -> dict[str, dict]:
    """对象展平为 点分路径 → 字段信息"""
    out: dict[str, dict] = {}
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        _emit(path, value, out)
    return out


def _emit(path: str, value: Any, out: list | dict) -> None:
    if isinstance(value, dict) and value:
        out[path] = {"type": "object", "nullable": False, "is_array": False,
                     "sample_value": "{...}"}
        for sub_path, info in _flatten_object(value, prefix=path).items():
            out[sub_path] = info
        return
    if isinstance(value, list):
        objects = [v for v in value if isinstance(v, dict)]
        if objects:
            # 数组元素路径在返回时统一加 [].，这里先以对象键并集产出
            merged: dict[str, dict] = {}
            for entry in objects:
                for sub_path, info in _flatten_object(entry).items():
                    if sub_path not in merged:
                        merged[sub_path] = {"type": info["type"], "nullable": False,
                                            "is_array": info["is_array"],
                                            "sample_value": info["sample_value"]}
            for sub_path, info in merged.items():
                info_out = dict(info)
                info_out["is_array"] = True
                out[f"{path}{_ARRAY_MARKER}{sub_path}"] = info_out
            scalar_types = {_type_of(v) for v in value if not isinstance(v, (dict, list))}
            if scalar_types:
                out[path] = {"type": scalar_types.pop(), "nullable": False,
                             "is_array": True, "sample_value": value[:1]}
            return
        scalars = [v for v in value if v is not None]
        out[path] = {
            "type": _type_of(scalars[0]) if scalars else "null",
            "nullable": any(v is None for v in value) or not scalars,
            "is_array": True,
            "sample_value": value[:1],
        }
        return
    out[path] = {
        "type": _type_of(value),
        "nullable": value is None,
        "is_array": False,
        "sample_value": value,
    }


def infer_schema(sample: Any) -> list[dict]:
    """从样本推断字段清单（点分嵌套路径 + items[]. 数组表达）"""
    if isinstance(sample, list):
        records = [r for r in sample if isinstance(r, dict)]
        if records:
            merged: dict[str, dict] = {}
            for record in records:
                for path, info in _flatten_object(record).items():
                    if path not in merged:
                        merged[path] = {"types": set(), "null_seen": False,
                                        "sample": info["sample_value"],
                                        "is_array": info["is_array"]}
                    merged[path]["types"].add(info["type"])
                    if info["nullable"]:
                        merged[path]["null_seen"] = True
            result = []
            for path, info in sorted(merged.items()):
                types = {t for t in info["types"] if t != "null"}
                result.append({
                    "path": path,
                    "type": types.pop() if len(types) == 1 else
                            ("number" if types and types <= {"integer", "number"} else "mixed"),
                    "nullable": info["null_seen"] or not types,
                    "is_array": info["is_array"],
                    "sample_value": info["sample"],
                })
            return result
        # 标量数组：作为整体一个字段
        scalars = [v for v in sample if v is not None]
        return [{
            "path": "$", "type": _type_of(scalars[0]) if scalars else "null",
            "nullable": not scalars, "is_array": True, "sample_value": sample[:1],
        }]
    if isinstance(sample, dict):
        items = _flatten_object(sample)
        return [dict(info, path=path) for path, info in sorted(items.items())]
    return [{
        "path": "$", "type": _type_of(sample), "nullable": sample is None,
        "is_array": False, "sample_value": sample,
    }]
