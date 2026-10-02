"""协议适配器单元测试 — 覆盖 REST/SOAP/GraphQL/WebSocket/gRPC/Dubbo + AuthInjector + Factory"""
import asyncio
import json
import sys
import types
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4


# ============================================================
# 工厂测试
# ============================================================

class TestProtocolAdapterFactory:
    """验证: 工厂能按协议类型创建适配器实例"""

    def test_create_rest_adapter(self):
        """验证: 传入 'rest' 返回 RestAdapter 实例"""
        from app.adapters.factory import ProtocolAdapterFactory
        from app.adapters.rest import RestAdapter

        adapter = ProtocolAdapterFactory.create("rest", {"base_url": "https://api.example.com"})
        assert isinstance(adapter, RestAdapter)
        assert adapter.base_url == "https://api.example.com"

    def test_create_http_alias(self):
        """验证: 'http' 是 'rest' 的别名"""
        from app.adapters.factory import ProtocolAdapterFactory
        from app.adapters.rest import RestAdapter

        adapter = ProtocolAdapterFactory.create("http", {"base_url": "https://api.example.com"})
        assert isinstance(adapter, RestAdapter)

    def test_create_unknown_protocol_raises(self):
        """验证: 未知协议抛出 ValueError"""
        from app.adapters.factory import ProtocolAdapterFactory

        with pytest.raises(ValueError, match="不支持的协议类型"):
            ProtocolAdapterFactory.create("unknown_protocol", {})

    def test_register_custom_adapter(self):
        """验证: 可注册自定义适配器"""
        from app.adapters.factory import ProtocolAdapterFactory
        from app.adapters.base import ProtocolAdapter

        class CustomAdapter(ProtocolAdapter):
            async def fetch_data(self, endpoint, params=None): return {}
            async def push_data(self, endpoint, data, method="POST"): return {}
            async def test_connection(self): return {}

        ProtocolAdapterFactory.register("custom_test", CustomAdapter)
        adapter = ProtocolAdapterFactory.create("custom_test", {})
        assert isinstance(adapter, CustomAdapter)

    def test_get_supported_protocols(self):
        """验证: 获取已注册协议列表"""
        from app.adapters.factory import ProtocolAdapterFactory

        protocols = ProtocolAdapterFactory.get_supported_protocols()
        assert "rest" in protocols
        assert "http" in protocols


# ============================================================
# AuthInjector 测试
# ============================================================

class TestAuthInjector:
    """验证: 各认证方式的 Header/Query 注入正确"""

    def test_no_auth(self):
        """无认证时原样返回"""
        from app.adapters.auth import AuthInjector

        auth = AuthInjector(None, None)
        headers, params = auth.inject({"Content-Type": "application/json"})
        assert headers == {"Content-Type": "application/json"}
        assert params == {}

    def test_api_key_in_header(self):
        """API Key 默认注入到 Header"""
        from app.adapters.auth import AuthInjector

        auth = AuthInjector("api_key", {"key_name": "X-API-Key", "key_value": "secret123"})
        headers, params = auth.inject({})
        assert headers["X-API-Key"] == "secret123"

    def test_api_key_in_query(self):
        """API Key 可配置注入到 Query 参数"""
        from app.adapters.auth import AuthInjector

        auth = AuthInjector("api_key", {
            "key_name": "api_key", "key_value": "secret123", "location": "query"
        })
        headers, params = auth.inject({})
        assert params["api_key"] == "secret123"
        assert "X-API-Key" not in headers

    def test_bearer_token(self):
        """Bearer Token 注入 Authorization 头"""
        from app.adapters.auth import AuthInjector

        auth = AuthInjector("bearer", {"token": "jwt_token_xyz"})
        headers, _ = auth.inject({})
        assert headers["Authorization"] == "Bearer jwt_token_xyz"

    def test_basic_auth(self):
        """Basic Auth 注入 base64 编码的凭证"""
        import base64
        from app.adapters.auth import AuthInjector

        auth = AuthInjector("basic", {"username": "admin", "password": "pass123"})
        headers, _ = auth.inject({})
        expected = base64.b64encode(b"admin:pass123").decode()
        assert headers["Authorization"] == f"Basic {expected}"

    @pytest.mark.asyncio
    async def test_oauth2_token_refresh(self):
        """OAuth2 Token 自动获取并缓存"""
        from app.adapters.auth import AuthInjector

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"access_token": "oauth_token", "expires_in": 3600})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            auth = AuthInjector("oauth2", {
                "token_url": "https://auth.example.com/token",
                "client_id": "cid",
                "client_secret": "csec",
                "scopes": "read",
            })
            headers, _ = await auth.inject_async({})
            assert headers["Authorization"] == "Bearer oauth_token"

    def test_sign_md5(self):
        """自定义签名（MD5）注入"""
        from app.adapters.auth import AuthInjector

        auth = AuthInjector("sign", {
            "algorithm": "md5",
            "secret_key": "my_secret",
            "sign_fields": ["timestamp"],
        })
        headers, _ = auth.inject({})
        assert "X-Sign" in headers
        assert "X-Timestamp" in headers

    def test_sign_hmac_sha256(self):
        """自定义签名（HMAC-SHA256）注入"""
        from app.adapters.auth import AuthInjector

        auth = AuthInjector("sign", {
            "algorithm": "hmac-sha256",
            "secret_key": "my_secret",
            "sign_fields": ["timestamp", "nonce"],
        })
        headers, _ = auth.inject({})
        assert "X-Sign" in headers
        assert "X-Timestamp" in headers
        assert "X-Nonce" in headers

    def test_unsupported_auth_type_raises(self):
        """不支持的认证类型抛出 ValueError"""
        from app.adapters.auth import AuthInjector

        auth = AuthInjector("unknown_auth", {})
        with pytest.raises(ValueError, match="不支持的认证类型"):
            auth.inject({})


# ============================================================
# 抽象基类测试
# ============================================================

class TestProtocolAdapterBase:
    """验证: 抽象类无法实例化，子类必须实现3个方法"""

    def test_cannot_instantiate_abstract(self):
        """抽象类无法实例化"""
        from app.adapters.base import ProtocolAdapter

        with pytest.raises(TypeError, match="abstract"):
            ProtocolAdapter({})

    def test_subclass_missing_method_raises(self):
        """子类未实现所有抽象方法无法实例化"""
        from app.adapters.base import ProtocolAdapter

        class IncompleteAdapter(ProtocolAdapter):
            async def fetch_data(self, endpoint, params=None): return {}

        with pytest.raises(TypeError):
            IncompleteAdapter({})

    def test_config_initialization(self):
        """配置正确初始化"""
        from app.adapters.base import ProtocolAdapter

        class CompleteAdapter(ProtocolAdapter):
            async def fetch_data(self, endpoint, params=None): return {}
            async def push_data(self, endpoint, data, method="POST"): return {}
            async def test_connection(self): return {}

        adapter = CompleteAdapter({
            "base_url": "https://api.example.com",
            "headers": {"X-Custom": "value"},
            "timeout_ms": 5000,
        })
        assert adapter.base_url == "https://api.example.com"
        assert adapter.headers == {"X-Custom": "value"}
        assert adapter.timeout == 5.0  # 5000ms -> 5s


# ============================================================
# REST 适配器测试
# ============================================================

class TestRestAdapter:
    """验证: REST 适配器 GET/POST 请求与认证注入"""

    @pytest.mark.asyncio
    async def test_fetch_data_get(self):
        """GET 请求拉取数据"""
        from app.adapters.rest import RestAdapter

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"id": 1, "name": "test"})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            adapter = RestAdapter({"base_url": "https://api.example.com"})
            result = await adapter.fetch_data("/users", {"page": 1})

        assert result == {"id": 1, "name": "test"}
        mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_push_data_post(self):
        """POST 请求推送数据"""
        from app.adapters.rest import RestAdapter

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.status_code = 201
        mock_response.content = b'{"id": 1}'
        mock_response.json = MagicMock(return_value={"id": 1})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.request = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            adapter = RestAdapter({"base_url": "https://api.example.com"})
            result = await adapter.push_data("/users", {"name": "张三"})

        assert result["status_code"] == 201
        assert result["body"] == {"id": 1}

    @pytest.mark.asyncio
    async def test_fetch_with_api_key_auth(self):
        """带 API Key 认证的请求"""
        from app.adapters.rest import RestAdapter

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client) as mock_cls:
            adapter = RestAdapter({
                "base_url": "https://api.example.com",
                "auth_type": "api_key",
                "auth_config": {"key_name": "X-API-Key", "key_value": "secret"},
            })
            await adapter.fetch_data("/users")

        # 验证 headers 中包含 API Key
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["headers"]["X-API-Key"] == "secret"

    @pytest.mark.asyncio
    async def test_test_connection_ok(self):
        """测试连接成功"""
        from app.adapters.rest import RestAdapter

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            adapter = RestAdapter({"base_url": "https://api.example.com"})
            result = await adapter.test_connection()

        assert result["status"] == "ok"
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_test_connection_failed(self):
        """测试连接失败"""
        from app.adapters.rest import RestAdapter

        with patch("httpx.AsyncClient", side_effect=Exception("Connection refused")):
            adapter = RestAdapter({"base_url": "https://invalid.example.com"})
            result = await adapter.test_connection()

        assert result["status"] == "failed"
        assert "Connection refused" in result["error"]


# ============================================================
# GraphQL 适配器测试
# ============================================================

class TestGraphqlAdapter:
    """验证: GraphQL Query/Mutation 请求"""

    @pytest.mark.asyncio
    async def test_fetch_data_query(self):
        """执行 GraphQL Query"""
        from app.adapters.graphql import GraphqlAdapter

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"data": {"users": [{"id": 1}]}})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            adapter = GraphqlAdapter({"base_url": "https://api.example.com"})
            result = await adapter.fetch_data("/graphql", {"query": "{ users { id } }"})

        assert result == {"users": [{"id": 1}]}

    @pytest.mark.asyncio
    async def test_fetch_data_missing_query_raises(self):
        """缺少 query 参数抛出 ValueError"""
        from app.adapters.graphql import GraphqlAdapter

        adapter = GraphqlAdapter({"base_url": "https://api.example.com"})
        with pytest.raises(ValueError, match="params\\['query'\\]"):
            await adapter.fetch_data("/graphql", {})

    @pytest.mark.asyncio
    async def test_fetch_data_with_errors_raises(self):
        """GraphQL 返回 errors 时抛出 RuntimeError"""
        from app.adapters.graphql import GraphqlAdapter

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={
            "data": None,
            "errors": [{"message": "Unauthorized"}]
        })

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            adapter = GraphqlAdapter({"base_url": "https://api.example.com"})
            with pytest.raises(RuntimeError, match="GraphQL 返回错误"):
                await adapter.fetch_data("/graphql", {"query": "query { invalid }"})

    @pytest.mark.asyncio
    async def test_push_data_mutation(self):
        """执行 GraphQL Mutation"""
        from app.adapters.graphql import GraphqlAdapter

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"data": {"createUser": {"id": 1}}})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            adapter = GraphqlAdapter({"base_url": "https://api.example.com"})
            result = await adapter.push_data("/graphql", {
                "query": "mutation { createUser { id } }",
                "variables": {"name": "张三"},
            })

        assert result["status_code"] == 200
        assert result["body"]["data"]["createUser"]["id"] == 1

    @pytest.mark.asyncio
    async def test_push_data_string_query(self):
        """data 为字符串时直接作为 query"""
        from app.adapters.graphql import GraphqlAdapter

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"data": {}})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            adapter = GraphqlAdapter({"base_url": "https://api.example.com"})
            await adapter.push_data("/graphql", "mutation { ok }")


# ============================================================
# SOAP 适配器测试（mock zeep）
# ============================================================

class TestSoapAdapter:
    """验证: SOAP 适配器调用 zeep 客户端"""

    @pytest.mark.asyncio
    async def test_fetch_data_calls_operation(self):
        """调用 SOAP 操作"""
        # 使用 sys.modules 注入 fake zeep 模块
        fake_zeep = types.ModuleType("zeep")
        fake_client = MagicMock()
        fake_client.service.GetUser = MagicMock(return_value={"id": 1, "name": "张三"})
        fake_zeep.Client = MagicMock(return_value=fake_client)

        fake_transport = types.ModuleType("zeep.transports")
        fake_transport.Transport = MagicMock()
        fake_zeep.transports = fake_transport

        fake_requests = types.ModuleType("requests")
        fake_requests.Session = MagicMock(return_value=MagicMock(headers={}))

        with patch.dict(sys.modules, {
            "zeep": fake_zeep,
            "zeep.transports": fake_transport,
            "requests": fake_requests,
        }):
            # 延迟导入以应用 mock
            from app.adapters.soap import SoapAdapter
            adapter = SoapAdapter({"base_url": "http://example.com/service?wsdl"})
            result = await adapter.fetch_data("GetUser", {"userId": 1})

        assert result == {"id": 1, "name": "张三"}

    @pytest.mark.asyncio
    async def test_push_data(self):
        """SOAP push 调用操作"""
        fake_zeep = types.ModuleType("zeep")
        fake_client = MagicMock()
        fake_client.service.CreateUser = MagicMock(return_value={"success": True})
        fake_zeep.Client = MagicMock(return_value=fake_client)

        fake_transport = types.ModuleType("zeep.transports")
        fake_transport.Transport = MagicMock()
        fake_zeep.transports = fake_transport

        fake_requests = types.ModuleType("requests")
        fake_requests.Session = MagicMock(return_value=MagicMock(headers={}))

        with patch.dict(sys.modules, {
            "zeep": fake_zeep,
            "zeep.transports": fake_transport,
            "requests": fake_requests,
        }):
            from app.adapters.soap import SoapAdapter
            adapter = SoapAdapter({"base_url": "http://example.com/service?wsdl"})
            result = await adapter.push_data("CreateUser", {"name": "张三"})

        assert result["status_code"] == 200
        assert result["body"] == {"success": True}


# ============================================================
# WebSocket 适配器测试（mock websockets）
# ============================================================

class TestWebsocketAdapter:
    """验证: WebSocket 适配器连接→接收→断开流程"""

    @pytest.mark.asyncio
    async def test_fetch_data_single_message(self):
        """接收单条消息返回 dict"""
        fake_websockets = types.ModuleType("websockets")
        fake_ws = AsyncMock()
        fake_ws.recv = AsyncMock(return_value='{"event": "data"}')
        fake_ws.send = AsyncMock()

        class FakeConnect:
            def __init__(self, *args, **kwargs):
                self.ws = fake_ws

            async def __aenter__(self):
                return self.ws

            async def __aexit__(self, *args):
                return None

        fake_websockets.connect = FakeConnect

        with patch.dict(sys.modules, {"websockets": fake_websockets}):
            from app.adapters.websocket import WebsocketAdapter
            adapter = WebsocketAdapter({"base_url": "ws://localhost:8080/stream"})
            result = await adapter.fetch_data("")

        assert result == {"event": "data"}

    @pytest.mark.asyncio
    async def test_fetch_data_multiple_messages(self):
        """接收多条消息返回 list"""
        fake_websockets = types.ModuleType("websockets")
        fake_ws = AsyncMock()
        fake_ws.recv = AsyncMock(side_effect=['{"a": 1}', '{"b": 2}'])
        fake_ws.send = AsyncMock()

        class FakeConnect:
            def __init__(self, *args, **kwargs): self.ws = fake_ws
            async def __aenter__(self): return self.ws
            async def __aexit__(self, *args): return None

        fake_websockets.connect = FakeConnect

        with patch.dict(sys.modules, {"websockets": fake_websockets}):
            from app.adapters.websocket import WebsocketAdapter
            adapter = WebsocketAdapter({"base_url": "ws://localhost:8080/"})
            result = await adapter.fetch_data("", {"max_messages": 2})

        assert result == [{"a": 1}, {"b": 2}]

    @pytest.mark.asyncio
    async def test_fetch_data_with_payload(self):
        """建立连接后发送订阅消息"""
        fake_websockets = types.ModuleType("websockets")
        fake_ws = AsyncMock()
        fake_ws.recv = AsyncMock(return_value='{"subscribed": true}')
        fake_ws.send = AsyncMock()

        class FakeConnect:
            def __init__(self, *args, **kwargs): self.ws = fake_ws
            async def __aenter__(self): return self.ws
            async def __aexit__(self, *args): return None

        fake_websockets.connect = FakeConnect

        with patch.dict(sys.modules, {"websockets": fake_websockets}):
            from app.adapters.websocket import WebsocketAdapter
            adapter = WebsocketAdapter({"base_url": "ws://localhost:8080/"})
            await adapter.fetch_data("", {"payload": {"subscribe": "topic1"}})

        fake_ws.send.assert_called_once_with('{"subscribe": "topic1"}')

    @pytest.mark.asyncio
    async def test_push_data(self):
        """推送消息并等待响应"""
        fake_websockets = types.ModuleType("websockets")
        fake_ws = AsyncMock()
        fake_ws.recv = AsyncMock(return_value='{"ok": true}')
        fake_ws.send = AsyncMock()

        class FakeConnect:
            def __init__(self, *args, **kwargs): self.ws = fake_ws
            async def __aenter__(self): return self.ws
            async def __aexit__(self, *args): return None

        fake_websockets.connect = FakeConnect

        with patch.dict(sys.modules, {"websockets": fake_websockets}):
            from app.adapters.websocket import WebsocketAdapter
            adapter = WebsocketAdapter({"base_url": "ws://localhost:8080/"})
            result = await adapter.push_data("", {"action": "update"})

        assert result["status_code"] == 200
        assert result["body"] == {"ok": True}
        fake_ws.send.assert_called_once_with('{"action": "update"}')


# ============================================================
# gRPC 适配器测试（mock grpc）
# ============================================================

class TestGrpcAdapter:
    """验证: gRPC 适配器调用逻辑"""

    @pytest.mark.asyncio
    async def test_test_connection_ok(self):
        """测试 gRPC 端点可达性"""
        fake_grpc = types.ModuleType("grpc")
        fake_channel = AsyncMock()
        fake_channel.channel_ready = AsyncMock()
        fake_channel.close = AsyncMock()
        fake_grpc.aio = MagicMock()
        fake_grpc.aio.insecure_channel = MagicMock(return_value=fake_channel)
        fake_grpc.aio.secure_channel = MagicMock(return_value=fake_channel)
        fake_grpc.ssl_channel_credentials = MagicMock()

        with patch.dict(sys.modules, {"grpc": fake_grpc}):
            from app.adapters.grpc_adapter import GrpcAdapter
            adapter = GrpcAdapter({"base_url": "localhost:50051"})
            result = await adapter.test_connection()

        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_test_connection_failed(self):
        """测试 gRPC 端点不可达"""
        fake_grpc = types.ModuleType("grpc")
        fake_channel = AsyncMock()
        fake_channel.channel_ready = AsyncMock(side_effect=asyncio.TimeoutError())
        fake_channel.close = AsyncMock()
        fake_grpc.aio = MagicMock()
        fake_grpc.aio.insecure_channel = MagicMock(return_value=fake_channel)

        with patch.dict(sys.modules, {"grpc": fake_grpc}):
            from app.adapters.grpc_adapter import GrpcAdapter
            adapter = GrpcAdapter({"base_url": "localhost:99999"})
            result = await adapter.test_connection()

        assert result["status"] == "failed"


# ============================================================
# Dubbo 适配器测试（gateway 模式 mock httpx）
# ============================================================

class TestDubboAdapter:
    """验证: Dubbo 网关模式调用"""

    @pytest.mark.asyncio
    async def test_gateway_fetch_data(self):
        """gateway 模式拉取数据"""
        from app.adapters.dubbo import DubboAdapter

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"id": 1, "name": "张三"})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client) as mock_cls:
            adapter = DubboAdapter({
                "base_url": "http://dubbo-gateway:8001",
                "service": "com.example.UserService",
                "group": "production",
                "version": "1.0.0",
            })
            result = await adapter.fetch_data("getUserById", {"args": [1]})

        assert result == {"id": 1, "name": "张三"}
        # 验证 group/version 注入到 headers
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["headers"]["X-Dubbo-Group"] == "production"
        assert call_kwargs["headers"]["X-Dubbo-Version"] == "1.0.0"

    @pytest.mark.asyncio
    async def test_gateway_push_data_dict(self):
        """gateway 模式推送数据（dict 形式）"""
        from app.adapters.dubbo import DubboAdapter

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"success": True})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            adapter = DubboAdapter({
                "base_url": "http://dubbo-gateway:8001",
                "service": "com.example.UserService",
            })
            result = await adapter.push_data("createUser", {"args": [{"name": "张三"}]})

        assert result["status_code"] == 200
        assert result["body"] == {"success": True}

    @pytest.mark.asyncio
    async def test_gateway_push_data_list(self):
        """gateway 模式推送数据（list 形式，直接作为 args）"""
        from app.adapters.dubbo import DubboAdapter

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"success": True})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client) as mock_cls:
            adapter = DubboAdapter({
                "base_url": "http://dubbo-gateway:8001",
                "service": "com.example.UserService",
            })
            await adapter.push_data("createUser", [{"name": "张三"}])

        # 验证 POST body 包含 args
        call_args = mock_client.post.call_args
        body = call_args.kwargs["json"]
        assert body["args"] == [{"name": "张三"}]

    @pytest.mark.asyncio
    async def test_gateway_url_construction(self):
        """验证 URL 构造: {base_url}/{service}/{method}"""
        from app.adapters.dubbo import DubboAdapter

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            adapter = DubboAdapter({
                "base_url": "http://dubbo-gateway:8001/",
                "service": "com.example.UserService",
            })
            await adapter.fetch_data("getUserById", {"args": [1]})

        call_args = mock_client.post.call_args
        url = call_args.args[0] if call_args.args else call_args.kwargs.get("url")
        assert url == "http://dubbo-gateway:8001/com.example.UserService/getUserById"

    @pytest.mark.asyncio
    async def test_gateway_test_connection_ok(self):
        """gateway 模式测试连接"""
        from app.adapters.dubbo import DubboAdapter

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            adapter = DubboAdapter({"base_url": "http://dubbo-gateway:8001"})
            result = await adapter.test_connection()

        assert result["status"] == "ok"
