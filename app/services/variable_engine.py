"""变量引擎 — 支持 {{variable}} 语法解析内置和自定义变量

用法:
    engine = VariableEngine()
    resolved = engine.resolve("https://api.example.com/{{endpoint}}", {"endpoint": "users"})
    # → "https://api.example.com/users"

    engine.resolve("{{timestamp}}")           # → "1723456789000"
    engine.resolve("{{uuid}}")                # → "a1b2c3d4-..."
    engine.resolve("{{random}}")              # → "847291"
    engine.resolve("{{now}}")                 # → "2026-08-11T23:45:00"
    engine.resolve("{{index}}")               # → "0" (first call), "1" (second), ...
    engine.reset_counter()                    # reset index
"""
import re
import time
import random
from uuid import uuid4
from datetime import datetime
from typing import Any


class VariableEngine:
    """变量解析引擎"""

    # 内置变量工厂（每次调用时即时生成）
    BUILTINS = {
        "timestamp": lambda: str(int(time.time() * 1000)),
        "uuid": lambda: str(uuid4()),
        "uuid4": lambda: str(uuid4()),
        "random": lambda: str(random.randint(100000, 999999)),
        "random_int": lambda: str(random.randint(100000, 999999)),
        "random_str": lambda: "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=8)),
        "now": lambda: datetime.now().isoformat(),
        "now_date": lambda: datetime.now().strftime("%Y-%m-%d"),
        "now_time": lambda: datetime.now().strftime("%H:%M:%S"),
        "unix_ts": lambda: str(int(time.time())),
    }

    def __init__(self):
        self._counter = 0
        self._seq_counters: dict[str, int] = {}

    def reset_counter(self):
        """重置所有计数器"""
        self._counter = 0
        self._seq_counters.clear()

    def resolve(self, value: Any, extra_vars: dict[str, Any] | None = None) -> Any:
        """递归解析变量，支持 str/dict/list"""
        if isinstance(value, str):
            return self._resolve_string(value, extra_vars or {})
        elif isinstance(value, dict):
            return {k: self.resolve(v, extra_vars) for k, v in value.items()}
        elif isinstance(value, list):
            return [self.resolve(item, extra_vars) for item in value]
        return value

    def resolve_dict(self, data: dict, extra_vars: dict[str, Any] | None = None) -> dict:
        """解析 dict 中所有字符串值（headers 等场景）"""
        return {k: self._resolve_string(str(v), extra_vars or {}) for k, v in data.items()}

    def _resolve_string(self, s: str, extra_vars: dict[str, Any]) -> str:
        """替换 {{var}} 为对应值"""
        pattern = r"\{\{(\w+)\}\}"

        def replacer(match: re.Match) -> str:
            name = match.group(1)

            # 自定义变量优先
            if name in extra_vars:
                return str(extra_vars[name])

            # 内置变量
            if name in self.BUILTINS:
                return self.BUILTINS[name]()

            # {{index}} — 自增计数器
            if name == "index":
                self._counter += 1
                return str(self._counter)

            # {{seq:name}} — 命名计数器
            if name.startswith("seq_"):
                seq_name = name[4:]
                if seq_name not in self._seq_counters:
                    self._seq_counters[seq_name] = 0
                self._seq_counters[seq_name] += 1
                return str(self._seq_counters[seq_name])

            # 未匹配 — 保留原样
            return match.group(0)

        return re.sub(pattern, replacer, s)
