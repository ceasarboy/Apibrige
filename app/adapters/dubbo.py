"""Dubbo 协议适配器 — 通过 Dubbo HTTP 网关或 Triple 协议接入 Dubbo 服务

实现策略：
    Python 无法直接调用 Dubbo RPC（需 Java 生态），采用业界通用方案：
    1. **HTTP 网关模式（默认）**：通过 dubbo-go-pixiu / Nginx-dubbo-module / dubbo-proxy
       将 Dubbo 服务暴露为 HTTP 接口，Python 端通过 HTTP/JSON 调用。
       网关负责将 HTTP 请求转换为 Dubbo RPC 调用，并将响应转回 JSON。
    2. **Triple 协议模式**：Dubbo 3.x 引入的 Triple 协议基于 HTTP/2 + Protobuf，
       与 gRPC 兼容。可通过 HTTP/2 直接调用，或复用 GrpcAdapter。

配置约定（HTTP 网关模式）:
    config["base_url"]: 网关地址（如 http://dubbo-gateway:8001）
    config["mode"]: "gateway"（默认）或 "triple"
    config["service"]: Dubbo 服务接口全名（如 com.example.UserService）
    config["group"]: 服务分组（可选）
    config["version"]: 服务版本（可选）

配置约定（Triple 模式）:
    config["base_url"]: Triple 服务地址（host:port）
    config["mode"]: "triple"
    config["proto_content"]: .proto 文件内容
    config["service_name"]: gRPC 服务名
    复用 GrpcAdapter 的调用逻辑
"""
import time
from typing import Any
import httpx
from app.adapters.base import ProtocolAdapter
from app.adapters.auth import AuthInjector


class DubboAdapter(ProtocolAdapter):
    """Dubbo 协议适配器

    支持两种模式:
        1. gateway: 通过 HTTP 网关调用 Dubbo 服务（推荐，无需 Java 依赖）
        2. triple: 通过 Dubbo 3.x Triple 协议（HTTP/2）直接调用

    网关请求约定（HTTP 网关模式）:
        POST {base_url}/{service}/{method}
        Body: {"args": [...], "paramTypes": [...]}
        或简单形式: {"args": [...]}（自动推断类型）
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.auth = AuthInjector(
            config.get("auth_type"),
            config.get("auth_config"),
        )
        self.mode = config.get("mode", "gateway")
        self.service = config.get("service", "")
        self.group = config.get("group")
        self.version = config.get("version")
        self._triple_adapter = None  # Triple 模式下复用 GrpcAdapter

        if self.mode == "triple":
            self._init_triple_adapter(config)

    def _init_triple_adapter(self, config: dict):
        """初始化 Triple 模式（复用 GrpcAdapter）"""
        from app.adapters.grpc_adapter import GrpcAdapter
        # Triple 协议与 gRPC 在传输层兼容，复用 GrpcAdapter
        self._triple_adapter = GrpcAdapter(config)

    def _build_gateway_url(self, endpoint: str) -> str:
        """构造 HTTP 网关 URL: {base_url}/{service}/{method}"""
        method = endpoint.lstrip("/")
        base = self.base_url.rstrip("/")
        return f"{base}/{self.service}/{method}"

    def _build_gateway_headers(self) -> dict:
        """构造网关请求头（携带 group/version 元数据）"""
        headers = dict(self.headers)
        headers["Content-Type"] = "application/json"
        if self.group:
            headers["X-Dubbo-Group"] = self.group
        if self.version:
            headers["X-Dubbo-Version"] = self.version
        return headers

    async def _gateway_call(self, method: str, args: list, param_types: list | None = None) -> Any:
        """通过 HTTP 网关调用 Dubbo 方法"""
        headers, _ = await self.auth.inject_async(self._build_gateway_headers())
        url = self._build_gateway_url(method)

        body: dict = {"args": args}
        if param_types:
            body["paramTypes"] = param_types

        async with httpx.AsyncClient(
            headers=headers, timeout=self.timeout
        ) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            return resp.json()

    async def fetch_data(self, endpoint: str, params: dict | None = None) -> Any:
        """调用 Dubbo 方法拉取数据

        Args:
            endpoint: 方法名（如 getUserById）
            params:
                params["args"]: 参数列表（位置参数）
                params["param_types"]: 参数类型全名列表（可选，网关模式用于精确匹配重载）
        """
        if self.mode == "triple" and self._triple_adapter:
            return await self._triple_adapter.fetch_data(endpoint, params)

        params = params or {}
        args = params.get("args", [])
        param_types = params.get("param_types")
        return await self._gateway_call(endpoint, args, param_types)

    async def push_data(self, endpoint: str, data: Any, method: str = "POST") -> dict:
        """调用 Dubbo 方法推送数据

        data 可以是:
            - dict: {"args": [...], "param_types": [...]}
            - list: 直接作为 args
            - 其他: 包装为单参数调用 [data]
        """
        if self.mode == "triple" and self._triple_adapter:
            return await self._triple_adapter.push_data(endpoint, data, method)

        if isinstance(data, dict) and "args" in data:
            args = data["args"]
            param_types = data.get("param_types")
        elif isinstance(data, list):
            args = data
            param_types = None
        else:
            args = [data]
            param_types = None

        result = await self._gateway_call(endpoint, args, param_types)
        return {"status_code": 200, "body": result}

    async def test_connection(self) -> dict:
        """测试 Dubbo 服务可达性

        gateway 模式: 探测网关健康端点
        triple 模式: 复用 GrpcAdapter.test_connection
        """
        if self.mode == "triple" and self._triple_adapter:
            return await self._triple_adapter.test_connection()

        start = time.time()
        try:
            headers, _ = await self.auth.inject_async(self._build_gateway_headers())
            async with httpx.AsyncClient(headers=headers, timeout=5) as client:
                # 探测网关健康端点
                resp = await client.get(f"{self.base_url.rstrip('/')}/health")
                latency = int((time.time() - start) * 1000)
                if resp.status_code < 400:
                    return {"status": "ok", "latency_ms": latency, "error": None}
                hint = ("鉴权失败（检查认证配置）" if resp.status_code in (401, 403)
                        else "探测端点不存在或不可达" if resp.status_code == 404
                        else "目标返回客户端错误")
                return {"status": "failed", "latency_ms": latency,
                        "error": f"HTTP {resp.status_code}：{hint}"}
        except Exception as e:
            return {"status": "failed", "latency_ms": int((time.time() - start) * 1000), "error": str(e)}
