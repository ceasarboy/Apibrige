"""协议适配器抽象基类 — 所有协议适配器的统一接口"""
from abc import ABC, abstractmethod
from typing import Any


class ProtocolAdapter(ABC):
    """协议适配器基类

    所有协议（REST/SOAP/GraphQL/WebSocket/gRPC）的适配器 MUST 实现此接口。
    核心同步逻辑通过此接口调用，不直接依赖具体协议。
    """

    def __init__(self, config: dict):
        """初始化适配器

        Args:
            config: 连接配置，包含 base_url、auth_type、auth_config、headers、timeout_ms 等
        """
        self.config = config
        self.base_url = config.get("base_url", "")
        self.headers = config.get("headers", {})
        self.timeout = (config.get("timeout_ms", 30000)) / 1000

    @abstractmethod
    async def fetch_data(self, endpoint: str, params: dict | None = None) -> Any:
        """从源系统拉取数据

        Args:
            endpoint: 端点路径（REST 的 path、GraphQL 的 query、SOAP 的 operation 等）
            params: 请求参数

        Returns:
            统一格式的数据（dict 或 list）
        """
        ...

    @abstractmethod
    async def push_data(self, endpoint: str, data: Any, method: str = "POST") -> dict:
        """推送数据到目标系统

        Args:
            endpoint: 端点路径
            data: 要推送的数据
            method: 请求方法（REST 使用，其他协议可忽略）

        Returns:
            统一格式的响应: {"status_code": int, "body": Any}
        """
        ...

    @abstractmethod
    async def test_connection(self) -> dict:
        """测试连接可用性

        Returns:
            {"status": "ok"|"failed", "latency_ms": int, "error": str|None}
        """
        ...
