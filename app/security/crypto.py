"""AES-256-Fernet 加密/解密 — 用于敏感认证信息存储

使用 cryptography 库的 Fernet 实现 AES-128-CBC + HMAC-SHA256 的对称加密。
Fernet 密钥为 32 字节 base64 编码字符串（由 Fernet.generate_key() 生成）。

约定:
    - 密钥从环境变量 APIBRIDGE_ENCRYPT_KEY 读取（app.config.settings.encrypt_key）
    - 敏感字段加密后以 "enc:" 前缀标记，便于识别和兼容旧数据
    - 解密时如无 "enc:" 前缀视为明文（向后兼容）
"""
from __future__ import annotations

from typing import Any

from app.config import settings


# 加密值前缀，用于区分加密值和明文
_ENCRYPTED_PREFIX = "enc:"

# Fernet 单例（延迟初始化，密钥缺失时按需抛错）
_fernet = None


class CryptoError(Exception):
    """加密/解密错误"""


def _get_fernet():
    """获取 Fernet 实例（单例），密钥缺失时抛错"""
    global _fernet
    if _fernet is None:
        if not settings.encrypt_key:
            raise CryptoError(
                "encrypt_key_missing: 加密密钥未配置，请设置环境变量 APIBRIDGE_ENCRYPT_KEY "
                "（可用 `python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` 生成）"
            )
        try:
            from cryptography.fernet import Fernet
            _fernet = Fernet(settings.encrypt_key.encode())
        except Exception as e:
            raise CryptoError(f"Fernet 密钥无效: {e}") from e
    return _fernet


def encrypt(plaintext: str) -> str:
    """加密字符串

    Args:
        plaintext: 明文

    Returns:
        加密后的字符串，带 "enc:" 前缀

    Raises:
        CryptoError: 密钥未配置或无效
    """
    if not plaintext:
        return plaintext
    # 已加密的不重复加密
    if plaintext.startswith(_ENCRYPTED_PREFIX):
        return plaintext
    f = _get_fernet()
    token = f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{_ENCRYPTED_PREFIX}{token}"


def decrypt(ciphertext: str) -> str:
    """解密字符串

    Args:
        ciphertext: 密文（带 "enc:" 前缀）或明文（直接返回）

    Returns:
        明文

    Raises:
        CryptoError: 密钥未配置、无效或解密失败
    """
    if not ciphertext:
        return ciphertext
    # 无前缀视为明文（向后兼容旧数据）
    if not ciphertext.startswith(_ENCRYPTED_PREFIX):
        return ciphertext
    f = _get_fernet()
    token = ciphertext[len(_ENCRYPTED_PREFIX):].encode("ascii")
    try:
        return f.decrypt(token).decode("utf-8")
    except Exception as e:
        raise CryptoError(f"解密失败（密钥不匹配或数据损坏）: {e}") from e


# ============================================================
# auth_config 字段级加密
# ============================================================

# 各认证类型中的敏感字段名（按 auth_type 分类）
SENSITIVE_FIELDS: dict[str, list[str]] = {
    "api_key": ["key_value"],
    "bearer": ["token"],
    "basic": ["password"],
    "oauth2": ["client_secret"],
    "sign": ["secret_key"],
}

# 合法认证类型白名单（与 ConnectionValidation.normalize_auth 同口径）
KNOWN_AUTH_TYPES = frozenset(SENSITIVE_FIELDS) | {"custom"}


def sensitive_auth_fields(auth_type: str | None, config: dict | None) -> list[str]:
    if auth_type == 'custom':
        return list(config or {})
    if auth_type and auth_type not in KNOWN_AUTH_TYPES:
        # fail-closed（审查 P1）：未知/错拼的 auth_type（如 "Bearer"）原实现返回空
        # 集合 → 脱敏与加密双双失效、明文凭据落库；改为按 custom 全量处理
        return list(config or {})
    if not auth_type and config:
        # auth_type 缺失但带了配置：同样按 custom 全量处理（宁可多脱敏）
        return list(config or {})
    hap_keys = {'app_key', 'appkey', 'hap_appkey', 'hap-appkey', 'sign', 'hap_sign', 'hap-sign'}
    return list(set(SENSITIVE_FIELDS.get(auth_type or '', [])) |
                {k for k in (config or {}) if k.lower() in hap_keys})


def encrypt_auth_config(auth_type: str | None, auth_config: dict | None) -> dict | None:
    """加密 auth_config 中的敏感字段

    Args:
        auth_type: 认证类型
        auth_config: 认证配置 dict

    Returns:
        新 dict，敏感字段已加密；auth_config 为 None 时返回 None

    注意：auth_type 为空不再早退——sensitive_auth_fields 对"无类型但带配置"
    fail-closed（全量按 custom 处理），否则内联探针传 auth_type=null 时凭证明文落库。
    """
    if not auth_config:
        return auth_config

    sensitive = sensitive_auth_fields(auth_type, auth_config)
    if not sensitive:
        return dict(auth_config)

    result = dict(auth_config)
    for field in sensitive:
        if field in result and result[field]:
            result[field] = encrypt(str(result[field]))
    return result


def decrypt_auth_config(auth_type: str | None, auth_config: dict | None) -> dict | None:
    """解密 auth_config 中的敏感字段

    Args:
        auth_type: 认证类型
        auth_config: 认证配置 dict（可能含加密字段）

    Returns:
        新 dict，敏感字段已解密；auth_config 为 None 时返回 None
    """
    if not auth_config:
        return auth_config
    # 与加密侧对称的 fail-closed（复审修正）：auth_type 为空时按全量键解密——
    # 加密侧（encrypt_auth_config）对该形态全量加密，解密早退会把 enc: 密文
    # 当真实凭据注入，该连接所有 API 调用认证必挂
    sensitive = sensitive_auth_fields(auth_type, auth_config)
    if not sensitive:
        return dict(auth_config)

    result = dict(auth_config)
    for field in sensitive:
        if field in result and result[field]:
            result[field] = decrypt(str(result[field]))
    return result


def is_encrypted(value: Any) -> bool:
    """判断值是否已加密"""
    return isinstance(value, str) and value.startswith(_ENCRYPTED_PREFIX)


_DB_SENSITIVE_FIELDS = ("password",)


def encrypt_db_config(db_config: dict | None) -> dict | None:
    """加密 db_config 中的密码字段（入库前调用）"""
    if not db_config:
        return db_config
    result = dict(db_config)
    for field in _DB_SENSITIVE_FIELDS:
        if result.get(field):
            result[field] = encrypt(str(result[field]))
    return result


def decrypt_db_config(db_config: dict | None) -> dict | None:
    """解密 db_config 中的密码字段（构造连接前调用）"""
    if not db_config:
        return db_config
    result = dict(db_config)
    for field in _DB_SENSITIVE_FIELDS:
        if result.get(field):
            result[field] = decrypt(str(result[field]))
    return result


