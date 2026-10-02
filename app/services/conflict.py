"""冲突检测服务 — 检测目标数据冲突，按策略处理

在 push 目标前，检测目标端是否已存在相同记录。
策略：
  - skip: 跳过冲突数据（保留现有）
  - overwrite: 覆盖现有数据（默认行为）
  - merge: 合并后写入（未实现，预留）
"""
import hashlib
import json
from typing import Any


def compute_record_hash(record: dict, key_field: str | None = None) -> str:
    """计算记录哈希值

    Args:
        record: 数据记录
        key_field: 主键字段（用于冲突检测，不影响哈希）

    Returns:
        SHA256 哈希（16 位十六进制）
    """
    # 移除 key_field 后计算哈希（只对内容做比较）
    normalized = {k: v for k, v in record.items() if k != key_field and not k.startswith("_")}
    canonical = json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def detect_conflicts(
    data_list: list[dict],
    existing_data: list[dict],
    key_field: str = "id",
    strategy: str = "skip",
) -> tuple[list[dict], list[dict], list[dict]]:
    """检测数据冲突

    Args:
        data_list: 待推送的数据
        existing_data: 目标端已有的数据（以 key_field 为索引）
        key_field: 冲突检测的主键字段
        strategy: 冲突处理策略 (skip|overwrite)

    Returns:
        (to_push, conflicts, skipped) - 分别是要推送、冲突、被跳过的数据
    """
    if not existing_data:
        return data_list, [], []

    existing_map = {str(item.get(key_field)): item for item in existing_data if item.get(key_field) is not None}
    conflicts: list[dict] = []
    skipped: list[dict] = []
    to_push: list[dict] = []

    for item in data_list:
        item_key = str(item.get(key_field, ""))
        if item_key and item_key in existing_map:
            conflicts.append(item)
            if strategy == "skip":
                skipped.append(item)
            elif strategy == "overwrite":
                to_push.append(item)
        else:
            to_push.append(item)

    return to_push, conflicts, skipped


def detect_hash_conflict(
    data_list: list[dict],
    existing_hashes: set[str],
    key_field: str = "id",
    strategy: str = "skip",
) -> tuple[list[dict], int]:
    """基于哈希的轻量冲突检测（无需完整数据）

    返回: (to_push, conflict_count)
    """
    if strategy == "overwrite" or not existing_hashes:
        return data_list, 0

    to_push = []
    conflict_count = 0
    for item in data_list:
        item_hash = compute_record_hash(item, key_field)
        if item_hash in existing_hashes:
            conflict_count += 1
            if strategy == "skip":
                continue
        to_push.append(item)

    return to_push, conflict_count
