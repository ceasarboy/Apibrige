"""pytest 公共 fixtures：mock 外部依赖（Redis/RabbitMQ/API）"""
import base64
import os
import tempfile

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

# 测试自足：不依赖工作区或发行包里的 .env
# - 数据库：临时 SQLite（否则落到配置默认的 PostgreSQL，测试会去连真实库）
# - 加密密钥：测试专用（非真实凭据；需要时用例内 with_encrypt_key 会再覆盖）
_TMP_DIR = tempfile.mkdtemp(prefix="apibridge-pytest-")
os.environ.setdefault("APIBRIDGE_DATABASE_URL",
                      f"sqlite+aiosqlite:///{_TMP_DIR}/pytest.db")
os.environ.setdefault(
    "APIBRIDGE_ENCRYPT_KEY",
    base64.urlsafe_b64encode(b"apibridge-pytest-key-0123456789x").decode())

# 测试统一使用的管理 token（task 2.5：全量测试在鉴权开启下通过）
TEST_ADMIN_TOKEN = "test-admin-token"


@pytest.fixture(autouse=True)
def _admin_token(monkeypatch):
    """task 2.5：统一注入测试 token，API 用例需带 X-Admin-Token 请求头"""
    from app.config import settings

    monkeypatch.setattr(settings, "admin_token", TEST_ADMIN_TOKEN)
    yield


@pytest.fixture(autouse=True)
def _reset_redis_pool():
    """用例间不得泄漏真实 Redis 连接池

    合并后若干路径（限流/幂等/调度锁）会自行 get_redis()，若某用例真的连上了本机
    Redis，连接池会跨用例存活；本机若是旧版本 Redis（不支持 HELLO 握手），
    后续用例会拿到 `unknown command 'HELLO'` 这类"非"连接失败的异常，
    表现为与被测用例无关的偶发 failed（实测）。
    """
    yield
    import app.redis_client as rc

    rc._pool = None


@pytest.fixture(autouse=True)
def _bi_enabled(monkeypatch):
    """合并后 BI 端点带启用门禁（BI 关闭 → 404，见 app/api/bi.py::require_bi）。

    既有 BI 用例按"功能已启用"编写，这里统一打开；门禁本身由
    tests/test_bi_api.py::TestBiGate 单独覆盖（关闭态 + /status 例外）。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "bi_enabled", True)
    monkeypatch.setattr(settings, "bi_aggregation_enabled", True)
    yield


def authed_client_kwargs() -> dict:
    """TestClient 构造参数：默认带鉴权请求头"""
    return {"headers": {"X-Admin-Token": TEST_ADMIN_TOKEN}}


@pytest.fixture
def task_id():
    return uuid4()


@pytest.fixture
def mock_redis():
    """Mock Redis 客户端（patch 所有 import 站点）"""
    client = AsyncMock()
    client.set = AsyncMock(return_value=True)
    client.get = AsyncMock(return_value=None)
    client.delete = AsyncMock(return_value=1)
    client.ping = AsyncMock(return_value=True)
    client.eval = AsyncMock(return_value=1)
    client.scan_iter = MagicMock(return_value=iter([]))
    # 各模块通过 from app.redis_client import get_redis 导入，需逐一 patch
    with patch("app.redis_client.get_redis", return_value=client), \
         patch("app.cache.lock.get_redis", return_value=client), \
         patch("app.cache.idempotency.get_redis", return_value=client), \
         patch("app.cache.rate_limiter.get_redis", return_value=client), \
         patch("app.cache.schema_cache.get_redis", return_value=client), \
         patch("app.api.metrics.get_redis", return_value=client):
        yield client


# 源/目标 fixture 共享同一适配器替身：两个 fixture 都会 patch
# sync_handler 的工厂，后应用者覆盖前者；共享对象保证两个方法都可用。
_SHARED_ADAPTER = {"adapter": None}


@pytest.fixture(autouse=True)
def _reset_shared_adapter():
    """每个用例重置共享适配器，避免 mock 调用计数跨用例泄漏"""
    _SHARED_ADAPTER["adapter"] = None
    yield


def _shared_adapter() -> MagicMock:
    if _SHARED_ADAPTER["adapter"] is None:
        adapter = MagicMock()
        adapter.fetch_data = AsyncMock(return_value={})
        adapter.push_data = AsyncMock(return_value={"status_code": 200, "body": None})
        _SHARED_ADAPTER["adapter"] = adapter
    return _SHARED_ADAPTER["adapter"]


def _factory_for(adapter: MagicMock) -> MagicMock:
    factory = MagicMock()
    factory.create = MagicMock(return_value=adapter)
    return factory


@pytest.fixture
def mock_api_source():
    """Mock 源 API（适配器路径）：工厂替身返回 fetch_data 替身适配器"""
    mock = AsyncMock(return_value={"user_name": "张三", "user_email": "zhangsan@example.com", "age": "25"})
    adapter = _shared_adapter()
    adapter.fetch_data = mock
    with patch("app.services.sync_handler.ProtocolAdapterFactory", _factory_for(adapter)), \
         patch("app.services.router.ProtocolAdapterFactory", _factory_for(adapter)):
        yield mock


@pytest.fixture
def mock_api_target():
    """Mock 目标 API 推送（适配器路径）：工厂替身返回 push_data 替身适配器"""
    mock = AsyncMock(return_value={"status_code": 200, "body": {"id": 1}})
    adapter = _shared_adapter()
    adapter.push_data = mock
    with patch("app.services.sync_handler.ProtocolAdapterFactory", _factory_for(adapter)), \
         patch("app.services.async_handler.ProtocolAdapterFactory", _factory_for(adapter)):
        yield mock


@pytest.fixture
def mapping_config():
    """字段映射配置"""
    return {
        "fields": [
            {"source": "user_name", "target": "customerName", "transform": "string"},
            {"source": "user_email", "target": "email", "transform": "string"},
            {"source": "age", "target": "age", "transform": "int"},
        ]
    }
