"""apibridge CLI 契约测试（task 6.2/6.3/6.5/6.12）

--json 输出为稳定契约：每个子命令的字段集在此钉死，改字段名必须改这里。
HTTP 层整体替身化，不依赖真实服务端。
"""
import json
from unittest.mock import patch

import pytest

from app.probe import cli


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("APIBRIDGE_SERVER", "http://server.example:8000")
    monkeypatch.setenv("APIBRIDGE_ADMIN_TOKEN", "tok")
    yield


def _patch_http(routes: dict, calls: list | None = None):
    """HTTP 替身：routes = {(method, path): (status, body)}；支持前缀匹配"""
    recorded = calls if calls is not None else []

    def _request(method, path, body=None, query=None):
        recorded.append((method, path, body))
        for (m, prefix), (status, payload) in routes.items():
            if m == method and path.startswith(prefix):
                return status, payload
        return 404, {"detail": {"code": "not_found", "message": path}}

    return patch.object(cli, "_http", side_effect=_request)


def _run(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr().out
    return code, json.loads(captured) if captured.strip() else None


class TestEnvContract:
    def test_missing_server_env_exit4(self, monkeypatch, capsys):
        """task 6.1：缺环境变量 → 退出码 4 并指明缺哪个"""
        monkeypatch.delenv("APIBRIDGE_SERVER", raising=False)
        monkeypatch.delenv("APIBRIDGE_ADMIN_TOKEN", raising=False)
        code, out = _run(capsys, "review", "list")
        assert code == cli.EXIT_USAGE
        assert out["error"]["code"] == "missing_env"
        assert "APIBRIDGE_SERVER" in out["error"]["message"]

    def test_rejects_token_arg(self, env, capsys):
        """task 6.5：拒绝 --token 参数并指向环境变量"""
        code, out = _run(capsys, "review", "list", "--token", "x")
        assert code == cli.EXIT_USAGE
        assert out["error"]["code"] == "token_arg_forbidden"

    def test_envelope_has_schema_version(self, env, capsys):
        with _patch_http({("GET", "/api/v1/probes"): (200, [])}):
            code, out = _run(capsys, "review", "list")
        assert out["schema_version"] == cli.SCHEMA_VERSION
        assert out["ok"] is True
        assert out["error"] is None


class TestSubcommandContracts:
    def test_probe_contract(self, env, capsys):
        calls: list = []
        session_body = {"id": "s-1", "status": "draft"}
        l1_body = {"id": "s-1", "status": "L1_reachable",
                   "l1_result": {"source": {"status": "ok", "latency_ms": 12},
                                 "target": {"status": "ok", "latency_ms": 30}}}
        with _patch_http({("POST", "/api/v1/probes/s-1/probe"): (200, l1_body),
                          ("POST", "/api/v1/probes"): (201, session_body)}, calls):
            code, out = _run(capsys, "probe", "--source-url", "https://s",
                             "--source-endpoint", "/u", "--target-url", "https://t",
                             "--target-endpoint", "/c")
        assert code == cli.EXIT_OK
        assert set(out) == {"schema_version", "ok", "data", "error"}
        assert set(out["data"]) == {"session_id", "status", "source", "target", "next"}
        assert out["data"]["source"]["status"] == "ok"
        # task 6.6：不创建连接（无 /connections 调用）
        assert all("/connections" not in c[1] for c in calls)

    def test_infer_contract(self, env, capsys):
        body = {"source_schema": {"fields": [
            {"path": "a", "type": "string", "nullable": False, "is_array": False, "sample_value": "x"}]}}
        with _patch_http({("POST", "/api/v1/probes/s-1/infer"): (200, body)}):
            code, out = _run(capsys, "infer", "--session", "s-1")
        assert code == cli.EXIT_OK
        assert set(out["data"]) == {"session_id", "fields", "next"}
        f = out["data"]["fields"][0]
        assert set(f) == {"path", "type", "nullable", "is_array"} | {"sample_value"}

    def test_draft_fail_exit2(self, env, capsys):
        """task 6.3：验证未通过 → 退出码 2"""
        body = {"valid": False, "fields": [], "issues": [
            {"code": "unknown_source_field", "path": "x", "message": "m"}],
            "missing_required": []}
        with _patch_http({("POST", "/api/v1/probes/s-1/draft"): (200, body)}):
            code, out = _run(capsys, "draft", "--session", "s-1")
        assert code == cli.EXIT_VERIFY_FAILED
        assert out["ok"] is False

    def test_draft_with_fields_json(self, env, capsys):
        body = {"valid": True, "fields": [{"source": "a", "target": "b"}],
                "issues": [], "missing_required": []}
        with _patch_http({("POST", "/api/v1/probes/s-1/draft"): (200, body)}):
            code, out = _run(capsys, "draft", "--session", "s-1",
                             "--fields", '[{"source":"a","target":"b"}]')
        assert code == cli.EXIT_OK
        assert out["data"]["fields"] == [{"source": "a", "target": "b"}]

    def test_verify_l2_pass(self, env, capsys):
        body = {"status": "transform_verified",
                "l2_result": {"level": "L2", "valid": True, "contract_issues": []}}
        with _patch_http({("POST", "/api/v1/probes/s-1/verify"): (200, body)}):
            code, out = _run(capsys, "verify", "--session", "s-1", "--level", "L2")
        assert code == cli.EXIT_OK
        assert set(out["data"]) == {"session_id", "status", "level", "valid",
                                    "contract_issues", "next"}

    def test_verify_l3_unauthorized_maps_403_to_exit3(self, env, capsys):
        """task 6.3：被拒绝（鉴权/状态）→ 退出码 3"""
        body = {"detail": {"code": "requires_human_authorization",
                           "message": "需要人工授权"}}
        with _patch_http({("POST", "/api/v1/probes/s-1/verify"): (403, body)}):
            code, out = _run(capsys, "verify", "--session", "s-1", "--level", "L3")
        assert code == cli.EXIT_REJECTED
        assert out["error"]["code"] == "requires_human_authorization"

    def test_verify_failure_has_next_action(self, env, capsys):
        """task 6.4：失败输出带 next_action"""
        body = {"detail": {"code": "requires_human_authorization",
                           "message": "需要人工授权"}}
        with _patch_http({("POST", "/api/v1/probes/s-1/verify"): (403, body)}):
            code, out = _run(capsys, "verify", "--session", "s-1", "--level", "L3")
        assert out["error"]["next_action"]
        assert "待确认清单" in out["error"]["next_action"]

    def test_apply_below_l2_exit3(self, env, capsys):
        """task 6.10：未达 L2 提交 → 退出码 3"""
        body = {"detail": {"code": "below_l2_threshold", "message": "未达到 L2",
                           "next_action": "先 verify --level L2"}}
        with _patch_http({("POST", "/api/v1/probes/s-1/submit"): (409, body)}):
            code, out = _run(capsys, "protocol-apply", "--session", "s-1")
        assert code == cli.EXIT_REJECTED
        assert out["error"]["code"] == "below_l2_threshold"

    def test_apply_ok_zero_bridge(self, env, capsys):
        calls: list = []
        with _patch_http({("POST", "/api/v1/probes/s-1/submit"):
                          (200, {"status": "pending_review"})}, calls):
            code, out = _run(capsys, "protocol-apply", "--session", "s-1")
        assert code == cli.EXIT_OK
        assert out["data"]["status"] == "pending_review"
        # task 6.10：apply 不建桥（只调 submit 一个端点）
        assert len(calls) == 1 and "submit" in calls[0][1]

    def test_review_approve_forbidden(self, env, capsys):
        """task 6.12：review approve 指路管理界面且退出码 3"""
        code, out = _run(capsys, "review", "approve")
        assert code == cli.EXIT_REJECTED
        assert "管理界面" in out["error"]["message"]
        assert out["error"]["next_action"]

    def test_review_list_contract(self, env, capsys):
        body = [{"id": "s-1", "status": "pending_review", "level": "L2",
                 "protocol": "rest", "created_at": "2026-09-03T00:00:00"}]
        with _patch_http({("GET", "/api/v1/probes"): (200, body)}):
            code, out = _run(capsys, "review", "list")
        assert code == cli.EXIT_OK
        item = out["data"]["items"][0]
        assert set(item) == {"session_id", "status", "level", "protocol", "created_at"}

    def test_session_show_passes_through(self, env, capsys):
        body = {"id": "s-1", "status": "transform_verified"}
        with _patch_http({("GET", "/api/v1/probes/s-1"): (200, body)}):
            code, out = _run(capsys, "session", "show", "--session", "s-1")
        assert code == cli.EXIT_OK
        assert out["data"]["status"] == "transform_verified"

    def test_server_unreachable_exit4(self, env, capsys):
        with patch.object(cli, "_http",
                          side_effect=SystemExit(cli.EXIT_USAGE)):
            # _http 内部 _fail 后 sys.exit(4)
            with pytest.raises(SystemExit) as exc:
                cli._http("GET", "/api/v1/probes")
        assert exc.value.code == cli.EXIT_USAGE


class TestObserveCommands:
    """只读观察命令：fields（字段定义+字典标记）/ forms list（表单注册表）"""

    def test_fields_hap_contract(self, env, capsys):
        body = {"kind": "hap", "worksheet_id": "ws1",
                "fields": [{"path": "productCode", "name": "产品编码",
                            "type": "文本", "nullable": False,
                            "primary_key": False, "readable": True,
                            "writable": True, "saved": False}]}
        with _patch_http({("GET", "/api/v1/connections/c-1/fields"): (200, body)}):
            code, out = _run(capsys, "fields", "--connection-id", "c-1",
                             "--worksheet-id", "ws1")
        assert code == cli.EXIT_OK
        data = out["data"]
        assert data["kind"] == "hap" and data["count"] == 1
        field = data["fields"][0]
        assert set(field) == {"path", "name", "type", "nullable", "primary_key",
                              "readable", "writable", "saved"}
        assert "draft" in data["next"]  # 指向草稿用途

    def test_fields_db_table_list_then_next(self, env, capsys):
        """DB 连接未指定表 → 返回表清单 + 下一步提示带 --table"""
        body = {"kind": "db", "tables": ["T_USER", "T_ORDER"], "fields": None}
        with _patch_http({("GET", "/api/v1/connections/c-2/fields"): (200, body)}):
            code, out = _run(capsys, "fields", "--connection-id", "c-2")
        assert code == cli.EXIT_OK
        assert out["data"]["tables"] == ["T_USER", "T_ORDER"]
        assert out["data"]["fields"] is None
        assert "--table" in out["data"]["next"]

    def test_fields_none_schema_with_hint(self, env, capsys):
        body = {"kind": "none", "fields": None,
                "hint": "该协议无自动字段定义，请在映射中手动填写源字段"}
        with _patch_http({("GET", "/api/v1/connections/c-3/fields"): (200, body)}):
            code, out = _run(capsys, "fields", "--connection-id", "c-3")
        assert code == cli.EXIT_OK
        assert out["data"]["fields"] == [] and out["data"]["count"] == 0
        assert "手动填写" in out["data"]["hint"]

    def test_fields_unauthorized_exit3(self, env, capsys):
        with _patch_http({("GET", "/api/v1/connections/c-1/fields"):
                          (401, {"detail": {"code": "unauthorized",
                                            "message": "token 不对"}})}):
            code, out = _run(capsys, "fields", "--connection-id", "c-1")
        assert code == cli.EXIT_REJECTED
        assert out["ok"] is False and out["error"]["code"] == "unauthorized"

    def test_forms_list_contract(self, env, capsys):
        body = [{"id": "f-1", "form_key": "ws1", "display_name": "产品目录",
                 "original_name": "产品目录", "connection_id": "c-1",
                 "connection_name": "明道云2", "source": "auto",
                 "bridge_count": 1,
                 "bridges": [{"mapping_id": "m-1", "mapping_name": "桥A",
                              "side": "target", "peer_connection_id": "c-2",
                              "peer_connection_name": "Oracle",
                              "peer_form": "product"}],
                 "created_at": "2026-09-12T00:00:00"}]
        with _patch_http({("GET", "/api/v1/forms"): (200, body)}):
            code, out = _run(capsys, "forms", "list")
        assert code == cli.EXIT_OK
        assert out["data"]["count"] == 1
        item = out["data"]["items"][0]
        assert set(item) == {"form_key", "display_name", "original_name",
                             "connection_id", "connection_name", "source",
                             "bridge_count", "bridges"}
        assert set(item["bridges"][0]) == {"mapping_name", "side",
                                           "peer_connection_name", "peer_form"}
        assert "管理界面" in out["data"]["next"]  # 写能力边界提示

    def test_forms_list_search_filters_client_side(self, env, capsys):
        body = [{"form_key": "ws1", "display_name": "产品目录", "remark": ""},
                {"form_key": "ws2", "display_name": "交流记录", "remark": ""}]
        with _patch_http({("GET", "/api/v1/forms"): (200, body)}):
            code, out = _run(capsys, "forms", "list", "--search", "产品")
        assert code == cli.EXIT_OK
        assert out["data"]["count"] == 1
        assert out["data"]["items"][0]["display_name"] == "产品目录"
