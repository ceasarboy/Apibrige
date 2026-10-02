"""REST 协议适配器 — 基于 httpx 的 HTTP/REST 实现"""
import time
import asyncio

import httpx
from typing import Any
from app.adapters.base import ProtocolAdapter
from app.adapters.auth import AuthInjector
from app.adapters.errors import AdapterError
from app.services.rest_contract import prepare_request, validate_response


class RestAdapter(ProtocolAdapter):
    """REST 协议适配器

    支持 GET/POST/PUT/PATCH/DELETE 方法，JSON 请求体和查询参数。
    通过 AuthInjector 自动注入认证信息。
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.auth = AuthInjector(
            config.get("auth_type"),
            config.get("auth_config"),
        )

    @staticmethod
    def _contains_watermark(value):
        if isinstance(value, str):
            return '{{last_watermark}}' in value
        if isinstance(value, dict):
            return any(RestAdapter._contains_watermark(item) for item in value.values())
        if isinstance(value, list):
            return any(RestAdapter._contains_watermark(item) for item in value)
        return False

    @classmethod
    def _drop_unbound_watermark(cls, value, runtime_vars):
        if runtime_vars.get('last_watermark') is not None:
            return value
        if isinstance(value, dict):
            return {key: cls._drop_unbound_watermark(item, runtime_vars)
                    for key, item in value.items() if not cls._contains_watermark(item)}
        if isinstance(value, list):
            return [cls._drop_unbound_watermark(item, runtime_vars)
                    for item in value if not cls._contains_watermark(item)]
        return value

    async def fetch_data(self, endpoint: str, params: dict | None = None) -> Any:
        if self.config.get('read') is not None:
            return await self._read_records(endpoint, params)
        headers, params = await self.auth.inject_async(self.headers, params)
        # GET 读数据是幂等操作：按连接 retry_count 做有限重试（只针对瞬时错误：
        # 传输层失败/限流/网关错误）。此前 retry_count 是无消费点的死配置（审查 P2）
        retries = max(0, int(self.config.get('retry_count') or 0))
        attempts = retries + 1
        last_error: Exception | None = None
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout) as client:
            for attempt in range(attempts):
                try:
                    response = await client.get(endpoint, params=params)
                    if int(response.status_code) < 500:
                        response.raise_for_status()
                        return response.json()
                    last_error = httpx.HTTPStatusError(
                        f"HTTP {response.status_code}", request=response.request,
                        response=response)
                except (httpx.TransportError, httpx.HTTPStatusError) as e:
                    if isinstance(e, httpx.TransportError):
                        last_error = e
                    elif e.response is not None and int(e.response.status_code) < 500:
                        raise           # 4xx 非瞬时，重试无意义
                    last_error = e
                if attempt < attempts - 1:
                    await asyncio.sleep(min(2 ** attempt, 5))
        raise last_error if last_error else RuntimeError("unreachable")

    async def _read_records(self, endpoint: str, params: dict | None):
        import copy
        import json
        from app.config import settings
        from app.adapters.rest_pagination import SourcePagination, pagination_error
        from app.services.sync_config import SourceReadConfig
        from app.services.api_client import resolve_variables
        from app.services.rest_contract import pointer_get, resolve_target_url
        from urllib.parse import parse_qsl, urlsplit
        read = SourceReadConfig.model_validate(self.config['read'])
        url = resolve_target_url(self.base_url, resolve_variables(endpoint))
        runtime_vars = self.config.get('_runtime_vars') or {}
        query = {**dict(parse_qsl(urlsplit(url).query, keep_blank_values=True)),
                 **resolve_variables(self._drop_unbound_watermark(read.query, runtime_vars), runtime_vars), **(params or {})}
        payload = resolve_variables(self._drop_unbound_watermark(read.body, runtime_vars), runtime_vars) if read.body is not None else None
        paging = SourcePagination(read.pagination)
        names = read.pagination.parameter_names()
        if read.pagination.location == 'query' and names & query.keys():
            pagination_error('固定查询参数或端点 URL 与分页参数重名，请移除重复配置')
        paged = read.pagination.mode != 'none'
        # 行/字节上限不再以 paged 为条件（审查 P2）：非分页单页大响应同样无界
        # 累积，`incremental_round_too_large` 保护对默认配置形同虚设
        max_rows = self.config.get('_scan_max_rows', settings.incremental_max_rows)
        max_bytes = self.config.get('_scan_max_bytes', settings.incremental_max_bytes)
        records, total_bytes = [], 0
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for _ in range(read.pagination.max_pages if paged else 1):
                page_query, page_body = dict(query), copy.deepcopy(payload)
                dynamic = paging.parameters()
                if read.pagination.location == 'body' and paged:
                    page_body = {**(page_body or {}), **dynamic}
                else:
                    page_query.update(dynamic)
                headers, authenticated_query = await self.auth.inject_async(self.headers, page_query)
                if read.pagination.location == 'query' and any(
                        authenticated_query.get(key) != page_query.get(key) for key in names):
                    pagination_error('分页参数与认证查询参数冲突，请使用不同的参数名')
                async with client.stream(read.method, url, headers=headers, params=authenticated_query or None, json=page_body) as response:
                    if not 200 <= response.status_code < 300:
                        raise AdapterError('source_http_failed', f'源接口 HTTP {response.status_code}', 'source_read')
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        total_bytes += len(chunk)
                        if max_bytes is not None and total_bytes > max_bytes:
                            raise AdapterError('incremental_round_too_large', '源响应累计超过字节上限', 'source_read')
                        content.extend(chunk)
                    try:
                        body = json.loads(content)
                    except (ValueError, UnicodeError):
                        raise AdapterError('source_response_invalid', '源响应不是有效 JSON，请检查源接口响应格式', 'source_read') from None
                    response_config = self.config.get('response') or {}
                    try:
                        validate_response(response.status_code, body, response_config)
                    except AdapterError as exc:
                        if exc.code == 'target_business_failed':
                            message = pointer_get(body, response_config['message_path']) if response_config.get('message_path') else None
                            message = self._source_error_message(message, headers, authenticated_query)
                            raise AdapterError('source_business_failed', message, 'source_read',
                                               details={'status_code': response.status_code}) from None
                        if exc.code == 'target_http_failed':
                            raise AdapterError('source_http_failed', f'源接口 HTTP {response.status_code} 不符合成功状态配置',
                                               'source_read', details={'status_code': response.status_code}) from None
                        raise AdapterError('source_response_contract_failed', '源响应不符合成功判断契约，请检查响应字段路径和类型',
                                           'source_read', details={'status_code': response.status_code}) from None
                rows = pointer_get(body, read.records_path) if read.records_path else body
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise AdapterError('source_records_invalid', '源响应数组路径不存在或内容不是记录对象数组', 'source_read')
                if max_rows is not None and len(records) + len(rows) > max_rows:
                    raise AdapterError('incremental_round_too_large', '源响应累计超过条数上限', 'source_read')
                done = paging.finished(body, rows)
                records.extend(rows)
                if done:
                    return records
        pagination_error('达到最大读取页数但尚未确认结束，请检查分页配置或提高最大页数')

    def _source_error_message(self, message, headers, query):
        """只展示配置 message_path 的短文本；先清除已知请求秘密再截断。"""
        if not isinstance(message, str) or not message.strip():
            return '源接口业务拒绝：响应未满足业务成功条件'
        from app.security.desensitize import redact_text, _SENSITIVE_KEY_HINTS
        from app.security.headers import PUBLIC_HEADERS
        from urllib.parse import quote, quote_plus
        secrets = set()
        def collect(value):
            if isinstance(value, dict):
                for item in value.values():
                    collect(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    collect(item)
            elif isinstance(value, str) and value:
                secrets.add(value)
        collect(self.auth.auth_config)
        collect({k: v for k, v in headers.items() if k.lower() not in PUBLIC_HEADERS})
        collect({k: v for k, v in (query or {}).items() if any(hint in k.lower() for hint in _SENSITIVE_KEY_HINTS)})
        for secret in sorted(secrets, key=len, reverse=True):
            for encoded in (secret, quote(secret, safe=''), quote_plus(secret)):
                message = message.replace(encoded, '***')
        message = redact_text(message)
        return '源接口业务拒绝：' + ' '.join(message.split())[:400]

    def prepare_push_request(self, endpoint: str, data: Any, method: str | None = None) -> dict:
        return prepare_request(self.config, endpoint, data, method)

    async def push_data(self, endpoint: str, data: Any, method: str | None = None) -> dict:
        prepared = self.prepare_push_request(endpoint, data, method)
        if prepared['skipped']:
            return {'status_code': None, 'body': None, 'skipped': True}
        try:
            headers, params = await self.auth.inject_async(prepared['headers'])
        except ValueError as exc:
            raise AdapterError('unsupported_auth_type', str(exc), 'request_build') from None
        try:
            async with httpx.AsyncClient(headers=headers, timeout=self.timeout) as client:
                response = await client.request(prepared['method'], prepared['url'], json=prepared['json'], params=params or None)
        except httpx.TransportError:
            raise AdapterError('outcome_unknown', '请求中断，目标是否已提交未知；请核对后重试', 'target_http') from None
        cfg = self.config.get('response') or {}
        if not 200 <= response.status_code < 300:
            raise AdapterError('target_http_failed', f'HTTP {response.status_code}', 'target_http', details={'status_code': response.status_code})
        try:
            body = (response.text if cfg.get('body_format') == 'text' else response.json()) if response.content else None
        except ValueError:
            raise AdapterError('response_contract_failed', '响应不是有效JSON', 'target_business', details={'status_code': response.status_code}) from None
        try:
            verified = validate_response(response.status_code, body, cfg)
        except AdapterError as exc:
            # 抹去本次请求中的任意凭据回显，避免第三方 msg 泄漏 Header 值。
            from app.security.headers import PUBLIC_HEADERS
            message = str(exc)
            secrets = [v for k, v in headers.items() if k.lower() not in PUBLIC_HEADERS]
            secrets += list((self.auth.auth_config or {}).values())
            for value in secrets:
                if isinstance(value, str) and value:
                    message = message.replace(value, '***')
            raise AdapterError(exc.code, message, exc.stage, details={**exc.details, 'url':prepared['url'], 'record_count':prepared['record_count']}) from None
        from app.security.headers import PUBLIC_HEADERS
        from app.security.desensitize import desensitize_record
        secrets = [v for k, v in headers.items() if k.lower() not in PUBLIC_HEADERS] + list((self.auth.auth_config or {}).values())
        def redact_echo(value):
            if isinstance(value, dict):
                return {k: redact_echo(v) for k, v in value.items()}
            if isinstance(value, list):
                return [redact_echo(v) for v in value]
            if isinstance(value, str):
                for secret in secrets:
                    if isinstance(secret, str) and secret:
                        value = value.replace(secret, '***')
            return value
        request_summary = desensitize_record({'url':prepared['url'], 'method':prepared['method'], 'headers':headers,
                                              'record_count':prepared['record_count'], 'json':prepared['json']})
        return {'status_code': response.status_code, 'body': desensitize_record(redact_echo(body)), 'business_verified': verified,
                'request':request_summary}

    async def test_connection(self) -> dict:
        start = time.time()
        request_url = f"{self.base_url}/health"
        try:
            headers, _ = await self.auth.inject_async(self.headers)
            async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=5) as client:
                resp = await client.get("/health")
                latency = int((time.time() - start) * 1000)
                details = {
                    "request": f"GET {request_url}",
                    "status_code": resp.status_code,
                    "response_excerpt": resp.text[:500],
                }
                if resp.status_code < 400:
                    return {"status": "ok", "latency_ms": latency, "error": None, **details}
                # 4xx 不再判 ok（审查 P3）：401/403 是最需要诊断的鉴权问题
                hint = ("鉴权失败（检查认证配置）" if resp.status_code in (401, 403)
                        else "探测端点不存在或不可达" if resp.status_code == 404
                        else "目标返回客户端错误")
                return {"status": "failed", "latency_ms": latency,
                        "error": f"HTTP {resp.status_code}：{hint}", **details}
        except Exception as e:
            return {"status": "failed",
                    "latency_ms": int((time.time() - start) * 1000), "error": str(e)}


# === 兼容层：保留原函数签名，已有代码和测试不破坏 ===

async def fetch_from_api(base_url: str, endpoint: str, headers: dict | None = None,
                         params: dict | None = None, timeout: float = 30.0) -> Any:
    """从源 API 拉取数据（兼容旧接口）"""
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        response = await client.get(endpoint, params=params)
        response.raise_for_status()
        return response.json()


async def push_to_api(base_url: str, endpoint: str, data: Any, headers: dict | None = None,
                      method: str = "POST", timeout: float = 30.0) -> dict:
    """推送数据到目标 API（兼容旧接口）"""
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        response = await client.request(method, endpoint, json=data)
        response.raise_for_status()
        return {"status_code": response.status_code, "body": response.json() if response.content else None}
