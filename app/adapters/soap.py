"""SOAP 协议适配器 — 基于 zeep，通过 asyncio.to_thread 包装同步调用"""
import asyncio
import time
from typing import Any
from app.adapters.base import ProtocolAdapter
from app.adapters.auth import AuthInjector


class SoapAdapter(ProtocolAdapter):
    """SOAP 协议适配器

    使用 zeep 解析 WSDL 并调用操作。
    zeep 为同步库，通过 asyncio.to_thread 包装为异步调用。

    配置约定:
        config["base_url"]: WSDL URL
        config["operation"]: 默认操作名（可被 endpoint 参数覆盖）
        config["service"]: 服务名（多服务 WSDL 时指定，可选）
        config["port"]: 端口名（可选）
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.auth = AuthInjector(
            config.get("auth_type"),
            config.get("auth_config"),
        )
        self.wsdl = config.get("base_url", "")
        self.service = config.get("service")
        self.port = config.get("port")
        self._client = None  # 延迟初始化 zeep Client

    async def _get_client(self):
        """延迟创建并缓存 zeep Client（线程安全）"""
        if self._client is None:
            from zeep import Client
            from zeep.transports import Transport
            import requests

            session = requests.Session()
            # 注入认证到 requests session headers
            headers, _ = self.auth.inject(dict(self.headers))
            session.headers.update(headers)

            transport = Transport(timeout=self.timeout, operation_timeout=self.timeout, session=session)
            # zeep Client 创建是同步阻塞操作，在线程池中执行
            self._client = await asyncio.to_thread(Client, self.wsdl, transport=transport)
        return self._client

    async def fetch_data(self, endpoint: str, params: dict | None = None) -> Any:
        """调用 SOAP 操作拉取数据

        Args:
            endpoint: SOAP 操作名（operation）
            params: 操作参数 dict
        """
        client = await self._get_client()
        operation = endpoint
        kwargs = params or {}

        def _call():
            if self.service and self.port:
                service = client.bind(self.service, self.port)
                return getattr(service, operation)(**kwargs)
            return getattr(client.service, operation)(**kwargs)

        return await asyncio.to_thread(_call)

    async def push_data(self, endpoint: str, data: Any, method: str = "POST") -> dict:
        """调用 SOAP 操作推送数据

        Args:
            endpoint: SOAP 操作名
            data: 参数 dict
            method: 忽略（SOAP 不使用 HTTP method）
        """
        result = await self.fetch_data(endpoint, data if isinstance(data, dict) else {"data": data})
        return {"status_code": 200, "body": result}

    async def test_connection(self) -> dict:
        """测试 WSDL 可达性"""
        start = time.time()
        try:
            await self._get_client()
            # 尝试列出操作验证 WSDL 解析成功
            if self._client is not None:
                latency = int((time.time() - start) * 1000)
                return {"status": "ok", "latency_ms": latency, "error": None}
            return {"status": "failed", "latency_ms": latency, "error": "客户端未初始化"}
        except Exception as e:
            return {"status": "failed", "latency_ms": int((time.time() - start) * 1000), "error": str(e)}
