"""Header 名大小写无关；公开头之外的值默认保密。"""
import re
from app.security.crypto import encrypt, decrypt, CryptoError

PUBLIC_HEADERS = {'content-type', 'accept', 'accept-encoding', 'accept-language', 'user-agent'}


def validate_headers(headers: dict | None):
    if headers is not None and not isinstance(headers, dict):
        raise ValueError('headers 必须为对象')
    seen = set()
    for key, value in (headers or {}).items():
        if not isinstance(key, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
            raise ValueError('Header 名称无效')
        if not isinstance(value, str) or '\r' in value or '\n' in value:
            raise ValueError(f'Header {key} 必须是无换行字符串')
        if key.lower() in seen:
            raise ValueError(f'Header 大小写重复: {key}')
        seen.add(key.lower())
    return headers


def merge_headers(*layers: dict | None) -> dict:
    result = {}
    for layer in layers:
        for key, value in (layer or {}).items():
            previous = next((k for k in result if k.lower() == key.lower()), None)
            if previous is not None:
                del result[previous]
            result[key] = value
    return result


def store_headers(headers: dict | None, old: dict | None = None):
    if headers is None:
        return None
    validate_headers(headers)
    prior = {k.lower(): v for k, v in (old or {}).items()}
    result = {}
    for key, value in headers.items():
        if value == '***':
            if key.lower() not in prior:
                raise CryptoError(f'Header {key} 无可保留的凭据')
            result[key] = prior[key.lower()]
        else:
            result[key] = value if key.lower() in PUBLIC_HEADERS else encrypt(value)
    return result


def decrypt_headers(headers: dict | None) -> dict:
    return {k: decrypt(v) for k, v in (headers or {}).items()}


def mask_headers(headers: dict | None):
    if headers is None:
        return None
    return {k: (v if k.lower() in PUBLIC_HEADERS and isinstance(v, str) and not v.startswith('enc:') else '***') for k, v in headers.items()}
