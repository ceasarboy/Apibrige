"""映射草案的确定性校验器（task 3.5 / 3.6）

校验用代码而非模型判断：源字段存在性、目标字段存在性、类型转换可行性、
必填覆盖。无提议时按"同名 + 类型兼容"规则生成退化草案（无模型可用场景）。
"""
from __future__ import annotations

from typing import Any

# 数值/字符串等价组：同组或 unknown 之间兼容（含 Python 运行时类型名与 DB 列类型名）
_NUMBER_TYPES = {"number", "integer", "int", "float", "decimal", "numeric",
                 "double", "bigint", "smallint"}
_STRING_TYPES = {"string", "text", "varchar", "varchar2", "char", "clob", "str"}
_DATETIME_TYPES = {"datetime", "date", "timestamp"}

# 目标必填字段（DB schema: nullable=False 且非主键自增）；API 契约用 required 列表
_UNKNOWN = "unknown"

# 声明了这些 transform 时，即使原始类型不兼容也视为可行（转换器做真实转换）
_CONVERTING_TRANSFORMS = {"int", "integer", "float", "number", "str", "string",
                          "bool", "boolean"}


def _norm_type(t: Any) -> str:
    return str(t or _UNKNOWN).strip().lower()


def types_compatible(source_type: Any, target_type: Any) -> bool:
    """源类型是否可写入目标类型（保守矩阵；unknown 互相兼容）"""
    s, t = _norm_type(source_type), _norm_type(target_type)
    if s == _UNKNOWN or t == _UNKNOWN or s == t:
        return True
    groups = (_NUMBER_TYPES, _STRING_TYPES, _DATETIME_TYPES)
    for group in groups:
        if s in group and t in group:
            return True
    # 数字 → 字符串 可行（有损方向由人审）；字符串 → 数字 不可行
    if s in _NUMBER_TYPES and t in _STRING_TYPES:
        return True
    return False


def _find_field(schema: list[dict], path: str) -> dict | None:
    """大小写不敏感查找（Oracle 列名大写、源路径任意大小写的互认）"""
    for field in schema or []:
        if str(field.get("path") or field.get("name") or "").lower() == path.lower():
            return field
    return None


def validate_draft(
    source_schema: list[dict],
    target_schema: list[dict] | None,
    proposal: list[dict],
    required_target_fields: set[str] | None = None,
) -> dict:
    """校验提议的字段对应关系（agent 提议 + core 确定性校验）

    Args:
        source_schema: infer_schema 输出（或含 name/type 的表结构）
        target_schema: 目标 schema（表结构或已声明契约）；None 表示目标契约
                       未声明（API 目标常见），此时跳过目标侧校验，
                       契约比对交由 L2 的 no_contract 降级兜底
        proposal: [{"source": "a.b", "target": "x", "transform": "string"?}, ...]
        required_target_fields: 目标必填字段名集合

    Returns:
        {"valid": bool, "items": [{source,target,status,reason}...],
         "issues": [{code,...}], "fields": 可用草案}
    """
    items: list[dict] = []
    issues: list[dict] = []
    accepted: list[dict] = []
    required = required_target_fields or set()

    for entry in proposal or []:
        src_path = str(entry.get("source", "")).strip()
        tgt_path = str(entry.get("target", "")).strip()
        if not src_path or not tgt_path:
            issues.append({"code": "invalid_entry", "entry": entry,
                           "message": "缺少 source 或 target"})
            continue

        src_field = _find_field(source_schema, src_path)
        if src_field is None:
            issues.append({"code": "unknown_source_field", "path": src_path,
                           "message": f"源字段不存在: {src_path}"})
            items.append({"source": src_path, "target": tgt_path,
                          "status": "unknown_source_field"})
            continue

        tgt_field = _find_field(target_schema or [], tgt_path)
        if tgt_field is None and target_schema:
            issues.append({"code": "unknown_target_field", "path": tgt_path,
                           "message": f"目标字段不存在: {tgt_path}"})
            items.append({"source": src_path, "target": tgt_path,
                          "status": "unknown_target_field"})
            continue

        if tgt_field is None:
            # 无契约：仅校验源侧，目标侧留给 L2 契约比对
            items.append({"source": src_path, "target": tgt_path, "status": "ok"})
            accepted.append({
                "source": src_path, "target": tgt_path,
                "transform": entry.get("transform", "string"),
            })
            continue

        # 数组源 → 标量目标且未声明数组路径，类型不可行（task 3.5 场景）
        if src_field.get("is_array") and not tgt_field.get("is_array") \
                and _ARRAY_IN_PATH not in src_path:
            issues.append({"code": "type_infeasible", "source": src_path,
                           "target": tgt_path,
                           "message": f"源字段为数组而目标为标量: {src_path} → {tgt_path}"})
            items.append({"source": src_path, "target": tgt_path,
                          "status": "type_infeasible"})
            continue

        transform = str(entry.get("transform") or "").lower()
        if not types_compatible(src_field.get("type"), tgt_field.get("type"))                 and transform not in _CONVERTING_TRANSFORMS:
            issues.append({"code": "type_infeasible", "source": src_path,
                           "target": tgt_path,
                           "message": f"类型不兼容: {src_field.get('type')} → "
                                      f"{tgt_field.get('type')}"
                                      f"（未声明可用的 transform）"})
            items.append({"source": src_path, "target": tgt_path,
                          "status": "type_infeasible"})
            continue

        items.append({"source": src_path, "target": tgt_path, "status": "ok"})
        accepted.append({
            "source": src_path, "target": tgt_path,
            "transform": entry.get("transform", "string"),
        })

    missing_required = sorted(
        req for req in required
        if req not in {a["target"] for a in accepted}
    )
    for req in missing_required:
        issues.append({"code": "missing_required", "path": req,
                       "message": f"目标必填字段未覆盖: {req}"})

    return {
        "valid": not issues,
        "items": items,
        "issues": issues,
        "fields": accepted,
        "missing_required": missing_required,
    }


_ARRAY_IN_PATH = "[]."


def generate_degenerate_draft(
    source_schema: list[dict],
    target_schema: list[dict],
) -> list[dict]:
    """无模型退化匹配（task 3.6）：同名 + 类型兼容

    返回可直接用作 mapping_config["fields"] 的草案。
    """
    draft: list[dict] = []
    for tgt in target_schema or []:
        t_name = tgt.get("name") or tgt.get("path")
        if not t_name:
            continue
        for src in source_schema or []:
            s_name = src.get("path") or src.get("name")
            if not s_name:
                continue
            if s_name.split(".")[-1].lower() != str(t_name).split(".")[-1].lower():
                continue
            if types_compatible(src.get("type"), tgt.get("type")):
                draft.append({"source": s_name, "target": t_name, "transform": "string"})
                break
    return draft
