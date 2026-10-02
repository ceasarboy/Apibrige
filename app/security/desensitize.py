"""查询脱敏 — API 返回前将敏感字段替换为掩码

策略:
    - 默认完全脱敏：敏感值替换为 "***"
    - 部分保留模式（retain_edges=True）：保留前4后4位，中间用 "***" 替换
      便于用户识别凭证是否更新，但不泄露完整内容
    - 仅对 auth_config 中的敏感字段脱敏，其他字段（如 token_url、client_id）保留
"""
from __future__ import annotations

from app.security.crypto import SENSITIVE_FIELDS, is_encrypted, sensitive_auth_fields


# 脱敏后的占位符
_MASK_FULL = "***"
_MASK_PARTIAL_KEEP = 4  # 部分保留模式下保留的前后字符数


def mask_value(value: str, retain_edges: bool = False) -> str:
    """对单个值脱敏

    Args:
        value: 原始值
        retain_edges: True 时保留前4后4字符；False 时完全脱敏

    Returns:
        脱敏后的值
    """
    if not value:
        return value
    # 已加密的值（带 enc: 前缀）直接脱敏为占位符
    if is_encrypted(value):
        return _MASK_FULL
    if not retain_edges or len(value) <= _MASK_PARTIAL_KEEP * 2:
        return _MASK_FULL
    return f"{value[:_MASK_PARTIAL_KEEP]}{_MASK_FULL}{value[-_MASK_PARTIAL_KEEP:]}"


def desensitize_auth_config(
    auth_type: str | None,
    auth_config: dict | None,
    retain_edges: bool = False,
) -> dict | None:
    """脱敏 auth_config 中的敏感字段

    Args:
        auth_type: 认证类型
        auth_config: 认证配置 dict（可能是加密后的）
        retain_edges: 是否保留前后字符（部分脱敏）

    Returns:
        新 dict，敏感字段已脱敏；auth_config 为 None 时返回 None
    """
    if not auth_config:
        return auth_config
    # auth_type 为空不再早退：sensitive_auth_fields 对"无类型但带配置"已
    # fail-closed（全量按 custom 处理），否则内联探针快照明文回显（审查 P1）

    sensitive = sensitive_auth_fields(auth_type, auth_config)
    if not sensitive:
        return dict(auth_config)

    result = dict(auth_config)
    for field in sensitive:
        if field in result and result[field]:
            result[field] = mask_value(str(result[field]), retain_edges)
    return result


def desensitize_connection(connection: dict, retain_edges: bool = False) -> dict:
    """脱敏连接记录（dict 形式，用于 API 响应）

    Args:
        connection: 连接记录 dict，需含 auth_type 和 auth_config 字段
        retain_edges: 是否保留前后字符

    Returns:
        脱敏后的连接 dict（新对象，不修改入参）
    """
    result = dict(connection)
    from app.security.headers import mask_headers
    result['headers'] = mask_headers(result.get('headers'))
    result["auth_config"] = desensitize_auth_config(
        result.get("auth_type"),
        result.get("auth_config"),
        retain_edges,
    )
    # db_config 中的 password 也脱敏
    if result.get("db_config"):
        db_config = dict(result["db_config"])
        if "password" in db_config and db_config["password"]:
            db_config["password"] = mask_value(str(db_config["password"]), retain_edges)
        result["db_config"] = db_config
    return result


def desensitize_connections(
    connections: list[dict],
    retain_edges: bool = False,
) -> list[dict]:
    """批量脱敏连接列表"""
    return [desensitize_connection(c, retain_edges) for c in connections]


# 键名命中即视为敏感（探针样本/任意业务记录的通用规则）
_SENSITIVE_KEY_HINTS = (
    "password", "passwd", "pwd", "token", "secret", "api_key", "apikey",
    "access_key", "private_key", "authorization",
    "phone", "mobile", "email", "id_card", "idcard", "ssn", "sign", "appkey", "cookie",
    "credential", "authkey", "bearer", "jwt", "session",
)   # 注意：不收孤字 "auth"——author/authors（作者）会被误脱敏（2026-10-01 复审修正）

import re as _re

# 提示词去分隔符归一（api_key→apikey），与键名同规则后做包含匹配
_HINT_NORM = tuple(_re.sub(r"[^a-z0-9]", "", h) for h in _SENSITIVE_KEY_HINTS)


def key_hits_sensitive(key) -> bool:
    """键名归一化匹配：小写 + 去掉非字母数字后查提示词。

    归一只增不减命中面（"app-key"/"App-Key"/"app_key" 与 "appkey" 同判），
    不会放过任何旧规则能拦下的键。
    """
    k = _re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(h in k for h in _HINT_NORM)


def _looks_like_url_key(key_l: str, value: str) -> bool:
    """键名尾缀 url/uri/endpoint/link/href → 做 URL 查询串清洗（值任意形态）。

    原实现只认 url/base_url 两个键，endpoint/link 里的 sign/appKey/token 直穿
    （审查 P2）；复审修正：不再要求 http(s) 前缀——ws:// 与相对路径
    （/api/x?sign=…）的 query 同样会带凭据，清洗失败由调用方兜底原样返回。
    """
    return key_l.endswith(("url", "uri", "endpoint", "link", "href"))


def desensitize_record(data, retain_edges: bool = False):
    """递归脱敏任意业务记录（探针样本落库前套用，task 3.11）

    dict：键名命中敏感提示词的字符串值替换为掩码；
    list：逐项递归；其他标量原样返回。不修改入参。
    """
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            key_l = str(key).lower()
            if key_l in ('headers', 'source_headers', 'target_headers') and isinstance(value, dict):
                from app.security.headers import mask_headers
                result[key] = mask_headers(value)
            elif isinstance(value, str) and _looks_like_url_key(key_l, value):
                from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
                try:
                    url = urlsplit(value)
                    # url.port 对非法端口抛 ValueError（数据驱动的 500，审查 P3）——
                    # 异常时保留原 netloc
                    try:
                        netloc = url.hostname + (f':{url.port}' if url.port else '')                             if url.hostname else url.netloc
                    except ValueError:
                        netloc = url.netloc
                    result[key] = urlunsplit((url.scheme, netloc, url.path,
                                              urlencode([(k, '***') for k, _ in parse_qsl(url.query)]), ''))
                except Exception:
                    result[key] = value   # 清洗失败不阻断主流程，原值返回
            elif key_hits_sensitive(key):
                result[key] = _MASK_FULL if value else value
            else:
                result[key] = desensitize_record(value, retain_edges)
        return result
    if isinstance(data, list):
        return [desensitize_record(item, retain_edges) for item in data]
    return data


def redact_text(text: str | None):
    if text is None:
        return None
    import re
    # 两段式（审查 P2）：先掩 `Bearer <token>` 的值（否则键值段会把 Bearer 本身
    # 当值掩掉、真实令牌留在后面）；再掩 键=值/键:值/JSON 引号形态。
    # 值字符类排除引号与 JSON 结构符——"token": "abc" 与 app-key/appKey 变体同判。
    text = re.sub(r'(?i)(bearer\s+)[^\s,;"}\']+', r'\1***', text)
    return re.sub(
        r'(?i)((?:password|passwd|pwd|token|secret|sign|app[_-]?key|api[_-]?key|'
        r'access[_-]?key|authorization|cookie|credential)[\w-]*"?\s*[=:]\s*"?)'
        r'[^\s,;"}\']+',
        r'\1***', text)
