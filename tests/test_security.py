"""认证管理单元测试 — 覆盖加密/解密、脱敏、字段级加密、端到端认证注入"""
import base64
import pytest
from unittest.mock import patch, MagicMock

# 生成测试用 Fernet 密钥（32 字节 base64 编码）
_TEST_KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()


@pytest.fixture(autouse=True)
def reset_crypto_singleton():
    """每个测试前重置 Fernet 单例，避免密钥变化后仍用旧实例"""
    import app.security.crypto as crypto_mod
    crypto_mod._fernet = None
    yield
    crypto_mod._fernet = None


@pytest.fixture
def with_encrypt_key():
    """注入测试用加密密钥"""
    with patch("app.security.crypto.settings") as mock_settings:
        mock_settings.encrypt_key = _TEST_KEY
        yield mock_settings


# ============================================================
# 加密/解密单元测试
# ============================================================

class TestCryptoBasic:
    """验证: 加密后解密还原原文，密钥缺失时报错"""

    def test_encrypt_decrypt_roundtrip(self, with_encrypt_key):
        """加密后解密还原原文"""
        from app.security.crypto import encrypt, decrypt

        plaintext = "my_secret_api_key_12345"
        ciphertext = encrypt(plaintext)

        # 加密后带前缀
        assert ciphertext.startswith("enc:")
        assert ciphertext != plaintext
        # 解密还原
        assert decrypt(ciphertext) == plaintext

    def test_encrypt_empty_string(self, with_encrypt_key):
        """空字符串不加密"""
        from app.security.crypto import encrypt, decrypt

        assert encrypt("") == ""
        assert decrypt("") == ""

    def test_encrypt_idempotent(self, with_encrypt_key):
        """已加密的值不重复加密"""
        from app.security.crypto import encrypt

        once = encrypt("secret123")
        twice = encrypt(once)
        assert once == twice

    def test_decrypt_plaintext_returns_as_is(self, with_encrypt_key):
        """无 enc: 前缀的值视为明文直接返回（向后兼容）"""
        from app.security.crypto import decrypt

        assert decrypt("plain_value") == "plain_value"

    def test_missing_key_raises(self):
        """密钥未配置时加密报错"""
        with patch("app.security.crypto.settings") as mock_settings:
            mock_settings.encrypt_key = ""
            from app.security.crypto import encrypt, CryptoError

            with pytest.raises(CryptoError, match="加密密钥未配置"):
                encrypt("secret")

    def test_missing_key_raises_on_decrypt_encrypted(self):
        """密钥未配置时解密已加密值报错"""
        with patch("app.security.crypto.settings") as mock_settings:
            mock_settings.encrypt_key = ""
            from app.security.crypto import decrypt, CryptoError

            with pytest.raises(CryptoError, match="加密密钥未配置"):
                decrypt("enc:some_token")

    def test_invalid_key_raises(self):
        """无效密钥格式报错"""
        with patch("app.security.crypto.settings") as mock_settings:
            mock_settings.encrypt_key = "not_a_valid_fernet_key"
            from app.security.crypto import encrypt, CryptoError

            with pytest.raises(CryptoError, match="Fernet 密钥无效"):
                encrypt("secret")

    def test_decrypt_with_wrong_key_raises(self, with_encrypt_key):
        """用错误密钥解密报错"""
        from app.security.crypto import encrypt, decrypt, CryptoError

        ciphertext = encrypt("secret_data")

        # 切换到另一个密钥
        import app.security.crypto as crypto_mod
        crypto_mod._fernet = None
        other_key = base64.urlsafe_b64encode(b"abcdef0123456789abcdef0123456789").decode()
        with patch("app.security.crypto.settings") as mock_settings:
            mock_settings.encrypt_key = other_key
            with pytest.raises(CryptoError, match="解密失败"):
                decrypt(ciphertext)

    def test_is_encrypted(self, with_encrypt_key):
        """判断值是否已加密"""
        from app.security.crypto import encrypt, is_encrypted

        assert is_encrypted(encrypt("x")) is True
        assert is_encrypted("plaintext") is False
        assert is_encrypted("") is False
        assert is_encrypted(None) is False


# ============================================================
# 字段级 auth_config 加密
# ============================================================

class TestAuthConfigEncryption:
    """验证: 各认证类型的敏感字段正确加密/解密"""

    def test_api_key_encryption(self, with_encrypt_key):
        """api_key: key_value 加密，key_name/location 保留"""
        from app.security.crypto import encrypt_auth_config, decrypt_auth_config, is_encrypted

        config = {"key_name": "X-API-Key", "key_value": "secret123", "location": "header"}
        encrypted = encrypt_auth_config("api_key", config)

        assert encrypted["key_name"] == "X-API-Key"
        assert encrypted["location"] == "header"
        assert is_encrypted(encrypted["key_value"])
        assert encrypted["key_value"] != "secret123"

        # 解密还原
        decrypted = decrypt_auth_config("api_key", encrypted)
        assert decrypted["key_value"] == "secret123"

    def test_bearer_encryption(self, with_encrypt_key):
        """bearer: token 加密"""
        from app.security.crypto import encrypt_auth_config, decrypt_auth_config

        config = {"token": "eyJhbGciOiJIUzI1NiJ9.payload.signature"}
        encrypted = encrypt_auth_config("bearer", config)
        assert encrypted["token"].startswith("enc:")
        assert decrypt_auth_config("bearer", encrypted)["token"] == config["token"]

    def test_basic_encryption(self, with_encrypt_key):
        """basic: password 加密，username 保留"""
        from app.security.crypto import encrypt_auth_config, decrypt_auth_config

        config = {"username": "admin", "password": "pass123"}
        encrypted = encrypt_auth_config("basic", config)
        assert encrypted["username"] == "admin"
        assert encrypted["password"].startswith("enc:")

        decrypted = decrypt_auth_config("basic", encrypted)
        assert decrypted["password"] == "pass123"

    def test_oauth2_encryption(self, with_encrypt_key):
        """oauth2: client_secret 加密，client_id/token_url/scopes 保留"""
        from app.security.crypto import encrypt_auth_config, decrypt_auth_config

        config = {
            "token_url": "https://auth.example.com/token",
            "client_id": "my_client_id",
            "client_secret": "super_secret",
            "scopes": "read write",
        }
        encrypted = encrypt_auth_config("oauth2", config)
        assert encrypted["token_url"] == config["token_url"]
        assert encrypted["client_id"] == "my_client_id"
        assert encrypted["scopes"] == "read write"
        assert encrypted["client_secret"].startswith("enc:")

        decrypted = decrypt_auth_config("oauth2", encrypted)
        assert decrypted["client_secret"] == "super_secret"

    def test_sign_encryption(self, with_encrypt_key):
        """sign: secret_key 加密，algorithm/sign_fields 保留"""
        from app.security.crypto import encrypt_auth_config, decrypt_auth_config

        config = {
            "algorithm": "hmac-sha256",
            "secret_key": "my_sign_secret",
            "sign_fields": ["timestamp", "nonce"],
        }
        encrypted = encrypt_auth_config("sign", config)
        assert encrypted["algorithm"] == "hmac-sha256"
        assert encrypted["sign_fields"] == ["timestamp", "nonce"]
        assert encrypted["secret_key"].startswith("enc:")

        decrypted = decrypt_auth_config("sign", encrypted)
        assert decrypted["secret_key"] == "my_sign_secret"

    def test_none_auth_config(self, with_encrypt_key):
        """auth_config 为 None 时返回 None"""
        from app.security.crypto import encrypt_auth_config, decrypt_auth_config

        assert encrypt_auth_config("api_key", None) is None
        assert decrypt_auth_config("api_key", None) is None

    def test_none_auth_type(self, with_encrypt_key):
        """auth_type 为 None 但带了配置 → fail-closed 全量加密（审查 P1）

        原行为"None 不加密原样返回"是内联探针明文凭据落库的根源，已改。
        """
        from app.security.crypto import encrypt_auth_config, is_encrypted

        config = {"key_value": "secret", "location": "header"}
        result = encrypt_auth_config(None, config)
        assert result is not config  # 返回新 dict
        assert is_encrypted(result["key_value"])
        assert config["key_value"] == "secret"  # 不改入参

    def test_empty_value_not_encrypted(self, with_encrypt_key):
        """空值不加密"""
        from app.security.crypto import encrypt_auth_config, is_encrypted

        config = {"key_name": "X-API-Key", "key_value": "", "location": "header"}
        encrypted = encrypt_auth_config("api_key", config)
        assert encrypted["key_value"] == ""
        assert not is_encrypted(encrypted["key_value"])


# ============================================================
# 脱敏测试
# ============================================================

class TestDesensitize:
    """验证: 脱敏功能正确隐藏敏感信息"""

    def test_mask_value_full(self):
        """完全脱敏：值替换为 ***"""
        from app.security.desensitize import mask_value

        assert mask_value("secret123") == "***"
        assert mask_value("very_long_secret_token_value") == "***"

    def test_mask_value_retain_edges(self):
        """部分保留：保留前4后4，中间用 ***"""
        from app.security.desensitize import mask_value

        result = mask_value("abcdefghijklmnop", retain_edges=True)
        assert result == "abcd***mnop"

    def test_mask_value_short_retain_edges(self):
        """短值即使 retain_edges 也完全脱敏"""
        from app.security.desensitize import mask_value

        assert mask_value("abc", retain_edges=True) == "***"
        assert mask_value("abcdefgh", retain_edges=True) == "***"  # 刚好8字符，不保留

    def test_mask_value_empty(self):
        """空值不脱敏"""
        from app.security.desensitize import mask_value

        assert mask_value("") == ""

    def test_mask_encrypted_value(self, with_encrypt_key):
        """已加密的值直接脱敏为 ***"""
        from app.security.desensitize import mask_value
        from app.security.crypto import encrypt

        encrypted = encrypt("secret")
        assert mask_value(encrypted) == "***"
        assert mask_value(encrypted, retain_edges=True) == "***"

    def test_desensitize_api_key(self):
        """脱敏 api_key 的 key_value"""
        from app.security.desensitize import desensitize_auth_config

        config = {"key_name": "X-API-Key", "key_value": "secret123", "location": "header"}
        result = desensitize_auth_config("api_key", config)

        assert result["key_name"] == "X-API-Key"
        assert result["key_value"] == "***"
        assert result["location"] == "header"

    def test_desensitize_bearer(self):
        """脱敏 bearer 的 token"""
        from app.security.desensitize import desensitize_auth_config

        config = {"token": "eyJhbGc.payload.sig"}
        result = desensitize_auth_config("bearer", config)
        assert result["token"] == "***"

    def test_desensitize_basic(self):
        """脱敏 basic 的 password，保留 username"""
        from app.security.desensitize import desensitize_auth_config

        config = {"username": "admin", "password": "pass123"}
        result = desensitize_auth_config("basic", config)
        assert result["username"] == "admin"
        assert result["password"] == "***"

    def test_desensitize_oauth2(self):
        """脱敏 oauth2 的 client_secret，保留 client_id/token_url"""
        from app.security.desensitize import desensitize_auth_config

        config = {
            "token_url": "https://auth.example.com/token",
            "client_id": "cid",
            "client_secret": "csec",
            "scopes": "read",
        }
        result = desensitize_auth_config("oauth2", config)
        assert result["token_url"] == "https://auth.example.com/token"
        assert result["client_id"] == "cid"
        assert result["client_secret"] == "***"
        assert result["scopes"] == "read"

    def test_desensitize_sign(self):
        """脱敏 sign 的 secret_key，保留 algorithm/sign_fields"""
        from app.security.desensitize import desensitize_auth_config

        config = {"algorithm": "md5", "secret_key": "sk", "sign_fields": ["ts"]}
        result = desensitize_auth_config("sign", config)
        assert result["algorithm"] == "md5"
        assert result["secret_key"] == "***"
        assert result["sign_fields"] == ["ts"]

    def test_desensitize_with_retain_edges(self):
        """部分保留脱敏"""
        from app.security.desensitize import desensitize_auth_config

        config = {"token": "abcdefghijklmnop"}
        result = desensitize_auth_config("bearer", config, retain_edges=True)
        assert result["token"] == "abcd***mnop"

    def test_desensitize_connection(self):
        """脱敏连接记录（含 db_config.password）"""
        from app.security.desensitize import desensitize_connection

        conn = {
            "id": "abc-123",
            "name": "MySQL源",
            "auth_type": "basic",
            "auth_config": {"username": "admin", "password": "secret"},
            "db_config": {"host": "localhost", "password": "db_pass"},
        }
        result = desensitize_connection(conn)

        assert result["id"] == "abc-123"
        assert result["name"] == "MySQL源"
        assert result["auth_config"]["username"] == "admin"
        assert result["auth_config"]["password"] == "***"
        assert result["db_config"]["host"] == "localhost"
        assert result["db_config"]["password"] == "***"
        # 原对象不被修改
        assert conn["auth_config"]["password"] == "secret"

    def test_desensitize_connections_batch(self):
        """批量脱敏"""
        from app.security.desensitize import desensitize_connections

        conns = [
            {"auth_type": "bearer", "auth_config": {"token": "t1"}},
            {"auth_type": "basic", "auth_config": {"username": "u", "password": "p"}},
        ]
        results = desensitize_connections(conns)
        assert results[0]["auth_config"]["token"] == "***"
        assert results[1]["auth_config"]["password"] == "***"


# ============================================================
# 端到端：加密存储 → 解密 → 认证注入
# ============================================================

class TestAuthEndToEnd:
    """验证: 加密存储的 auth_config 解密后能正确注入到请求"""

    def test_api_key_e2e(self, with_encrypt_key):
        """API Key 端到端: 加密存储 → 解密 → 注入 Header"""
        from app.security.crypto import encrypt_auth_config, decrypt_auth_config
        from app.adapters.auth import AuthInjector

        # 模拟用户提交的明文配置
        plain_config = {"key_name": "X-API-Key", "key_value": "secret123", "location": "header"}

        # 1. 加密存储
        stored = encrypt_auth_config("api_key", plain_config)
        assert stored["key_value"] != "secret123"

        # 2. 读取时解密
        decrypted = decrypt_auth_config("api_key", stored)
        assert decrypted["key_value"] == "secret123"

        # 3. 注入到请求
        auth = AuthInjector("api_key", decrypted)
        headers, params = auth.inject({})
        assert headers["X-API-Key"] == "secret123"

    def test_basic_e2e(self, with_encrypt_key):
        """Basic Auth 端到端"""
        from app.security.crypto import encrypt_auth_config, decrypt_auth_config
        from app.adapters.auth import AuthInjector

        plain_config = {"username": "admin", "password": "pass123"}
        stored = encrypt_auth_config("basic", plain_config)
        decrypted = decrypt_auth_config("basic", stored)

        auth = AuthInjector("basic", decrypted)
        headers, _ = auth.inject({})
        expected = base64.b64encode(b"admin:pass123").decode()
        assert headers["Authorization"] == f"Basic {expected}"

    def test_bearer_e2e(self, with_encrypt_key):
        """Bearer Token 端到端"""
        from app.security.crypto import encrypt_auth_config, decrypt_auth_config
        from app.adapters.auth import AuthInjector

        plain_config = {"token": "jwt_token_xyz"}
        stored = encrypt_auth_config("bearer", plain_config)
        decrypted = decrypt_auth_config("bearer", stored)

        auth = AuthInjector("bearer", decrypted)
        headers, _ = auth.inject({})
        assert headers["Authorization"] == "Bearer jwt_token_xyz"

    def test_sign_e2e(self, with_encrypt_key):
        """自定义签名端到端"""
        from app.security.crypto import encrypt_auth_config, decrypt_auth_config
        from app.adapters.auth import AuthInjector

        plain_config = {
            "algorithm": "md5",
            "secret_key": "my_secret",
            "sign_fields": ["timestamp"],
        }
        stored = encrypt_auth_config("sign", plain_config)
        decrypted = decrypt_auth_config("sign", stored)

        auth = AuthInjector("sign", decrypted)
        headers, _ = auth.inject({})
        assert "X-Sign" in headers
        assert "X-Timestamp" in headers


# ============================================================
# 场景: 加密存储 + 脱敏查询
# ============================================================

class TestStorageAndQueryScenario:
    """验证: 存储时加密，查询时脱敏，注入时解密"""

    def test_store_encrypt_query_desensitize_use_decrypt(self, with_encrypt_key):
        """完整场景: 用户存储 → 数据库（加密）→ API 查询（脱敏）→ 适配器使用（解密）"""
        from app.security.crypto import encrypt_auth_config, decrypt_auth_config
        from app.security.desensitize import desensitize_connection
        from app.adapters.auth import AuthInjector

        # 1. 用户提交明文配置（创建连接请求）
        user_input = {
            "name": "生产API",
            "auth_type": "api_key",
            "auth_config": {"key_name": "X-API-Key", "key_value": "prod_secret_key", "location": "header"},
        }

        # 2. 存储前加密
        stored_in_db = {
            **user_input,
            "auth_config": encrypt_auth_config(user_input["auth_type"], user_input["auth_config"]),
        }
        # 数据库中的 key_value 不是明文
        assert stored_in_db["auth_config"]["key_value"] != "prod_secret_key"
        assert stored_in_db["auth_config"]["key_value"].startswith("enc:")

        # 3. 查询时脱敏（API 返回给前端）
        api_response = desensitize_connection(stored_in_db)
        assert api_response["auth_config"]["key_value"] == "***"
        assert api_response["auth_config"]["key_name"] == "X-API-Key"

        # 4. 适配器使用时解密（从数据库读取后解密）
        decrypted = decrypt_auth_config(stored_in_db["auth_type"], stored_in_db["auth_config"])
        auth = AuthInjector(stored_in_db["auth_type"], decrypted)
        headers, _ = auth.inject({})
        assert headers["X-API-Key"] == "prod_secret_key"
