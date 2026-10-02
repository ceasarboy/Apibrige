"""测试用例管理 — CRUD、导入导出、分组、标签"""
from __future__ import annotations
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class TestCase:
    """测试用例"""
    id: str = ""
    name: str = ""
    description: str = ""
    group: str = ""
    tags: list[str] = field(default_factory=list)
    # 发送配置
    source_connection_id: str = ""
    source_endpoint: str = ""
    request_data: dict = field(default_factory=dict)
    # 接收配置
    target_connection_id: str = ""
    target_endpoint: str = ""
    # 比对配置
    field_mapping: dict[str, str] = field(default_factory=dict)
    key_field: str = ""
    # 压测配置
    concurrency: int = 1
    duration_ms: int = 5000
    ramp_up: bool = False
    # 元数据
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "group": self.group, "tags": self.tags,
            "source_connection_id": self.source_connection_id,
            "source_endpoint": self.source_endpoint,
            "request_data": self.request_data,
            "target_connection_id": self.target_connection_id,
            "target_endpoint": self.target_endpoint,
            "field_mapping": self.field_mapping, "key_field": self.key_field,
            "concurrency": self.concurrency, "duration_ms": self.duration_ms,
            "ramp_up": self.ramp_up,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TestCase":
        return cls(
            id=data.get("id", ""), name=data.get("name", ""),
            description=data.get("description", ""),
            group=data.get("group", ""), tags=data.get("tags", []),
            source_connection_id=data.get("source_connection_id", ""),
            source_endpoint=data.get("source_endpoint", ""),
            request_data=data.get("request_data", {}),
            target_connection_id=data.get("target_connection_id", ""),
            target_endpoint=data.get("target_endpoint", ""),
            field_mapping=data.get("field_mapping", {}),
            key_field=data.get("key_field", ""),
            concurrency=data.get("concurrency", 1),
            duration_ms=data.get("duration_ms", 5000),
            ramp_up=data.get("ramp_up", False),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


class CaseManager:
    """测试用例管理器（内存存储）"""

    def __init__(self):
        self._cases: dict[str, TestCase] = {}

    def create(self, case: TestCase) -> TestCase:
        self._cases[case.id] = case
        return case

    def get(self, case_id: str) -> TestCase | None:
        return self._cases.get(case_id)

    def update(self, case_id: str, updates: dict) -> TestCase | None:
        case = self._cases.get(case_id)
        if case is None:
            return None
        for key, value in updates.items():
            if hasattr(case, key):
                setattr(case, key, value)
        case.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return case

    def delete(self, case_id: str) -> bool:
        if case_id in self._cases:
            del self._cases[case_id]
            return True
        return False

    def list(self, group: str | None = None, tag: str | None = None) -> list[TestCase]:
        cases = list(self._cases.values())
        if group:
            cases = [c for c in cases if c.group == group]
        if tag:
            cases = [c for c in cases if tag in c.tags]
        return cases

    def list_groups(self) -> list[str]:
        return sorted(set(c.group for c in self._cases.values() if c.group))

    def export_json(self, group: str | None = None) -> str:
        """导出为 JSON 字符串"""
        cases = self.list(group=group)
        return json.dumps([c.to_dict() for c in cases], ensure_ascii=False, indent=2)

    def import_json(self, json_str: str) -> list[TestCase]:
        """从 JSON 字符串导入"""
        data_list = json.loads(json_str)
        imported = []
        for data in data_list:
            case = TestCase.from_dict(data)
            case.id = str(uuid.uuid4())  # 新ID避免冲突
            self._cases[case.id] = case
            imported.append(case)
        return imported

    @property
    def count(self) -> int:
        return len(self._cases)


# 全局单例
case_manager = CaseManager()
