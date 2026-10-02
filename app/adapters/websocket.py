"""WebSocket 协议适配器 — 基于 websockets 库，拉取后断开模式"""
import asyncio
import json
import time
from typing import Any
from app.adapters.base import ProtocolAdapter
from urllib.parse import urlencode

from app.adapters.auth import AuthInjector


class WebsocketAdapter(ProtocolAdapter):
    """WebSocket 协议适配器

    设计约定：WebSocket 仅用于 fetch（建立连接→接收消息→断开），
    不做持久连接管理。push 通过向服务端发送消息实现。

    配置约定:
        config["base_url"]: ws:// 或 wss:// URL（如 ws://localhost:8080/stream）
        config["max_messages"]: fetch 时最多接收的消息数（默认 1）
        config["wait_timeout"]: 接收消息等待超时秒数（默认 30）
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.auth = AuthInjector(
            config.get("auth_type"),
            config.get("auth_config"),
        )
        self.max_messages = config.get("max_messages", 1)
        self.wait_timeout = config.get("wait_timeout", 30.0)

    def _build_url(self, endpoint: str) -> str:
        """构造完整 WebSocket URL"""
        if not endpoint:
            return self.base_url
        # 简单拼接，避免重复斜杠
        if self.base_url.endswith("/"):
            return f"{self.base_url}{endpoint.lstrip('/')}"
        return f"{self.base_url}/{endpoint.lstrip('/')}"

    async def _inject_auth_to_query(self) -> tuple[dict, dict]:
        """注入认证，返回 (extra_headers, query_params)"""
        headers, params = await self.auth.inject_async(self.headers)
        return headers, params

    async def fetch_data(self, endpoint: str, params: dict | None = None) -> Any:
        """连接 WebSocket，接收 N 条消息后断开

        Args:
            endpoint: URL 路径（拼接到 base_url 后）
            params: 可选参数
                params["max_messages"]: 本次接收的消息数（覆盖配置）
                params["payload"]: 建立连接后先发送的消息（订阅请求）

        Returns:
            单条消息返回 dict/list，多条消息返回 list
        """
        import websockets

        params = params or {}
        max_msgs = params.get("max_messages", self.max_messages)
        payload = params.get("payload")

        headers, query_params = await self._inject_auth_to_query()
        url = self._build_url(endpoint)
        # query 型凭据（api_key location=query）必须拼进握手 URL——原实现取完即丢，
        # 握手无凭据认证失败（审查 P2）
        if query_params:
            url = url + ("&" if "?" in url else "?") + urlencode(query_params)

        messages: list = []
        async with websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=self.wait_timeout,
            close_timeout=5,
        ) as ws:
            if payload is not None:
                await ws.send(json.dumps(payload) if not isinstance(payload, str) else payload)

            for _ in range(max_msgs):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.wait_timeout)
                except asyncio.TimeoutError:
                    break
                try:
                    messages.append(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    messages.append(raw)

        if len(messages) == 1:
            return messages[0]
        return messages

    async def push_data(self, endpoint: str, data: Any, method: str = "POST") -> dict:
        """连接 WebSocket，发送一条消息，可选等待响应后断开"""
        import websockets

        headers, query_params = await self._inject_auth_to_query()
        url = self._build_url(endpoint)
        if query_params:
            url = url + ("&" if "?" in url else "?") + urlencode(query_params)

        async with websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=self.wait_timeout,
            close_timeout=5,
        ) as ws:
            payload = json.dumps(data) if not isinstance(data, str) else data
            await ws.send(payload)
            # 尝试接收一条响应（带超时，无响应视为单向推送）
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self.wait_timeout)
                try:
                    body = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    body = raw
            except asyncio.TimeoutError:
                body = None

        return {"status_code": 200, "body": body}

    async def test_connection(self) -> dict:
        """测试 WebSocket 端点可达性（仅建立连接）"""
        import websockets

        start = time.time()
        try:
            headers, query_params = await self._inject_auth_to_query()
            url = self.base_url
            if query_params:   # 与 fetch/push 同口径：query 型凭据拼进握手 URL
                url = url + ("&" if "?" in url else "?") + urlencode(query_params)
            async with websockets.connect(
                url,
                additional_headers=headers,
                open_timeout=5,
                close_timeout=2,
            ) as ws:
                latency = int((time.time() - start) * 1000)
                # 连接成功即视为可达
                return {"status": "ok", "latency_ms": latency, "error": None}
        except Exception as e:
            return {"status": "failed", "latency_ms": int((time.time() - start) * 1000), "error": str(e)}
