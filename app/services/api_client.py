"""API 客户端 — Header 构建（认证注入 + 变量解析）

传输职责已归协议适配器（app/adapters/），本模块仅保留 header 构建与
变量解析能力，供各调用方复用。
"""
from typing import Any
from app.security.crypto import decrypt_auth_config
from app.services.variable_engine import VariableEngine

# 全局变量引擎实例
_variable_engine = VariableEngine()


def resolve_variables(value: Any, extra_vars: dict[str, Any] | None = None) -> Any:
    """解析字符串/dict/list 中的 {{变量}}"""
    return _variable_engine.resolve(value, extra_vars)


def merge_headers(base_headers: dict[str, str] | None,
                  override_headers: dict[str, str] | None) -> dict[str, str]:
    """合并请求头：override 覆盖 base 中同名 key"""
    merged = dict(base_headers or {})
    if override_headers:
        merged.update(override_headers)
    return merged


def build_headers(conn, mapping=None,
                  extra_vars: dict[str, Any] | None = None) -> dict[str, str]:
    """构建完整请求头（连接级 + 映射级覆盖 + 变量解析）

    优先级: 映射级 headers > 连接级 headers > auth 生成 headers
    """
    headers: dict[str, str] = {}

    # 1. auth 生成的 headers（basic/bearer/api_key/custom/oauth2）
    # 库中存储的是加密态凭据，注入前必须解密（task 1.1）
    if conn.auth_config and conn.auth_type:
        decrypted_auth = decrypt_auth_config(conn.auth_type, conn.auth_config)
        headers.update(_auth_to_headers(conn.auth_type, decrypted_auth))

    # 2. 连接级自定义 headers
    if conn.headers:
        headers.update(conn.headers)

    # 3. 映射级覆盖
    if mapping:
        if hasattr(mapping, 'source_headers') and mapping.source_headers is not None:
            headers.update(mapping.source_headers)
        if hasattr(mapping, 'target_headers') and mapping.target_headers is not None:
            headers.update(mapping.target_headers)

    # 4. 解析变量
    if extra_vars:
        headers = _variable_engine.resolve_dict(headers, extra_vars)
    else:
        headers = _variable_engine.resolve_dict(headers, {})

    return headers


def _auth_to_headers(auth_type: str, auth_config: dict) -> dict[str, str]:
    """将认证配置转换为请求头"""
    auth_type = auth_type.lower()
    if auth_type == "bearer":
        token = auth_config.get("token", "")
        return {"Authorization": f"Bearer {token}"}
    elif auth_type == "basic":
        import base64
        username = auth_config.get("username", "")
        password = auth_config.get("password", "")
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}
    elif auth_type == "api_key":
        key_name = auth_config.get("key_name", "X-API-Key")
        key_value = auth_config.get("key_value", "")
        location = auth_config.get("location", "header")
        if location == "header":
            return {key_name: key_value}
        return {}
    elif auth_type == "sign":
        # sign 类型通常需要在 adapters 中处理
        return {}
    elif auth_type == "custom":
        # custom: 直接把 auth_config 的键值对作为 headers
        return {str(k): str(v) for k, v in auth_config.items()}
    return {}
