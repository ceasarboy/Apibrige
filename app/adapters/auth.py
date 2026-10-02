"""认证注入器 — 根据 auth_type 将认证信息注入到请求中"""
import base64
import hashlib
import hmac
import time
from typing import Any


class AuthInjector:
    """认证注入器

    根据 auth_type 将认证信息注入到适配器的请求 headers 或 params 中。
    支持: api_key / bearer / basic / oauth2 / sign
    """

    def __init__(self, auth_type: str | None, auth_config: dict | None):
        self.auth_type = None if auth_type in (None, '', 'none') else auth_type
        self.auth_config = auth_config or {}
        self._oauth2_token: str | None = None
        self._oauth2_expires: float = 0

    def inject(self, headers: dict, params: dict | None = None) -> tuple[dict, dict]:
        """注入认证信息到 headers 和 params

        Returns:
            (updated_headers, updated_params)
        """
        headers = dict(headers)
        params = dict(params) if params else {}

        if self.auth_type is None:
            return headers, params

        method = getattr(self, f"_inject_{self.auth_type}", None)
        if method is None:
            raise ValueError(f"不支持的认证类型: {self.auth_type}")

        from app.security.headers import merge_headers
        generated, params = method({}, params)
        return merge_headers(generated, headers), params

    def _inject_custom(self, headers: dict, params: dict) -> tuple[dict, dict]:
        from app.security.headers import validate_headers
        validate_headers(self.auth_config)
        return dict(self.auth_config), params

    async def inject_async(self, headers: dict, params: dict | None = None) -> tuple[dict, dict]:
        """异步注入认证信息（OAuth2 需要异步获取 Token）"""
        if self.auth_type == "oauth2":
            from app.security.headers import merge_headers
            generated, params = await self._inject_oauth2_async({}, params or {})
            return merge_headers(generated, headers), params
        return self.inject(headers, params)

    def _inject_api_key(self, headers: dict, params: dict) -> tuple[dict, dict]:
        key_name = self.auth_config.get("key_name", "X-API-Key")
        key_value = self.auth_config.get("key_value", "")
        location = self.auth_config.get("location", "header")

        if location == "query":
            params[key_name] = key_value
        else:
            headers[key_name] = key_value
        return headers, params

    def _inject_bearer(self, headers: dict, params: dict) -> tuple[dict, dict]:
        token = self.auth_config.get("token", "")
        headers["Authorization"] = f"Bearer {token}"
        return headers, params

    def _inject_basic(self, headers: dict, params: dict) -> tuple[dict, dict]:
        username = self.auth_config.get("username", "")
        password = self.auth_config.get("password", "")
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {credentials}"
        return headers, params

    async def _inject_oauth2_async(self, headers: dict, params: dict) -> tuple[dict, dict]:
        if self._oauth2_token is None or time.time() >= self._oauth2_expires:
            await self._refresh_oauth2_token()
        headers["Authorization"] = f"Bearer {self._oauth2_token}"
        return headers, params

    async def _refresh_oauth2_token(self):
        import httpx

        token_url = self.auth_config["token_url"]
        client_id = self.auth_config["client_id"]
        client_secret = self.auth_config["client_secret"]
        scopes = self.auth_config.get("scopes", "")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": scopes,
                },
            )
            resp.raise_for_status()
            token_data = resp.json()

        self._oauth2_token = token_data["access_token"]
        self._oauth2_expires = time.time() + token_data.get("expires_in", 3600) - 60

    def _inject_sign(self, headers: dict, params: dict) -> tuple[dict, dict]:
        algorithm = self.auth_config.get("algorithm", "md5")
        secret_key = self.auth_config.get("secret_key", "")
        sign_fields = self.auth_config.get("sign_fields", ["timestamp"])

        sign_data = {}
        for field in sign_fields:
            if field == "timestamp":
                sign_data[field] = str(int(time.time()))
            elif field == "nonce":
                sign_data[field] = str(hash(time.time()))

        raw = "".join(f"{k}{v}" for k, v in sorted(sign_data.items())) + secret_key

        if algorithm == "hmac-sha256":
            signature = hmac.new(secret_key.encode(), raw.encode(), hashlib.sha256).hexdigest()
        else:
            signature = hashlib.md5(raw.encode()).hexdigest()

        headers["X-Sign"] = signature
        for k, v in sign_data.items():
            headers[f"X-{k.capitalize()}"] = v

        return headers, params
