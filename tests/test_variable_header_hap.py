"""变量引擎 + Header 合并 + HAP 适配器 测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.variable_engine import VariableEngine
from app.services.api_client import build_headers, merge_headers, resolve_variables


# ============================================================
# 变量引擎测试
# ============================================================

class TestVariableEngine:
    """变量解析引擎"""

    def test_timestamp_resolves_to_digits(self):
        engine = VariableEngine()
        result = engine.resolve("ts={{timestamp}}")
        assert result.startswith("ts=")
        ts = result[3:]
        assert ts.isdigit()
        assert len(ts) == 13  # milliseconds

    def test_uuid_generates_unique(self):
        engine = VariableEngine()
        u1 = engine.resolve("{{uuid}}")
        u2 = engine.resolve("{{uuid}}")
        assert u1 != u2
        assert len(u1) == 36  # UUID format

    def test_random_generates_6_digits(self):
        engine = VariableEngine()
        r = engine.resolve("{{random}}")
        assert len(r) == 6
        assert r.isdigit()

    def test_index_counter_increments(self):
        engine = VariableEngine()
        assert engine.resolve("{{index}}") == "1"
        assert engine.resolve("{{index}}") == "2"
        assert engine.resolve("{{index}}") == "3"

    def test_reset_counter(self):
        engine = VariableEngine()
        engine.resolve("{{index}}")
        engine.resolve("{{index}}")
        engine.reset_counter()
        assert engine.resolve("{{index}}") == "1"

    def test_now_resolves_to_iso(self):
        engine = VariableEngine()
        result = engine.resolve("{{now}}")
        assert "T" in result  # ISO format has T separator

    def test_now_date_format(self):
        engine = VariableEngine()
        result = engine.resolve("{{now_date}}")
        assert len(result) == 10  # YYYY-MM-DD
        assert "-" in result

    def test_custom_variable(self):
        engine = VariableEngine()
        result = engine.resolve("Hello {{name}}", {"name": "World"})
        assert result == "Hello World"

    def test_multiple_variables_in_string(self):
        engine = VariableEngine()
        result = engine.resolve("{{a}} and {{b}}", {"a": "1", "b": "2"})
        assert result == "1 and 2"

    def test_unknown_variable_kept_literal(self):
        engine = VariableEngine()
        result = engine.resolve("{{unknown_var}}")
        assert result == "{{unknown_var}}"

    def test_resolve_dict(self):
        engine = VariableEngine()
        data = {"host": "https://{{env}}.api.com", "ts": "{{timestamp}}"}
        resolved = engine.resolve_dict(data, {"env": "test"})
        assert resolved["host"] == "https://test.api.com"
        assert resolved["ts"].isdigit()

    def test_resolve_list(self):
        engine = VariableEngine()
        data = ["{{index}}", "{{index}}"]
        result = engine.resolve(data)
        assert result == ["1", "2"]

    def test_resolve_nested_dict(self):
        engine = VariableEngine()
        data = {"headers": {"X-Request-Id": "{{uuid}}"}}
        result = engine.resolve(data)
        assert len(result["headers"]["X-Request-Id"]) == 36

    def test_resolve_int_passthrough(self):
        engine = VariableEngine()
        assert engine.resolve(42) == 42

    def test_resolve_none_passthrough(self):
        engine = VariableEngine()
        assert engine.resolve(None) is None

    def test_resolve_bool_passthrough(self):
        engine = VariableEngine()
        assert engine.resolve(True) is True

    def test_resolve_float_passthrough(self):
        engine = VariableEngine()
        assert engine.resolve(3.14) == 3.14


# ============================================================
# Header 合并测试
# ============================================================

class TestHeaderMerging:
    """Header 合并逻辑"""

    def test_merge_override_replaces(self):
        result = merge_headers({"A": "1", "B": "2"}, {"B": "3"})
        assert result == {"A": "1", "B": "3"}

    def test_merge_adds_new_keys(self):
        result = merge_headers({"A": "1"}, {"C": "3"})
        assert result == {"A": "1", "C": "3"}

    def test_merge_base_none_returns_override(self):
        result = merge_headers(None, {"X": "v"})
        assert result == {"X": "v"}

    def test_merge_override_none_returns_base(self):
        result = merge_headers({"X": "v"}, None)
        assert result == {"X": "v"}

    def test_merge_both_none_returns_empty(self):
        result = merge_headers(None, None)
        assert result == {}

    def test_build_headers_conn_only(self):
        conn = MagicMock(
            auth_type="bearer", auth_config={"token": "abc123"},
            headers={"X-Custom": "val"},
        )
        mapping = None
        headers = build_headers(conn, mapping)
        assert headers["Authorization"] == "Bearer abc123"
        assert headers["X-Custom"] == "val"

    def test_build_headers_custom_auth(self):
        """custom auth_type: auth_config keys become headers"""
        conn = MagicMock(
            auth_type="custom",
            auth_config={"HAP-Appkey": "key123", "HAP-Sign": "sign456"},
            headers={},
        )
        headers = build_headers(conn)
        assert headers["HAP-Appkey"] == "key123"
        assert headers["HAP-Sign"] == "sign456"

    def test_build_headers_basic_auth(self):
        conn = MagicMock(
            auth_type="basic",
            auth_config={"username": "user", "password": "pass"},
            headers={},
        )
        headers = build_headers(conn)
        import base64
        expected = base64.b64encode(b"user:pass").decode()
        assert headers["Authorization"] == f"Basic {expected}"

    def test_build_headers_api_key_header(self):
        conn = MagicMock(
            auth_type="api_key",
            auth_config={"key_name": "X-API-Key", "key_value": "secret", "location": "header"},
            headers={},
        )
        headers = build_headers(conn)
        assert headers["X-API-Key"] == "secret"

    def test_build_headers_api_key_query_not_in_headers(self):
        conn = MagicMock(
            auth_type="api_key",
            auth_config={"key_name": "key", "key_value": "val", "location": "query"},
            headers={},
        )
        headers = build_headers(conn)
        assert "key" not in headers  # query params not in headers

    def test_build_headers_mapping_overrides_conn(self):
        conn = MagicMock(auth_type=None, auth_config=None, headers={"X-Old": "old_val"})
        mapping = MagicMock(source_headers={"X-Old": "new_val"}, target_headers={})
        headers = build_headers(conn, mapping)
        assert headers["X-Old"] == "new_val"

    def test_build_headers_variable_resolution(self):
        conn = MagicMock(
            auth_type="bearer", auth_config={"token": "{{dynamic_token}}"},
            headers={},
        )
        headers = build_headers(conn, extra_vars={"dynamic_token": "resolved123"})
        assert headers["Authorization"] == "Bearer resolved123"

    def test_build_headers_no_auth_no_headers(self):
        conn = MagicMock(auth_type=None, auth_config=None, headers={})
        headers = build_headers(conn)
        assert headers == {}

    def test_resolve_variables_helper(self):
        result = resolve_variables("url={{base}}/{{endpoint}}", {"base": "api.com", "endpoint": "v1"})
        assert result == "url=api.com/v1"


# ============================================================
# HAP 适配器测试
# ============================================================

class TestHapAdapter:
    """HAP V3 协议适配器"""

    def test_get_auth_params(self):
        from app.adapters.hap import HapV3Adapter
        adapter = HapV3Adapter({"base_url": "https://api.mingdao.com",
                                 "hap_appkey": "test_appkey",
                                 "hap_sign": "test_sign"})
        params = adapter._auth_params()
        assert params["appKey"] == "test_appkey"
        assert params["sign"] == "test_sign"

    def test_get_auth_params_from_custom_header_names(self):
        """UI 自定义 Header 形态（HAP-Appkey/HAP-Sign）同样识别"""
        from app.adapters.hap import HapV3Adapter
        adapter = HapV3Adapter({"base_url": "https://api.mingdao.com",
                                 "auth_config": {"HAP-Appkey": "k1",
                                                  "HAP-Sign": "s1"}})
        params = adapter._auth_params()
        assert params["appKey"] == "k1"
        assert params["sign"] == "s1"

    def test_get_auth_params_no_config(self):
        from app.adapters.hap import HapV3Adapter
        adapter = HapV3Adapter({"base_url": "https://api.mingdao.com"})
        params = adapter._auth_params()
        assert params == {}

    def test_auth_from_auth_config(self):
        from app.adapters.hap import HapV3Adapter
        adapter = HapV3Adapter({"base_url": "https://api.mingdao.com",
                                 "auth_config": {"hap_appkey": "key_from_auth",
                                                  "hap_sign": "sign_from_auth"}})
        params = adapter._auth_params()
        assert params["appKey"] == "key_from_auth"
        assert params["sign"] == "sign_from_auth"

    def test_build_worksheet_url(self):
        from app.adapters.hap import HapV3Adapter
        url = HapV3Adapter.build_worksheet_url("ws123")
        assert url == "/v3/app/worksheets/ws123/rows"

    def test_build_filter_single_condition(self):
        from app.adapters.hap import HapV3Adapter
        f = HapV3Adapter.build_filter([
            {"field": "status", "operator": "eq", "value": ["active"]},
        ])
        assert f["type"] == "group"
        assert f["logic"] == "AND"
        assert len(f["children"]) == 1
        assert f["children"][0]["field"] == "status"

    def test_build_filter_multiple_conditions(self):
        from app.adapters.hap import HapV3Adapter
        f = HapV3Adapter.build_filter([
            {"field": "age", "operator": "gt", "value": ["18"]},
            {"field": "name", "operator": "contains", "value": ["张"]},
        ], logic="OR")
        assert f["logic"] == "OR"
        assert len(f["children"]) == 2

    def test_build_update_fields(self):
        from app.adapters.hap import HapV3Adapter
        fields = HapV3Adapter.build_update_fields({"customer_name": "测试"})
        assert fields == [{"id": "customer_name", "value": "测试"}]

    def test_build_update_fields_multiple(self):
        from app.adapters.hap import HapV3Adapter
        fields = HapV3Adapter.build_update_fields({"name": "A", "age": 25})
        assert len(fields) == 2
        assert {"id": "age", "value": 25} in fields

    def test_protocol_name(self):
        from app.adapters.hap import HapV3Adapter
        assert HapV3Adapter.PROTOCOL_NAME == "hapv3"

    def test_factory_registration_hap(self):
        from app.adapters.factory import ProtocolAdapterFactory
        protocols = ProtocolAdapterFactory.get_supported_protocols()
        assert "hap" in protocols
        assert "hapv3" in protocols

    @pytest.mark.asyncio
    async def test_test_connection_failed(self):
        from app.adapters.hap import HapV3Adapter
        adapter = HapV3Adapter({"base_url": "https://api.mingdao.com"})
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_response = MagicMock(status_code=401, text="Unauthorized")
            mock_get.return_value = mock_response
            result = await adapter.test_connection()
            assert result["status"] == "failed"
            assert "401" in result["error"]

    @pytest.mark.asyncio
    async def test_test_connection_success(self):
        from app.adapters.hap import HapV3Adapter
        adapter = HapV3Adapter({"base_url": "https://api.mingdao.com",
                                 "hap_appkey": "key", "hap_sign": "sign"})
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_response = MagicMock(status_code=200)
            mock_response.json.return_value = {
                "success": True, "data": {"name": "TestApp"}}
            mock_get.return_value = mock_response
            result = await adapter.test_connection()
            assert result["status"] == "ok"
            assert result["app_name"] == "TestApp"
