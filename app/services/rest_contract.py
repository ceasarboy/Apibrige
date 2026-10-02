"""无网络副作用的 REST 请求和响应契约。"""
import copy
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from app.adapters.errors import AdapterError

MISSING = object()


def pointer_parts(path: str) -> list[str]:
    if not isinstance(path, str) or not path.startswith('/') or re.search(r'~(?![01])', path):
        raise ValueError('路径必须是 JSON Pointer，例如 /data')
    return [part.replace('~1', '/').replace('~0', '~') for part in path[1:].split('/')]


def pointer_get(body, path):
    for key in pointer_parts(path):
        if not isinstance(body, dict) or key not in body:
            return MISSING
        body = body[key]
    return body


def wrap_records(records: list, path: str | None, extra: dict):
    parts = pointer_parts(path)
    if any(not p or p.isdigit() or p in ('*', '-') for p in parts):
        raise ValueError('records_path 仅支持对象字段路径')
    body = copy.deepcopy(extra)
    node = body
    for key in parts[:-1]:
        if key not in node:
            node[key] = {}
        if not isinstance(node[key], dict):
            raise AdapterError('request_body_conflict', '包装路径父级不是对象')
        node = node[key]
    if parts[-1] in node:
        raise AdapterError('request_body_conflict', 'extra_body 已占用记录数组路径')
    node[parts[-1]] = records
    return body


def resolve_target_url(base: str, endpoint: str) -> str:
    root, ep = urlsplit(base), urlsplit(endpoint)
    if root.scheme not in ('http', 'https') or not root.netloc or root.username or root.password or root.fragment:
        raise AdapterError('invalid_target_url', '需要不含凭据或片段的 HTTP(S) 根地址')
    if ep.scheme or ep.netloc or endpoint.startswith('//') or ep.fragment:
        raise AdapterError('invalid_target_endpoint', '目标端点必须为相对路径')
    if not endpoint:
        return base
    path = ep.path if ep.path.startswith('/') else root.path.rstrip('/') + '/' + ep.path
    query = dict(parse_qsl(root.query, keep_blank_values=True))
    query.update(parse_qsl(ep.query, keep_blank_values=True))
    return urlunsplit((root.scheme, root.netloc, path, urlencode(query), ''))


def prepare_request(config: dict, endpoint: str, data, method=None) -> dict:
    from app.services.sync_config import RequestConfig
    from app.security.headers import merge_headers
    from app.services.api_client import resolve_variables
    endpoint = resolve_variables(endpoint)
    version = config.get('config_version', 1)
    configured = config.get('request')
    request = RequestConfig.model_validate(configured or {}).model_dump()
    records = data if isinstance(data, list) else [data]
    if request['max_records'] is not None and len(records) > request['max_records']:
        raise AdapterError('batch_limit_exceeded', f"记录数 {len(records)} 超过 {request['max_records']}")
    skip = False
    if not records and (configured is not None or version == 2):
        if request['on_empty'] == 'error':
            raise AdapterError('empty_records', '无可发送记录')
        skip = True
    mode = request['mode']
    if mode != 'raw' and any(not isinstance(r, dict) for r in records):
        raise AdapterError('invalid_records', '记录必须为对象')
    if mode == 'object':
        if len(records) != 1 and not skip:
            raise AdapterError('invalid_record_count', '单对象模式必须恰好一条记录')
        body = records[0] if records else None
    elif mode == 'wrapped_array':
        body = wrap_records(records, request['records_path'], request['extra_body'])
    else:
        body = records if mode == 'array' else data
    headers = merge_headers({'Content-Type': 'application/json'}, config.get('headers'))
    content_type = next(v for k, v in headers.items() if k.lower() == 'content-type')
    if version == 2 and not (content_type.split(';')[0].strip().lower() == 'application/json' or content_type.split(';')[0].strip().lower().endswith('+json')):
        raise AdapterError('content_type_conflict', 'JSON请求必须使用JSON Content-Type')
    # 两个版本统一走 resolve_target_url：v1 原样放行绝对 endpoint 的旧规则与
    # httpx "绝对 URL 覆盖 base_url" 语义相同——绝对端点会把目标连接的凭据
    # （Authorization/签名参数）发往任意主机并绕过 SSRF 白名单（审查 P1）
    url = resolve_target_url(config['base_url'], endpoint)
    return {'method': method or config.get('method') or 'POST', 'url': url, 'headers': headers, 'json': body, 'skipped': skip, 'record_count': len(records)}


def strict_equal(a, b):
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(strict_equal(x, y) for x, y in zip(a, b))
    return a == b


def validate_response(status: int, body, config: dict) -> bool:
    from app.services.sync_config import ResponseConfig
    cfg = ResponseConfig.model_validate(config or {}).model_dump()
    if not 200 <= status < 300 or (cfg['http_statuses'] is not None and status not in cfg['http_statuses']):
        raise AdapterError('target_http_failed', f'HTTP {status}', 'target_http', details={'status_code': status})
    if cfg['success_mode'] == 'http_only':
        return False
    verdicts = []
    for condition in cfg['conditions']:
        actual = pointer_get(body, condition['path'])
        if actual is MISSING:
            if condition['operator'] != 'exists':
                raise AdapterError('response_contract_failed', f"响应缺少 {condition['path']}", 'target_business', details={'status_code': status})
            verdicts.append(False)
            continue
        expected, operator = condition['value'], condition['operator']
        if operator == 'exists':
            verdicts.append(True)
        elif operator == 'in':
            verdicts.append(any(strict_equal(actual, v) for v in expected))
        else:
            verdicts.append(type(actual) is type(expected) and (strict_equal(actual, expected) if operator == 'eq' else not strict_equal(actual, expected)))
    if not (all(verdicts) if cfg['match'] == 'all' else any(verdicts)):
        msg = pointer_get(body, cfg['message_path']) if cfg['message_path'] else MISSING
        raise AdapterError('target_business_failed', str(msg)[:500] if isinstance(msg, str) else '响应未满足业务成功条件', 'target_business', details={'status_code': status})
    return True
