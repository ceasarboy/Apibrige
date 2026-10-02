"""阶段Ⅰ 组1 的 API 层验证（1.2 密钥缺失 422、1.3 PUT 掩码保留）"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from app.main import app
from app.database import get_session
from app.security import crypto
from tests.conftest import authed_client_kwargs
from app.security.crypto import decrypt_auth_config


@pytest.fixture()
def real_encrypt_key(monkeypatch):
    from cryptography.fernet import Fernet
    from app.config import settings

    monkeypatch.setattr(settings, "encrypt_key", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_fernet", None)
    yield
    monkeypatch.setattr(crypto, "_fernet", None)


@pytest.fixture()
def api(mock_db_session=None):
    """mock 会话 + TestClient（用例内通过 fixture 拿 session）"""
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)

    async def _populate(instance):
        if not instance.id:
            instance.id = uuid4()
        for attr, val in (("status", "active"), ("created_at", datetime.now(timezone.utc)),
                          ("updated_at", datetime.now(timezone.utc))):
            if hasattr(instance, attr) and getattr(instance, attr) is None:
                setattr(instance, attr, val)

    session.refresh = AsyncMock(side_effect=_populate)
    session.commit = AsyncMock()
    session.add = MagicMock()

    async def override():
        yield session

    app.dependency_overrides[get_session] = override
    yield session, TestClient(app, **authed_client_kwargs())
    app.dependency_overrides.clear()


class TestEncryptKeyMissingApi:
    def test_create_connection_returns_422_with_code(self, monkeypatch, api):
        """task 1.2：encrypt_key 为空时创建带凭证连接 → 422 且含 encrypt_key_missing"""
        from app.config import settings

        _, client = api
        monkeypatch.setattr(settings, "encrypt_key", "")
        monkeypatch.setattr(crypto, "_fernet", None)
        resp = client.post("/api/v1/connections", json={
            "name": "带认证连接",
            "connection_type": "target",
            "protocol": "rest",
            "base_url": "https://api.example.com",
            "auth_type": "bearer",
            "auth_config": {"token": "secret"},
        })
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "encrypt_key_missing"
        monkeypatch.setattr(crypto, "_fernet", None)

    def test_create_connection_ok_with_key(self, real_encrypt_key, api):
        """密钥就位时同样请求创建成功（201）"""
        _, client = api
        resp = client.post("/api/v1/connections", json={
            "name": "带认证连接",
            "connection_type": "target",
            "protocol": "rest",
            "base_url": "https://api.example.com",
            "auth_type": "bearer",
            "auth_config": {"token": "secret"},
        })
        assert resp.status_code == 201


class TestPutMaskPreserved:
    def test_put_masked_token_preserves_stored_value(self, real_encrypt_key, api):
        """task 1.3：GET→PUT 回传 *** 后，库中密文不变、可解密出原 token"""
        session, client = api
        created = client.post("/api/v1/connections", json={
            "name": "认证桥",
            "connection_type": "source",
            "protocol": "rest",
            "base_url": "https://api.example.com",
            "auth_type": "bearer",
            "auth_config": {"token": "real-token"},
        }).json()
        conn_id = created["id"]
        assert created["auth_config"]["token"] == "***"  # GET 已脱敏

        # 让后续 GET(PUT 内部) 返回已创建的 ORM 对象
        stored_conn = session.add.call_args_list[0][0][0]
        session.get = AsyncMock(return_value=stored_conn)

        # GET→PUT 原样回传（token 为 ***）
        updated = client.put(f"/api/v1/connections/{conn_id}", json={
            "auth_config": {"token": "***"},
        }).json()
        assert updated["auth_config"]["token"] == "***"

        # 库中实际保存的密文未变（create 时 add 进 session 的 ORM 对象）
        stored_auth = session.add.call_args_list[0][0][0].auth_config
        assert decrypt_auth_config("bearer", stored_auth)["token"] == "real-token"
