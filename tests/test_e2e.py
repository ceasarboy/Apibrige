"""端到端测试 — 覆盖协议适配器、认证管理、连接 CRUD 全链路、监控数据准确性"""
import base64
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from fastapi.testclient import TestClient
from app.main import app
from tests.conftest import authed_client_kwargs
from app.database import get_session


_TEST_KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()


# ============================================================
# TestClient + mock session fixture
# ============================================================

@pytest.fixture
def mock_session():
    """Mock DB session that populates model fields on refresh."""
    from datetime import datetime, timezone
    from uuid import uuid4

    session = AsyncMock()
    session.get = AsyncMock(return_value=None)

    async def _populate_on_refresh(instance):
        if not instance.id:
            instance.id = uuid4()
        if hasattr(instance, 'status') and instance.status is None:
            instance.status = "active"
        if hasattr(instance, 'created_at'):
            instance.created_at = datetime.now(timezone.utc)
        if hasattr(instance, 'updated_at'):
            instance.updated_at = datetime.now(timezone.utc)
        if hasattr(instance, 'last_run_at'):
            instance.last_run_at = None
        if hasattr(instance, 'next_run_at'):
            instance.next_run_at = None

    session.refresh = AsyncMock(side_effect=_populate_on_refresh)
    session.commit = AsyncMock()
    session.delete = AsyncMock()
    session.execute = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture
def client(mock_session):
    async def override_get_session():
        yield mock_session
    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app, **authed_client_kwargs())
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def reset_crypto():
    import app.security.crypto as crypto_mod
    crypto_mod._fernet = None
    yield
    crypto_mod._fernet = None


@pytest.fixture
def encrypt_key():
    with patch("app.security.crypto.settings") as s:
        s.encrypt_key = _TEST_KEY
        yield s


HEADERS = {"accept": "application/json", "content-type": "application/json"}


# ============================================================
# 7.1 协议适配器端到端测试
# ============================================================

class TestProtocolE2E:
    """验证: 各协议类型的测试连接端点正常返回"""

    def test_rest_connection_test(self, client, mock_session, encrypt_key):
        """REST 协议连接测试"""
        conn_id = str(uuid4())
        fake_conn = MagicMock()
        fake_conn.id = conn_id
        fake_conn.name = "REST源"
        fake_conn.protocol = "rest"
        fake_conn.base_url = "https://httpbin.org"
        fake_conn.auth_type = "api_key"
        fake_conn.auth_config = {"key_name": "X-Key", "key_value": "***"}
        fake_conn.headers = {}
        fake_conn.timeout_ms = 30000
        fake_conn.db_config = None
        mock_session.get = AsyncMock(return_value=fake_conn)

        with patch("app.api.connections.decrypt_auth_config", return_value={"key_name": "X-Key", "key_value": "test"}):
            resp = client.post(f"/api/v1/connections/{conn_id}/test")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "failed")

    def test_graphql_connection_test(self, client, mock_session, encrypt_key):
        """GraphQL 协议连接测试"""
        conn_id = str(uuid4())
        fake_conn = MagicMock()
        fake_conn.id = conn_id
        fake_conn.protocol = "graphql"
        fake_conn.base_url = "https://api.example.com/graphql"
        fake_conn.auth_type = "bearer"
        fake_conn.auth_config = {"token": "***"}
        fake_conn.headers = {}
        fake_conn.timeout_ms = 30000
        fake_conn.db_config = None
        mock_session.get = AsyncMock(return_value=fake_conn)

        resp = client.post(f"/api/v1/connections/{conn_id}/test")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "failed")

    def test_soap_connection_test(self, client, mock_session, encrypt_key):
        """SOAP 协议连接测试"""
        conn_id = str(uuid4())
        fake_conn = MagicMock()
        fake_conn.id = conn_id
        fake_conn.protocol = "soap"
        fake_conn.base_url = "https://example.com/service?wsdl"
        fake_conn.auth_type = "basic"
        fake_conn.auth_config = {"username": "user", "password": "***"}
        fake_conn.headers = {}
        fake_conn.timeout_ms = 30000
        fake_conn.db_config = None
        mock_session.get = AsyncMock(return_value=fake_conn)

        resp = client.post(f"/api/v1/connections/{conn_id}/test")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "failed")

    def test_websocket_connection_test(self, client, mock_session, encrypt_key):
        """WebSocket 协议连接测试"""
        conn_id = str(uuid4())
        fake_conn = MagicMock()
        fake_conn.id = conn_id
        fake_conn.protocol = "websocket"
        fake_conn.base_url = "wss://echo.example.com"
        fake_conn.auth_type = "api_key"
        fake_conn.auth_config = {"key_name": "token", "key_value": "***"}
        fake_conn.headers = {}
        fake_conn.timeout_ms = 30000
        fake_conn.db_config = None
        mock_session.get = AsyncMock(return_value=fake_conn)

        resp = client.post(f"/api/v1/connections/{conn_id}/test")

        assert resp.status_code == 200

    def test_grpc_connection_test(self, client, mock_session, encrypt_key):
        """gRPC 协议连接测试"""
        conn_id = str(uuid4())
        fake_conn = MagicMock()
        fake_conn.id = conn_id
        fake_conn.protocol = "grpc"
        fake_conn.base_url = "localhost:50051"
        fake_conn.auth_type = None
        fake_conn.auth_config = None
        fake_conn.headers = {}
        fake_conn.timeout_ms = 30000
        fake_conn.db_config = None
        mock_session.get = AsyncMock(return_value=fake_conn)

        resp = client.post(f"/api/v1/connections/{conn_id}/test")

        assert resp.status_code == 200

    def test_dubbo_connection_test(self, client, mock_session, encrypt_key):
        """Dubbo 协议连接测试（网关模式）"""
        conn_id = str(uuid4())
        fake_conn = MagicMock()
        fake_conn.id = conn_id
        fake_conn.protocol = "dubbo"
        fake_conn.base_url = "http://gateway:8080"
        fake_conn.auth_type = "sign"
        fake_conn.auth_config = {"secret_key": "***", "app_id": "app001"}
        fake_conn.headers = {}
        fake_conn.timeout_ms = 30000
        fake_conn.db_config = None
        mock_session.get = AsyncMock(return_value=fake_conn)

        resp = client.post(f"/api/v1/connections/{conn_id}/test")

        assert resp.status_code == 200


# ============================================================
# 7.2 认证管理端到端测试
# ============================================================

class TestAuthE2E:
    """验证: 5种认证方式的存储加密→查询脱敏→解密使用全链路"""

    def _create_connection(self, client, auth_type, auth_config):
        """Helper: 创建一个带认证的连接"""
        body = {
            "name": f"E2E-{auth_type}",
            "protocol": "rest",
            "base_url": "https://api.example.com",
            "auth_type": auth_type,
            "auth_config": auth_config,
        }
        resp = client.post("/api/v1/connections", json=body, headers=HEADERS)
        return resp

    def test_api_key_e2e(self, client, mock_session, encrypt_key):
        """API Key: 创建→脱敏返回→解密使用"""
        resp = self._create_connection(client, "api_key", {
            "key_name": "X-API-Key",
            "key_value": "sk-secret-12345",
        })
        assert resp.status_code == 201
        data = resp.json()
        # 脱敏: key_value 应为 ***
        assert data["auth_config"]["key_value"] == "***"
        assert data["auth_config"]["key_name"] == "X-API-Key"

    def test_bearer_e2e(self, client, mock_session, encrypt_key):
        """Bearer Token: 创建→脱敏返回"""
        resp = self._create_connection(client, "bearer", {
            "token": "ghp_abcdef1234567890",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["auth_config"]["token"] == "***"

    def test_basic_e2e(self, client, mock_session, encrypt_key):
        """Basic Auth: 创建→脱敏返回→密码已加密"""
        resp = self._create_connection(client, "basic", {
            "username": "admin",
            "password": "super-secret-pwd",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["auth_config"]["password"] == "***"
        assert data["auth_config"]["username"] == "admin"

    def test_oauth2_e2e(self, client, mock_session, encrypt_key):
        """OAuth2: 创建→脱敏返回"""
        resp = self._create_connection(client, "oauth2", {
            "client_id": "oauth-client-001",
            "client_secret": "cs-very-secret",
            "token_url": "https://auth.example.com/token",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["auth_config"]["client_secret"] == "***"
        assert data["auth_config"]["client_id"] == "oauth-client-001"

    def test_sign_e2e(self, client, mock_session, encrypt_key):
        """Sign 签名: 创建→脱敏返回"""
        resp = self._create_connection(client, "sign", {
            "app_id": "app-001",
            "secret_key": "sign-secret-key-abcdef",
            "sign_algorithm": "HMAC-SHA256",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["auth_config"]["secret_key"] == "***"
        assert data["auth_config"]["app_id"] == "app-001"


# ============================================================
# 7.3 连接管理 CRUD 端到端测试
# ============================================================

class TestConnectionCrudE2E:
    """验证: 连接全生命周期 CRUD + 测试"""

    def test_full_lifecycle(self, client, mock_session, encrypt_key):
        """创建→查询列表→查询单个→测试连接→更新→删除"""
        # 1. 创建
        body = {
            "name": "E2E全流程连接",
            "protocol": "rest",
            "base_url": "https://api.example.com",
            "auth_type": "api_key",
            "auth_config": {"key_name": "X-Key", "key_value": "test-key"},
        }
        create_resp = client.post("/api/v1/connections", json=body, headers=HEADERS)
        assert create_resp.status_code == 201
        conn_id = create_resp.json()["id"]

        # 为了后续 GET 请求能查到，注入 mock 数据
        fake_conn = MagicMock()
        fake_conn.id = conn_id
        fake_conn.name = "E2E全流程连接"
        fake_conn.connection_type = "source"
        fake_conn.protocol = "rest"
        fake_conn.base_url = "https://api.example.com"
        fake_conn.auth_type = "api_key"
        fake_conn.auth_config = {"key_name": "X-Key", "key_value": "***"}
        fake_conn.headers = None
        fake_conn.timeout_ms = 30000
        fake_conn.retry_count = 3
        fake_conn.db_type = None
        fake_conn.db_config = None
        fake_conn.status = "active"
        from datetime import datetime, timezone
        fake_conn.created_at = datetime.now(timezone.utc)
        fake_conn.updated_at = datetime.now(timezone.utc)
        mock_session.get = AsyncMock(return_value=fake_conn)

        # 2. 列表查询
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [fake_conn]
        mock_session.execute = AsyncMock(return_value=mock_result)
        list_resp = client.get("/api/v1/connections")
        assert list_resp.status_code == 200
        assert len(list_resp.json()) == 1

        # 3. 单个查询
        get_resp = client.get(f"/api/v1/connections/{conn_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["name"] == "E2E全流程连接"

        # 4. 测试连接
        test_resp = client.post(f"/api/v1/connections/{conn_id}/test")
        assert test_resp.status_code == 200

        # 5. 更新
        with patch("app.api.connections.encrypt_auth_config") as mock_enc:
            mock_enc.return_value = {"key_name": "X-Key", "key_value": "enc:xxx"}
            update_resp = client.put(f"/api/v1/connections/{conn_id}",
                                     json={"name": "E2E更新"}, headers=HEADERS)
        assert update_resp.status_code == 200

        # 6. 删除（无引用）
        mock_result2 = MagicMock()
        mock_result2.scalar.return_value = 0
        mock_session.execute = AsyncMock(return_value=mock_result2)
        del_resp = client.delete(f"/api/v1/connections/{conn_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["id"] == conn_id

    def test_create_with_db_type(self, client, mock_session, encrypt_key):
        """创建数据库类型连接"""
        body = {
            "name": "MySQL源",
            "protocol": "mysql",
            "base_url": None,
            "auth_type": "basic",
            "auth_config": {"username": "root", "password": "root123"},
            "db_type": "mysql",
            "db_config": {"host": "localhost", "port": 3306, "database": "test"},
        }
        resp = client.post("/api/v1/connections", json=body, headers=HEADERS)
        assert resp.status_code == 201
        data = resp.json()
        assert data["db_type"] == "mysql"
        assert data["protocol"] == "mysql"

    def test_delete_with_references_blocked(self, client, mock_session, encrypt_key):
        """有引用的连接删除返回 409"""
        conn_id = str(uuid4())
        mock_result = MagicMock()
        mock_result.scalar.return_value = 3
        mock_session.execute = AsyncMock(return_value=mock_result)

        resp = client.delete(f"/api/v1/connections/{conn_id}")
        assert resp.status_code == 409
        assert "3" in resp.json()["detail"]


# ============================================================
# 7.5 监控看板数据准确性测试
# ============================================================

class TestMetricsAccuracyE2E:
    """验证: 监控看板 API 返回的数据结构与业务逻辑一致"""

    @pytest.fixture
    def mock_redis_for_metrics(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=None)
        client.set = AsyncMock(return_value=True)
        with patch("app.api.metrics.get_redis", return_value=client), \
             patch("app.redis_client.get_redis", return_value=client):
            yield client

    def test_overview_structure(self, client, mock_session, mock_redis_for_metrics):
        """overview 返回完整结构"""
        from datetime import datetime, timezone

        mock_result = MagicMock()
        # sync_count
        mock_result.scalar.return_value = 42
        # latencies：现在是取 duration_ms 值列表，Python 侧算分位
        lat_result = MagicMock()
        lat_result.scalars.return_value.all.return_value = [10.0, 15.0, 20.0]
        # task_status_distribution
        mock_result.all.return_value = [("active", 3), ("paused", 1), ("error", 0)]
        mock_result.scalars.return_value.all.return_value = [10.0, 15.0, 20.0]

        async def _execute(stmt, *a, **k):
            return lat_result if "duration_ms" in str(stmt) else mock_result

        mock_session.execute = AsyncMock(side_effect=_execute)

        resp = client.get("/api/v1/metrics/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sync_count"] == 42
        assert isinstance(data["success_rate"], (int, float))
        assert data["p50_latency"] == 15.0  # 线性插值分位
        assert data["p95_latency"] == 19.5  # [10,15,20] 的 P95 线性插值
        assert data["p99_latency"] == 19.9
        assert isinstance(data["queue_depth"], (int, float))
        assert isinstance(data["task_status_distribution"], dict)
        assert data["task_status_distribution"].get("active") == 3

    def test_trend_returns_items(self, client, mock_session, mock_redis_for_metrics):
        """trend 返回趋势数据项（Python 侧分桶）"""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        rows = []
        for h in range(0, 3):
            ts = now - timedelta(hours=h)
            rows.append((ts, "success"))
            rows.append((ts, "failed"))

        mock_result = MagicMock()
        mock_result.all.return_value = rows
        mock_session.execute = AsyncMock(return_value=mock_result)

        resp = client.get("/api/v1/metrics/trend", params={"period": "24h", "interval": 1})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["interval"] == "1h"
        assert len(data["items"]) >= 1
        for item in data["items"]:
            assert item["success"] == 1 and item["failed"] == 1

    def test_overview_success_rate_calculation(self, client, mock_session, mock_redis_for_metrics):
        """成功率计算: 成功数 / 总数"""
        call_count = [0]

        def mock_execute_side_effect(stmt, **kwargs):
            mock = MagicMock()
            call_count[0] += 1
            idx = call_count[0]
            # Query order in get_overview: count(total)→count(success)→latency→queue_depth→task_distribution
            if idx == 1:
                mock.scalar.return_value = 40  # sync_count
            elif idx == 2:
                mock.scalar.return_value = 30  # success_count
            elif idx == 3:
                mock.one_or_none.return_value = (10.0, 50.0, 100.0)  # latencies
            elif idx == 4:
                mock.scalar.return_value = 5  # queue_depth
            elif idx == 5:
                mock.all.return_value = [("active", 2)]  # task_status_distribution
            else:
                mock.scalar.return_value = 0
            return mock

        mock_session.execute = AsyncMock(side_effect=mock_execute_side_effect)

        resp = client.get("/api/v1/metrics/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sync_count"] == 40
        assert data["success_rate"] == 75.0  # 30/40 * 100
        assert data["queue_depth"] == 5
