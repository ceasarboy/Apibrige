"""GraphQL 协议适配器 — 基于 httpx 直接发送 GraphQL 请求"""
import time
import httpx
from typing import Any
from app.adapters.base import ProtocolAdapter
from app.adapters.auth import AuthInjector


class GraphqlAdapter(ProtocolAdapter):
    """GraphQL 协议适配器

    使用 httpx 直接发送 GraphQL Query/Mutation 请求到 /graphql 端点。
    不引入 gql 库依赖，保持轻量；httpx 已是 REST 适配器的依赖。

    配置约定:
        config["base_url"]: GraphQL 服务根 URL（如 https://api.example.com）
        endpoint 参数: GraphQL 路径（默认 /graphql）

    fetch_data(endpoint, params):
        params["query"]: GraphQL 查询字符串（必填）
        params["variables"]: 变量 dict（可选）
        params["operation_name"]: 操作名（可选）

    push_data(endpoint, data, method):
        data["query"]: Mutation 字符串
        data["variables"]: 变量 dict
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.auth = AuthInjector(
            config.get("auth_type"),
            config.get("auth_config"),
        )

    async def _post(self, endpoint: str, payload: dict) -> dict:
        """发送 GraphQL 请求"""
        headers, _ = await self.auth.inject_async(self.headers)
        path = endpoint or "/graphql"
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=self.timeout
        ) as client:
            resp = await client.post(path, json=payload)
            resp.raise_for_status()
            return resp.json()

    async def fetch_data(self, endpoint: str, params: dict | None = None) -> Any:
        """执行 GraphQL Query"""
        params = params or {}
        query = params.get("query")
        if not query:
            raise ValueError("GraphQL fetch_data 需要 params['query']")

        payload = {"query": query}
        if "variables" in params:
            payload["variables"] = params["variables"]
        if "operation_name" in params:
            payload["operationName"] = params["operation_name"]

        result = await self._post(endpoint, payload)
        if "errors" in result and result["errors"]:
            raise RuntimeError(f"GraphQL 返回错误: {result['errors']}")
        return result.get("data")

    async def push_data(self, endpoint: str, data: Any, method: str = "POST") -> dict:
        """执行 GraphQL Mutation

        data 可以是:
            - dict: {"query": "...", "variables": {...}}
            - str: 直接作为 query 字符串
        """
        if isinstance(data, str):
            payload = {"query": data}
        elif isinstance(data, dict):
            payload = {"query": data.get("query")}
            if "variables" in data:
                payload["variables"] = data["variables"]
            if "operation_name" in data:
                payload["operationName"] = data["operation_name"]
        else:
            raise ValueError("GraphQL push_data 的 data 必须是 str 或 dict")

        result = await self._post(endpoint, payload)
        return {"status_code": 200, "body": result}

    async def test_connection(self) -> dict:
        """测试 GraphQL 端点可达性（发送 introspection __typename 查询）"""
        start = time.time()
        try:
            result = await self._post("/graphql", {"query": "{ __typename }"})
            latency = int((time.time() - start) * 1000)
            if "data" in result:
                return {"status": "ok", "latency_ms": latency, "error": None}
            return {"status": "failed", "latency_ms": latency, "error": f"响应格式异常: {result}"}
        except Exception as e:
            return {"status": "failed", "latency_ms": int((time.time() - start) * 1000), "error": str(e)}
