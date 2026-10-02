"""API 集成测试 — 覆盖连接/映射/任务/日志/监控 CRUD 全流程"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from fastapi.testclient import TestClient
from app.main import app
from tests.conftest import authed_client_kwargs
from app.database import get_session
from app.schemas import (
    ConnectionCreate, MappingCreate, TaskCreate,
    MappingPreviewRequest,
)


@pytest.fixture
def mock_db_session():
    """Mock 异步数据库会话，所有 API 依赖注入用"""
    from datetime import datetime, timezone
    from uuid import uuid4

    session = AsyncMock()

    # 默认 get 返回 None（模拟记录不存在）
    session.get = AsyncMock(return_value=None)

    async def _populate_on_refresh(instance):
        """模拟 DB 提交后自动填充 id/status/timestamps"""
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
def client(mock_db_session):
    """FastAPI TestClient，注入 mock session"""
    async def override_get_session():
        yield mock_db_session

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app, **authed_client_kwargs())
    app.dependency_overrides.clear()


# ============================================================
# 连接管理 CRUD
# ============================================================

class TestConnectionAPI:
    """验证: 连接 CRUD 端点返回正确状态码和数据"""

    HEADERS = {"accept": "application/json", "content-type": "application/json"}

    def test_create_connection(self, client, mock_db_session):
        """POST /api/v1/connections 返回 201"""
        body = {
            "name": "测试连接",
            "protocol": "rest",
            "base_url": "https://api.example.com",
            "auth_type": "api_key",
            "auth_config": {"key_name": "X-API-Key", "key_value": "secret123"},
        }
        # mock 加密密钥
        with patch("app.security.crypto.settings") as mock_settings:
            import base64
            mock_settings.encrypt_key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()

            resp = client.post("/api/v1/connections", json=body, headers=self.HEADERS)

        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "测试连接"
        assert data["auth_config"]["key_value"] == "***"  # 脱敏

    def test_list_connections_empty(self, client, mock_db_session):
        """GET /api/v1/connections 返回空列表"""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db_session.execute.return_value = mock_result

        resp = client.get("/api/v1/connections")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_connection_not_found(self, client, mock_db_session):
        """GET /api/v1/connections/{id} 返回 404"""
        conn_id = str(uuid4())
        mock_db_session.get.return_value = None

        resp = client.get(f"/api/v1/connections/{conn_id}")
        assert resp.status_code == 404

    def test_delete_connection_no_refs(self, client, mock_db_session):
        """DELETE /api/v1/connections/{id} 无引用时返回 200"""
        conn_id = str(uuid4())
        fake_conn = MagicMock()
        fake_conn.id = conn_id
        mock_db_session.get = AsyncMock(return_value=fake_conn)
        # 引用计数为 0
        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        mock_db_session.execute.return_value = mock_result

        resp = client.delete(f"/api/v1/connections/{conn_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == conn_id

    def test_delete_connection_with_refs(self, client, mock_db_session):
        """DELETE /api/v1/connections/{id} 有引用时返回 409"""
        conn_id = str(uuid4())
        # 引用计数 > 0
        mock_result = MagicMock()
        mock_result.scalar.return_value = 2
        mock_db_session.execute.return_value = mock_result

        resp = client.delete(f"/api/v1/connections/{conn_id}")
        assert resp.status_code == 409
        assert "引用" in resp.json()["detail"]

    def test_list_connections_with_filter(self, client, mock_db_session):
        """GET /api/v1/connections?connection_type=source 过滤"""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db_session.execute.return_value = mock_result

        resp = client.get("/api/v1/connections?connection_type=source")
        assert resp.status_code == 200


# ============================================================
# 字段映射 CRUD
# ============================================================

class TestMappingAPI:
    """验证: 映射 CRUD + 转换预览"""

    HEADERS = {"accept": "application/json", "content-type": "application/json"}

    def test_create_mapping(self, client, mock_db_session):
        """POST /api/v1/mappings 返回 201"""
        body = {
            "name": "测试映射",
            "source_connection_id": str(uuid4()),
            "target_connection_id": str(uuid4()),
            "source_endpoint": "/users",
            "target_endpoint": "/customers",
            "mapping_config": {
                "fields": [
                    {"source": "name", "target": "customerName", "transform": "string"},
                ]
            },
        }

        resp = client.post("/api/v1/mappings", json=body, headers=self.HEADERS)
        assert resp.status_code == 201
        assert resp.json()["name"] == "测试映射"

    def test_list_mappings_empty(self, client, mock_db_session):
        """GET /api/v1/mappings 返回空列表"""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db_session.execute.return_value = mock_result

        resp = client.get("/api/v1/mappings")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_mapping_not_found(self, client, mock_db_session):
        """GET /api/v1/mappings/{id} 返回 404"""
        mock_db_session.get.return_value = None

        resp = client.get(f"/api/v1/mappings/{uuid4()}")
        assert resp.status_code == 404

    def test_preview_mapping(self, client, mock_db_session):
        """POST /api/v1/mappings/{id}/preview 转换预览"""
        mapping_id = str(uuid4())
        fake_mapping = MagicMock()
        fake_mapping.mapping_config = {
            "fields": [
                {"source": "user_name", "target": "customerName", "transform": "string"},
            ]
        }
        mock_db_session.get = AsyncMock(return_value=fake_mapping)

        body = {"test_data": {"user_name": "张三"}}
        resp = client.post(f"/api/v1/mappings/{mapping_id}/preview", json=body, headers=self.HEADERS)

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["transformed_data"]["customerName"] == "张三"

    def test_preview_mapping_not_found(self, client, mock_db_session):
        """转换预览时映射不存在返回 404"""
        mock_db_session.get.return_value = None

        resp = client.post(f"/api/v1/mappings/{uuid4()}/preview", json={"test_data": {}}, headers=self.HEADERS)
        assert resp.status_code == 404


# ============================================================
# 任务 CRUD + 暂停/恢复
# ============================================================

class TestTaskAPI:
    """验证: 任务 CRUD + 暂停/恢复"""

    HEADERS = {"accept": "application/json", "content-type": "application/json"}

    def test_create_task(self, client, mock_db_session):
        """POST /api/v1/tasks 返回 201"""
        body = {
            "mapping_id": str(uuid4()),
            "sync_mode": "async",
            "fallback_to_async": True,
        }
        # mapping_id 存在性校验（审查修复）：mock get 返回已存在的映射
        # （validate_task_change 会读其 mapping_config/sync_mode，需给真实形状）
        mock_db_session.get = AsyncMock(return_value=MagicMock(
            mapping_config={}, sync_mode="full", incremental_field=None))

        resp = client.post("/api/v1/tasks", json=body, headers=self.HEADERS)
        assert resp.status_code == 201
        assert resp.json()["sync_mode"] == "async"

    def test_list_tasks_empty(self, client, mock_db_session):
        """GET /api/v1/tasks 返回空列表"""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db_session.execute.return_value = mock_result

        resp = client.get("/api/v1/tasks")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_tasks_with_status_filter(self, client, mock_db_session):
        """GET /api/v1/tasks?status=active 过滤"""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db_session.execute.return_value = mock_result

        resp = client.get("/api/v1/tasks?status=active")
        assert resp.status_code == 200

    def test_get_task_not_found(self, client, mock_db_session):
        """GET /api/v1/tasks/{id} 返回 404"""
        mock_db_session.get.return_value = None

        resp = client.get(f"/api/v1/tasks/{uuid4()}")
        assert resp.status_code == 404

    def test_pause_task(self, client, mock_db_session):
        """POST /api/v1/tasks/{id}/pause 暂停任务"""
        task_id = str(uuid4())
        fake_task = MagicMock()
        fake_task.status = "active"
        mock_db_session.get = AsyncMock(return_value=fake_task)

        resp = client.post(f"/api/v1/tasks/{task_id}/pause")
        assert resp.status_code == 200
        assert resp.json()["message"] == "任务已暂停"

    def test_pause_already_paused(self, client, mock_db_session):
        """暂停已暂停的任务返回 400"""
        task_id = str(uuid4())
        fake_task = MagicMock()
        fake_task.status = "paused"
        mock_db_session.get = AsyncMock(return_value=fake_task)

        resp = client.post(f"/api/v1/tasks/{task_id}/pause")
        assert resp.status_code == 400

    def test_resume_task(self, client, mock_db_session):
        """POST /api/v1/tasks/{id}/resume 恢复任务"""
        task_id = str(uuid4())
        fake_task = MagicMock()
        fake_task.status = "paused"
        mock_db_session.get = AsyncMock(return_value=fake_task)

        resp = client.post(f"/api/v1/tasks/{task_id}/resume")
        assert resp.status_code == 200
        assert resp.json()["message"] == "任务已恢复"

    def test_resume_not_paused(self, client, mock_db_session):
        """恢复未暂停的任务返回 400"""
        task_id = str(uuid4())
        fake_task = MagicMock()
        fake_task.status = "active"
        mock_db_session.get = AsyncMock(return_value=fake_task)

        resp = client.post(f"/api/v1/tasks/{task_id}/resume")
        assert resp.status_code == 400


# ============================================================
# 日志查询
# ============================================================

class TestLogsAPI:
    """验证: 日志分页查询和过滤"""

    def test_list_logs_empty(self, client, mock_db_session):
        """GET /api/v1/logs 返回空分页"""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        mock_result.scalars.return_value.all.return_value = []
        # execute 被调用两次：一次 count，一次列表
        mock_db_session.execute = AsyncMock(return_value=mock_result)

        resp = client.get("/api/v1/logs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_list_logs_with_filters(self, client, mock_db_session):
        """GET /api/v1/logs?status=failed&action=push 过滤"""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 5
        mock_result.scalars.return_value.all.return_value = []
        mock_db_session.execute = AsyncMock(return_value=mock_result)

        resp = client.get("/api/v1/logs?status=failed&action=push")
        assert resp.status_code == 200

    def test_get_log_not_found(self, client, mock_db_session):
        """GET /api/v1/logs/{id} 返回 404"""
        mock_db_session.get.return_value = None

        resp = client.get(f"/api/v1/logs/{uuid4()}")
        assert resp.status_code == 404


# ============================================================
# 监控统计
# ============================================================

class TestMetricsAPI:
    """验证: 监控统计 API 返回正确结构"""

    def test_get_overview_with_cache_miss(self, client, mock_db_session, mock_redis):
        """GET /api/v1/metrics/overview 返回正确统计结构"""
        # Redis 缓存未命中
        mock_redis.get = AsyncMock(return_value=None)

        # 模拟 SQL 聚合查询
        mock_result = MagicMock()
        mock_result.scalar.return_value = 100  # sync_count
        mock_result.one_or_none.return_value = (50.0, 200.0, 500.0)  # p50, p95, p99
        mock_result.all.return_value = [("active", 3), ("paused", 1)]
        mock_db_session.execute = AsyncMock(return_value=mock_result)

        resp = client.get("/api/v1/metrics/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert "sync_count" in data
        assert "success_rate" in data
        assert "p50_latency" in data
        assert "p95_latency" in data
        assert "p99_latency" in data
        assert "queue_depth" in data
        assert "task_status_distribution" in data

    def test_get_trend_empty(self, client, mock_db_session, mock_redis):
        """GET /api/v1/metrics/trend 返回空趋势"""
        mock_redis.get = AsyncMock(return_value=None)
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_db_session.execute = AsyncMock(return_value=mock_result)

        resp = client.get("/api/v1/metrics/trend?period=24h&interval=1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["interval"] == "1h"
        assert data["items"] == []


# ============================================================
# 字段校验测试
# ============================================================

class TestSchemaValidation:
    """验证: 请求体校验（Pydantic）"""

    HEADERS = {"accept": "application/json", "content-type": "application/json"}

    def test_create_connection_missing_name(self, client, mock_db_session):
        """缺少必填字段 name 时返回 422"""
        with patch("app.security.crypto.settings") as mock_settings:
            import base64
            mock_settings.encrypt_key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
            resp = client.post("/api/v1/connections", json={"protocol": "rest"}, headers=self.HEADERS)
        assert resp.status_code == 422

    def test_create_connection_name_too_long(self, client, mock_db_session):
        """name 超过 100 字符返回 422"""
        with patch("app.security.crypto.settings") as mock_settings:
            import base64
            mock_settings.encrypt_key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
            resp = client.post("/api/v1/connections", json={
                "name": "x" * 101, "protocol": "rest"
            }, headers=self.HEADERS)
        assert resp.status_code == 422

    def test_create_task_invalid_sync_mode(self, client, mock_db_session):
        """sync_mode 不在枚举中时返回 422（schema 层枚举校验）"""
        resp = client.post("/api/v1/tasks", json={
            "mapping_id": str(uuid4()), "sync_mode": "invalid_mode"
        }, headers=self.HEADERS)
        assert resp.status_code == 422

    def test_health_check(self, client):
        """GET /health 返回 200"""
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"
