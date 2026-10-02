"""阶段Ⅰ 组1 前置修复的专项单测

覆盖：同步链路解密（1.1）、encrypt_key_missing（1.2）、PUT 掩码保留（1.3）、
adapter_config 唯一构造点（1.6）、协议参数与缺失报错（1.10）。
"""
import base64
from unittest.mock import MagicMock

import pytest

from app.security import crypto
from app.security.crypto import CryptoError, encrypt, encrypt_auth_config, decrypt_auth_config
from app.services.adapter_config import build_adapter_config


@pytest.fixture()
def real_encrypt_key(monkeypatch):
    """注入真实 Fernet 密钥并清空单例缓存"""
    from cryptography.fernet import Fernet
    from app.config import settings

    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "encrypt_key", key)
    monkeypatch.setattr(crypto, "_fernet", None)
    yield key
    monkeypatch.setattr(crypto, "_fernet", None)


class TestSyncDecrypt:
    def test_build_headers_decrypts_bearer_token(self, real_encrypt_key):
        """task 1.1：发出的 Authorization 必须是明文 token，不得以 enc: 开头"""
        from app.services.api_client import build_headers

        encrypted_token = encrypt("plain-token-123")
        assert encrypted_token.startswith("enc:")
        conn = MagicMock(auth_type="bearer", auth_config={"token": encrypted_token},
                         headers={})
        mapping = MagicMock(source_headers=None, target_headers=None)

        headers = build_headers(conn, mapping)
        assert headers["Authorization"] == "Bearer plain-token-123"

    def test_build_headers_basic_auth_decrypts_password(self, real_encrypt_key):
        from app.services.api_client import build_headers

        conn = MagicMock(auth_type="basic",
                         auth_config={"username": "u", "password": encrypt("p@ss")},
                         headers={})
        headers = build_headers(conn)
        expected = base64.b64encode(b"u:p@ss").decode()
        assert headers["Authorization"] == f"Basic {expected}"


class TestEncryptKeyMissing:
    def test_crypto_error_carries_code(self, monkeypatch):
        """task 1.2：密钥缺失时错误信息含 encrypt_key_missing 可识别码"""
        from app.config import settings

        monkeypatch.setattr(settings, "encrypt_key", "")
        monkeypatch.setattr(crypto, "_fernet", None)
        with pytest.raises(CryptoError, match="encrypt_key_missing"):
            encrypt("secret")
        monkeypatch.setattr(crypto, "_fernet", None)

    def test_sync_failure_carries_same_cause(self, monkeypatch):
        """task 1.2：跑同步时因密钥缺失失败，错误含同因代码"""
        from app.config import settings
        from app.services.api_client import build_headers

        monkeypatch.setattr(settings, "encrypt_key", "")
        monkeypatch.setattr(crypto, "_fernet", None)
        conn = MagicMock(auth_type="bearer", auth_config={"token": "enc:AbCd"}, headers={})
        with pytest.raises(CryptoError, match="encrypt_key_missing"):
            build_headers(conn)
        monkeypatch.setattr(crypto, "_fernet", None)


class TestMaskPreservation:
    def test_masked_field_keeps_old_value(self):
        """task 1.3：PUT 回传 *** 的敏感字段保留库中原密文"""
        from app.api.connections import _preserve_masked_credentials

        old = {"token": "enc:OLD"}
        new = {"token": "***"}
        result = _preserve_masked_credentials("bearer", new, old)
        assert result["token"] == "enc:OLD"

    def test_new_value_overwrites(self):
        """非掩码的新值正常覆盖"""
        from app.api.connections import _preserve_masked_credentials

        result = _preserve_masked_credentials("bearer", {"token": "enc:NEW"}, {"token": "enc:OLD"})
        assert result["token"] == "enc:NEW"

    def test_roundtrip_get_put_decrypt(self, real_encrypt_key):
        """GET→PUT 不回传真实凭证后，decrypt_auth_config 仍还原原 token"""
        from app.security.desensitize import desensitize_auth_config
        from app.api.connections import _preserve_masked_credentials

        stored = encrypt_auth_config("bearer", {"token": "real-token"})
        # GET：脱敏返回
        masked = desensitize_auth_config("bearer", stored)
        assert masked["token"] == "***"
        # PUT：原样回传掩码 → 保留原值 → 解密还原
        merged = _preserve_masked_credentials("bearer", masked, stored)
        assert decrypt_auth_config("bearer", merged)["token"] == "real-token"


class TestAdapterConfigBuilder:
    def _make_conn(self, real_encrypt_key):
        conn = MagicMock()
        conn.base_url = "https://api.example.com"
        conn.auth_type = "bearer"
        conn.auth_config = {"token": encrypt("XYZ")}
        conn.headers = {"X-Conn": "1"}
        conn.timeout_ms = 30000
        conn.retry_count = 3
        return conn

    def test_deep_equal_for_same_conn(self, real_encrypt_key):
        """task 1.6：同连接经两次构造结果深度相等"""
        conn = self._make_conn(real_encrypt_key)
        mapping = MagicMock(source_headers=None, target_headers=None, mapping_config=None)
        a = build_adapter_config(conn, mapping, side="source")
        b = build_adapter_config(conn, mapping, side="source")
        assert a == b

    def test_auth_decrypted_and_mapping_override(self, real_encrypt_key):
        conn = self._make_conn(real_encrypt_key)
        mapping = MagicMock(source_headers={"X-Src": "s"}, target_headers=None,
                            mapping_config=None)
        config = build_adapter_config(conn, mapping, side="source")
        assert config["auth_config"]["token"] == "XYZ"  # 已解密
        assert config["headers"] == {"X-Conn": "1", "X-Src": "s"}

    def test_no_mapping_no_override(self, real_encrypt_key):
        conn = self._make_conn(real_encrypt_key)
        config = build_adapter_config(conn)
        assert config["headers"] == {"X-Conn": "1"}

    def test_protocol_params_merged(self, real_encrypt_key):
        """task 1.10：mapping_config['protocol'] 合并进 adapter_config"""
        conn = self._make_conn(real_encrypt_key)
        mapping = MagicMock(source_headers=None, target_headers=None,
                            mapping_config={"protocol": {"service_name": "helloworld.Greeter",
                                                          "use_tls": True}})
        config = build_adapter_config(conn, mapping)
        assert config["service_name"] == "helloworld.Greeter"
        assert config["use_tls"] is True

    def test_rest_unchanged_without_protocol_key(self, real_encrypt_key):
        """task 1.10：无 protocol 键时 REST 行为不变（多余键不存在）"""
        conn = self._make_conn(real_encrypt_key)
        mapping = MagicMock(source_headers=None, target_headers=None,
                            mapping_config={"fields": []})
        config = build_adapter_config(conn, mapping)
        assert "service_name" not in config
        # RestAdapter 构造不受额外键影响
        from app.adapters.factory import ProtocolAdapterFactory
        adapter = ProtocolAdapterFactory.create("rest", config)
        assert adapter.base_url == "https://api.example.com"

    def test_grpc_missing_service_name_names_key(self):
        """task 1.10：gRPC 缺 service_name 报错指明键名"""
        import asyncio
        from app.adapters.grpc_adapter import GrpcAdapter

        adapter = GrpcAdapter({"base_url": "localhost:50051"})
        with pytest.raises(ValueError, match="service_name"):
            asyncio.run(adapter.fetch_data("GetStatus"))


class TestGrpcProtoContentMissing:
    def test_grpc_missing_proto_also_diagnosable(self):
        """未提供 proto_content 时的错误可诊断（非崩溃）"""
        import asyncio
        from app.adapters.grpc_adapter import GrpcAdapter

        adapter = GrpcAdapter({"base_url": "localhost:50051",
                               "service_name": "helloworld.Greeter"})
        with pytest.raises(Exception):
            asyncio.run(adapter.fetch_data("SayHello"))
