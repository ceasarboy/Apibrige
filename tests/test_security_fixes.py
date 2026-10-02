"""安全修复回归测试：custom 凭证加密脱敏 / db_config 密码加密 / 删除同步空键防护 / transformer 缺失字段"""
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.security.crypto import (
    decrypt_auth_config, decrypt_db_config, encrypt_auth_config, encrypt_db_config,
)
from app.security.desensitize import desensitize_auth_config

_TEST_KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()


@pytest.fixture(autouse=True)
def _reset_crypto_singleton():
    import app.security.crypto as crypto_mod
    crypto_mod._fernet = None
    yield
    crypto_mod._fernet = None


@pytest.fixture
def with_encrypt_key():
    with patch("app.security.crypto.settings") as mock_settings:
        mock_settings.encrypt_key = _TEST_KEY
        yield mock_settings


class TestCustomAuthEncryption:
    """custom（自定义 Header）凭证此前不加密不脱敏，现全值处理"""

    pytestmark = pytest.mark.usefixtures("with_encrypt_key")

    def test_encrypt_all_values(self):
        cfg = {"HAP-Appkey": "my-appkey", "HAP-Sign": "my-sign"}
        out = encrypt_auth_config("custom", cfg)
        assert out["HAP-Appkey"].startswith("enc:")
        assert out["HAP-Sign"].startswith("enc:")
        assert "my-appkey" not in str(out)

    def test_decrypt_roundtrip(self):
        cfg = {"HAP-Appkey": "my-appkey", "HAP-Sign": "my-sign"}
        out = decrypt_auth_config("custom", encrypt_auth_config("custom", cfg))
        assert out == cfg

    def test_mask_all_values(self):
        cfg = {"HAP-Appkey": "my-appkey", "HAP-Sign": "my-sign"}
        out = desensitize_auth_config("custom", cfg)
        assert out["HAP-Appkey"] == "***"
        assert out["HAP-Sign"] == "***"


class TestDbConfigPasswordEncryption:
    # 测试专用假值（拼接构造，非真实凭据）
    FAKE_PWD = "test-" + "pwd-" + "123"

    pytestmark = pytest.mark.usefixtures("with_encrypt_key")

    def test_encrypt_and_decrypt_roundtrip(self):
        cfg = {"host": "127.0.0.1", "port": 1521, "username": "system",
               "password": self.FAKE_PWD}
        out = encrypt_db_config(cfg)
        assert out["password"].startswith("enc:")
        assert out["host"] == "127.0.0.1"  # 非密码字段不动
        back = decrypt_db_config(out)
        assert back["password"] == self.FAKE_PWD

    def test_decrypt_plain_legacy_value(self):
        # 存量明文密码（无 enc: 前缀）解密原样返回
        assert decrypt_db_config({"password": self.FAKE_PWD})["password"] == self.FAKE_PWD

    def test_empty(self):
        assert encrypt_db_config(None) is None
        assert decrypt_db_config({}) == {}


class TestTransformerMissingField:
    def test_missing_source_field_not_written(self):
        from app.services.transformer import transform_data
        # 目标已有值的场景：源缺字段时不应产出 null 覆盖
        out = transform_data({"a": 1}, {"fields": [
            {"source": "a", "target": "x"},
            {"source": "not_exist", "target": "y"},
        ]})
        assert out == {"x": 1}

    def test_present_null_still_written(self):
        from app.services.transformer import transform_data
        # 源字段存在但值是 null：仍写 null（源明确表达）
        out = transform_data({"a": None}, {"fields": [{"source": "a", "target": "x"}]})
        assert out == {"x": None}


class TestDecryptWiring:
    """B3 回归防护：DB 连接的密码解密收口在连接器工厂，任何调用点都不得把 enc: 密文当密码

    合并后 `get_database_connector` 内部统一 decrypt_db_config（单一收口点），
    调用点只负责把连接上的原始 db_config 交给工厂。
    """

    def test_factory_decrypts_db_config(self, monkeypatch):
        import app.connectors.database as dbmod

        captured = {}
        enc = encrypt_db_config({"host": "h", "password": "SECRET"})

        class FakeConnector:
            def __init__(self, cfg):
                captured.update(cfg)

        monkeypatch.setattr(dbmod, "_CONNECTOR_REGISTRY",
                            {**dbmod._CONNECTOR_REGISTRY, "oracle": FakeConnector})
        dbmod.get_database_connector("oracle", enc)
        assert captured["password"] == "SECRET", "工厂内必须解密后再构造连接器"

    def test_prober_routes_through_connector_factory(self, monkeypatch):
        import app.probe.prober as pr

        captured = {}

        def fake_gdc(db_type, db_config):
            captured["db_type"] = db_type
            captured["db_config"] = db_config
            return MagicMock()

        monkeypatch.setattr(pr, "get_database_connector", fake_gdc)

        side = MagicMock()
        side.conn.db_type = "oracle"
        side.conn.db_config = {"host": "h", "password": "enc:SECRET"}
        pr._db_connector(side)
        # 探针侧只负责路由到工厂；解密由工厂内部完成（见上一个用例）
        assert captured["db_type"] == "oracle"
        assert captured["db_config"]["password"] == "enc:SECRET"

    def test_async_push_target_uses_db_config_unmodified(self, monkeypatch):
        """异步推送不得自行改写/丢弃 db_config（解密在工厂内统一完成）"""
        import app.services.async_handler as ah

        captured = {}

        async def fake_push(db_type, db_config, table, data, key_field=None, mode="insert"):
            captured["db_config"] = db_config
            captured["key_field"] = key_field
            captured["mode"] = mode
            return {"rows": 1}

        monkeypatch.setattr(ah, "push_to_db", fake_push)

        class FakeConn:
            db_type = "oracle"
            db_config = {"host": "h", "password": "enc:SECRET"}

        class FakeMapping:
            target_table = "T"
            target_endpoint = None
            mapping_config = {"key_field": "ID"}

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(ah._push_target(FakeConn(), FakeMapping(), [{"ID": 1}]))
        finally:
            loop.close()
        assert captured["db_config"]["password"] == "enc:SECRET"
        # 异步通道与同步通道同口径：映射配置了 key_field 即 upsert（审查 P1 修复）
        assert captured["key_field"] == "ID"
        assert captured["mode"] == "upsert"


class TestDraftInvalidationPrereqs:
    """B7 修复前提：transform_verified 回退 probing 是合法状态迁移"""

    def test_transform_verified_can_go_probing(self):
        from app.models.sync import PROBE_TRANSITIONS
        assert "probing" in PROBE_TRANSITIONS["transform_verified"]

    def test_e2e_verified_cannot_go_probing(self):
        # e2e_verified 不允许直接回退——draft 端点对其应 409 拒绝
        from app.models.sync import PROBE_TRANSITIONS
        assert "probing" not in PROBE_TRANSITIONS["e2e_verified"]


class TestDeleteSyncEmptyKeyGuard:
    """P0 防护：映射未覆盖 key 字段时删除同步必须跳过，不得清空目标表"""

    def test_source_keys_empty_skips_delete(self, monkeypatch):
        # 构造最小同步上下文：直接测 sync_handler 内部的删差判定逻辑
        # 通过 monkeypatch 让 push/锁/幂等都通过，映射的 transformed 不含 key_field
        import app.services.sync_handler as sh

        class FakeBuffer:
            id = "buf-1"
            status = None
            error_message = None
            data_hash = None
            transformed_data = None
            processed_at = None

        class FakeConn:
            id = "c1"
            db_type = "oracle"
            protocol = "rest"

        class FakeMapping:
            id = "m1"
            sync_mode = "full"
            source_endpoint = None
            target_endpoint = None
            source_table = "SRC"
            target_table = "TBL"
            mapping_config = {"fields": [{"source": "name", "target": "NAME"}],
                              "delete_sync": True, "key_field": "ROW_ID"}
            incremental_field = None
            source_headers = None
            target_headers = None

        class FakeTask:
            id = "t1"
            conflict_strategy = None
            fallback_to_async = False
            last_run_at = None
            last_sync_value = None
            task_type = "scheduled"

        monkeypatch.setattr(sh.lock, "acquire_lock", AsyncMock(return_value="tok"))
        monkeypatch.setattr(sh.lock, "refresh_lock", AsyncMock(return_value=True))
        monkeypatch.setattr(sh.lock, "release_lock", AsyncMock(return_value=True))
        monkeypatch.setattr(sh, "is_duplicate", AsyncMock(return_value=False))
        monkeypatch.setattr(sh, "mark_processed", AsyncMock())
        monkeypatch.setattr(sh, "rate_limit", AsyncMock(return_value=True))
        monkeypatch.setattr(sh, "record_sync", MagicMock())
        monkeypatch.setattr(sh, "record_api_call", MagicMock())
        monkeypatch.setattr(sh, "_fetch_source", AsyncMock(return_value=[{"name": "a"}]))
        monkeypatch.setattr(sh, "_push_target", AsyncMock(return_value={"rows": 1}))
        monkeypatch.setattr(sh, "log_execution", AsyncMock())

        session = MagicMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))
        # _gdc 断言：绝不应构造目标连接器去删差
        gdc = MagicMock()
        monkeypatch.setattr("app.connectors.database.get_database_connector", gdc)

        import asyncio
        result = asyncio.new_event_loop().run_until_complete(
            sh.sync_transfer(session, FakeTask(), FakeMapping(),
                             FakeConn(), FakeConn()))
        assert result["status"] == "success"
        gdc.assert_not_called()
