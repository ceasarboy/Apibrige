"""apibridge CLI — 外部 agent 的唯一入口（task 6.1-6.13）

设计约束（design.md 决策5）:
    - 只懂 HTTP + 参数解析 + 输出格式化，不含业务逻辑、不 import 服务端 core
    - 仅用 Python 标准库（urllib），可在任何装有 Python 3 的主机独立运行
    - token 与服务地址只从环境变量读取，拒绝 --token 参数
    - --json 输出为带 schema_version 的稳定契约（字段一经发布不改名）
    - 退出码: 0 成功 / 2 验证未通过 / 3 被拒绝（鉴权·预算·状态） / 4 调用错误
    - 每个失败输出 next_action

环境变量:
    APIBRIDGE_SERVER       服务地址，如 http://内网主机:8000
    APIBRIDGE_ADMIN_TOKEN  管理 token
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

SCHEMA_VERSION = "1.0"

EXIT_OK = 0
EXIT_VERIFY_FAILED = 2
EXIT_REJECTED = 3
EXIT_USAGE = 4

_NEXT_ACTIONS = {
    "unauthorized": "检查 APIBRIDGE_ADMIN_TOKEN 是否与服务端配置一致",
    "admin_token_missing": "服务端未配置 APIBRIDGE_ADMIN_TOKEN（生产环境不可用）",
    "probe_budget_exhausted": "预算耗尽：新建会话（apibridge probe）或联系管理员调整上限",
    "requires_human_authorization": "请在管理界面「待确认清单」中人工授权 L3 后重试",
    "below_l2_threshold": "先 apibridge verify --level L2 达到 transform_verified 再提交",
    "l2_stale": "L2 过期或映射被改：重新 apibridge verify --level L2",
    "invalid_transition": "按流程顺序执行：probe → infer → draft → verify → apply",
    "invalid_state": "按流程顺序执行：probe → infer → draft → verify → apply",
    "connection_config_required": "批准时需提供新连接配置，或在清单界面引用已有连接",
}


def _output(ok: bool, data=None, error: dict | None = None) -> None:
    """统一输出契约：schema_version + ok + data/error（task 6.2）"""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "data": data,
        "error": error,
    }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=1, default=str)
    sys.stdout.write("\n")


def _fail(code: str, message: str, next_action: str | None = None,
          exit_code: int = EXIT_USAGE, extra: dict | None = None) -> int:
    error = {"code": code, "message": message,
             "next_action": next_action or _NEXT_ACTIONS.get(code,
                                                             "查看 skills/apibridge-cli/SKILL.md")}
    if extra:
        error.update(extra)
    _output(False, None, error)
    return exit_code


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        _fail("missing_env", f"缺少环境变量 {name}",
              next_action=f"export {name}=... 后重试")
        sys.exit(EXIT_USAGE)
    return value.rstrip("/")


def _http(method: str, path: str, body: dict | None = None,
          query: dict | None = None) -> tuple[int, dict | list]:
    """发起 HTTP 调用；返回 (status, json_body)。仅标准库。"""
    server = _env("APIBRIDGE_SERVER")
    token = _env("APIBRIDGE_ADMIN_TOKEN")
    url = f"{server}{path}"
    if query:
        from urllib.parse import urlencode
        url += "?" + urlencode({k: v for k, v in query.items() if v is not None})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Admin-Token", token)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except (urllib.error.URLError, OSError) as e:
        _fail("server_unreachable", f"无法连接 {server}: {e}",
              next_action="确认 APIBRIDGE_SERVER 地址与网络连通性")
        sys.exit(EXIT_USAGE)


def _classify(status: int, body: dict, ok_codes: tuple[int, ...] = (200, 201)) -> int:
    """把 HTTP 响应映射为退出码（task 6.3 退出码分层）"""
    if status in ok_codes:
        return EXIT_OK
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else {}
    if status in (401, 403, 429, 409):
        # 鉴权 / 预算 / 状态不足（含 below_l2_threshold）→ 被拒绝
        return EXIT_REJECTED
    if status == 422:
        return EXIT_VERIFY_FAILED
    return EXIT_USAGE


def _emit_result(status: int, body: dict, ok_msg: str | None = None) -> int:
    """按契约输出并返回退出码；失败自动带 next_action（task 6.4）"""
    if status in (200, 201):
        _output(True, body)
        return EXIT_OK
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else {}
    code = detail.get("code", f"http_{status}")
    message = detail.get("message") or (json.dumps(body.get("detail"), ensure_ascii=False)
                                        if body.get("detail") else f"HTTP {status}")
    return _fail(code, message, extra={"http_status": status},
                 exit_code=_classify(status, body))


# ============================================================
# 子命令实现（全部经 HTTP）
# ============================================================

def cmd_probe(args) -> int:
    """建会话并执行 L1：返回状态码/延迟/响应样本；不创建任何连接（task 6.6）"""
    source = {"protocol": args.protocol, "base_url": args.source_url,
              "endpoint": args.source_endpoint}
    if args.source_token:
        source["auth_type"] = "bearer"
        source["auth_config"] = {"token": args.source_token}
    target = {"protocol": args.protocol, "base_url": args.target_url,
              "endpoint": args.target_endpoint}
    status, body = _http("POST", "/api/v1/probes", body={
        "source": source, "target": target, "via": "cli"})
    if status not in (200, 201):
        return _emit_result(status, body)
    session_id = body["id"]
    status, body = _http("POST", f"/api/v1/probes/{session_id}/probe",
                         query={"via": "cli"})
    if status != 200:
        return _emit_result(status, body)
    l1r = body.get("l1_result") or {}
    _output(True, {
        "session_id": session_id,
        "status": body.get("status"),
        "source": {"status": (l1r.get("source") or {}).get("status"),
                   "latency_ms": (l1r.get("source") or {}).get("latency_ms"),
                   "error": (l1r.get("source") or {}).get("error")},
        "target": {"status": (l1r.get("target") or {}).get("status"),
                   "latency_ms": (l1r.get("target") or {}).get("latency_ms"),
                   "error": (l1r.get("target") or {}).get("error")},
        "next": "apibridge infer --session " + session_id,
    })
    return _classify(status, body) if body.get("status") == "failed" else EXIT_OK



def cmd_infer(args) -> int:
    """源字段清单：path/type/nullable/is_array（task 6.7）"""
    status, body = _http("POST", f"/api/v1/probes/{args.session}/infer",
                         query={"via": "cli"})
    if status != 200:
        return _emit_result(status, body)
    fields = (body.get("source_schema") or {}).get("fields", [])
    _output(True, {"session_id": args.session, "fields": fields,
                   "next": f"apibridge draft --session {args.session} "
                           "[--fields JSON]（缺省生成同名退化草案）"})
    return EXIT_OK


def cmd_probe_mapping(args) -> int:
    """从已存在映射创建复验会话；不创建连接/映射/任务。"""
    status, mapping = _http('GET', f'/api/v1/mappings/{args.mapping_id}')
    if status != 200:
        return _emit_result(status, mapping)
    config = mapping['mapping_config']
    if args.mapping_config_file:
        try:
            with open(args.mapping_config_file, encoding='utf-8') as handle:
                config = json.load(handle)
        except (OSError, ValueError):
            return _fail('invalid_config', '无法读取映射配置文件')
    status, probe = _http('POST', '/api/v1/probes', body={
        'mapping_id':mapping['id'],
        'source': {'connection_id':mapping['source_connection_id'], 'endpoint':mapping.get('source_endpoint'), 'table':mapping.get('source_table')},
        'target': {'connection_id':mapping['target_connection_id'], 'endpoint':args.target_endpoint if args.target_endpoint is not None else mapping.get('target_endpoint'), 'table':mapping.get('target_table')},
        'mapping_config':config, 'via':'cli'})
    if status not in (200,201):
        return _emit_result(status, probe)
    return _emit_result(status, probe)


def cmd_draft(args) -> int:
    """提交字段对应关系，服务端确定性校验（task 6.8）"""
    fields = None
    if args.fields:
        try:
            fields = json.loads(args.fields)
        except json.JSONDecodeError as e:
            return _fail("invalid_fields", f"--fields 不是合法 JSON: {e}",
                         next_action='格式: [{"source":"a","target":"b"}]')
    contract = None
    if getattr(args, "target_contract_json", None):
        try:
            contract = json.loads(args.target_contract_json)
        except json.JSONDecodeError as e:
            return _fail("invalid_contract", f"--target-contract-json 不是合法 JSON: {e}",
                         next_action='格式: [{"name":"x","type":"string","required":true}]')
    mapping_config = None
    if getattr(args, 'mapping_config_file', None):
        try:
            with open(args.mapping_config_file, encoding='utf-8') as handle:
                mapping_config = json.load(handle)
        except (OSError, ValueError):
            return _fail('invalid_config', '无法读取映射配置JSON文件')
    status, body = _http("POST", f"/api/v1/probes/{args.session}/draft",
                         body={"fields": fields, "target_contract": contract,
                               "mapping_config": mapping_config, "via": "cli"})
    if status != 200:
        return _emit_result(status, body)
    ok = body.get("valid")
    _output(ok, {
        "session_id": args.session,
        "valid": ok,
        "fields": body.get("fields"),
        "issues": body.get("issues"),
        "missing_required": body.get("missing_required"),
        "next": f"apibridge verify --session {args.session} --level L2",
    })
    return EXIT_OK if ok else EXIT_VERIFY_FAILED


def cmd_verify(args) -> int:
    """L1/L2/L3 验证（task 6.9）"""
    if args.level.upper() == "L1":
        status, body = _http("POST", f"/api/v1/probes/{args.session}/probe",
                             query={"via": "cli"})
        if status != 200:
            return _emit_result(status, body)
        ok = body.get("status") == "L1_reachable"
        _output(ok, {"session_id": args.session, "status": body.get("status"),
                     "l1_result": body.get("l1_result")})
        return EXIT_OK if ok else EXIT_VERIFY_FAILED

    status, body = _http("POST", f"/api/v1/probes/{args.session}/verify",
                         body={"level": args.level.upper(), "via": "cli"})
    if status != 200:
        return _emit_result(status, body)
    if args.level.upper() == "L2":
        l2 = body.get("l2_result") or {}
        ok = l2.get("valid") is True
        _output(ok, {"session_id": args.session, "status": body.get("status"),
                     "level": l2.get("level"), "valid": l2.get("valid"),
                     "contract_issues": l2.get("contract_issues"),
                     "next": f"apibridge protocol apply --session {args.session}"
                             if ok else "根据 contract_issues 修正 draft 后重跑 L2"})
        return EXIT_OK if ok else EXIT_VERIFY_FAILED

    # L3
    l3 = body.get("l3_result") or {}
    ok = l3.get("valid") is True
    _output(ok, {"session_id": args.session, "status": body.get("status"),
                 "level": l3.get("level"), "valid": l3.get("valid"),
                 "match_rate": l3.get("match_rate"), "summary": l3.get("summary"),
                 "diffs": l3.get("diffs"),
                 "next": "apibridge protocol apply --session " + args.session if ok
                 else "根据 diffs 排查目标侧差异"})
    return EXIT_OK if ok else EXIT_VERIFY_FAILED


def cmd_protocol_apply(args) -> int:
    """提交至待确认清单（task 6.10）：仅 ≥L2；不建桥；未达 L2 退出码 3"""
    status, body = _http("POST", f"/api/v1/probes/{args.session}/submit",
                         body={"via": "cli", "decided_by": "cli"})
    if status != 200:
        return _emit_result(status, body)
    _output(True, {"session_id": args.session,
                   "status": body.get("status"),
                   "next": "等待人工在管理界面「待确认清单」批准（CLI 无批准能力）"})
    return EXIT_OK


def cmd_session_show(args) -> int:
    """只读：会话详情（task 6.11）"""
    status, body = _http("GET", f"/api/v1/probes/{args.session}")
    if status != 200:
        return _emit_result(status, body)
    _output(True, body)
    return EXIT_OK


def cmd_review_list(args) -> int:
    """只读：待确认清单（task 6.11）"""
    status, body = _http("GET", "/api/v1/probes",
                         query={"status": args.status, "protocol": args.protocol})
    if status != 200:
        return _emit_result(status, body)
    items = [{"session_id": i.get("id"), "status": i.get("status"),
              "level": i.get("level"), "protocol": i.get("protocol"),
              "created_at": i.get("created_at")} for i in body]
    _output(True, {"items": items, "count": len(items)})
    return EXIT_OK


def cmd_review_show(args) -> int:
    """只读：清单项详情（同 session show，脱敏输出）"""
    return cmd_session_show(args)


def cmd_review_approve(_args) -> int:
    """task 6.12：CLI 不提供批准——明确指路管理界面且退出码 3"""
    return _fail("approval_not_allowed_via_cli",
                 "批准请在管理界面「待确认清单」中完成",
                 next_action="在浏览器打开管理界面 → 待确认清单 → 查看对照后批准",
                 exit_code=EXIT_REJECTED)


# ============================================================
# 参数解析
# ============================================================

def cmd_fields(args) -> int:
    """只读：连接字段定义，合并字段字典（readable/writable/saved）

    字段 path 即运行时行键（HAP = alias || controlId），可直接用作
    draft --fields 的 source/target；源侧 readable=false 的字段同步时会被
    剔除、目标侧 writable=false 不推送——草稿应跳过这些字段。
    """
    query = {"table": args.table, "worksheet_id": args.worksheet_id}
    status, body = _http("GET", f"/api/v1/connections/{args.connection_id}/fields",
                         query=query)
    if status != 200:
        return _emit_result(status, body)
    kind = body.get("kind")
    raw_fields = body.get("fields")
    if kind == "db" and raw_fields is None and body.get("tables"):
        # DB 连接未指定表：先给表清单
        _output(True, {"kind": "db", "hint": None, "fields": None, "count": 0,
                       "tables": body.get("tables"),
                       "next": "apibridge fields --connection-id "
                               f"{args.connection_id} --table <表名>"})
        return EXIT_OK
    fields = raw_fields or []
    _output(True, {"kind": kind, "hint": body.get("hint"), "fields": fields,
                   "count": len(fields),
                   "tables": body.get("tables"),
                   "next": "字段 path 即运行时行键，可直接用作 draft --fields 的 "
                           "source/target；启用字段字典时 readable=false 源侧剔除、writable=false "
                           "目标侧不推送，草稿应跳过"})
    return EXIT_OK


def cmd_forms_list(args) -> int:
    """只读：表单注册表（含桥引用统计）；登记/从源端拉取属管理界面能力"""
    status, body = _http("GET", "/api/v1/forms",
                         query={"connection_id": args.connection_id})
    if status != 200:
        return _emit_result(status, body)
    items = body if isinstance(body, list) else []
    if args.search:
        s = args.search.lower()
        items = [f for f in items if s in str(f.get("display_name") or "").lower()
                 or s in str(f.get("form_key") or "").lower()
                 or s in str(f.get("remark") or "").lower()]
    compact = [{
        "form_key": f.get("form_key"),
        "display_name": f.get("display_name"),
        "original_name": f.get("original_name"),
        "connection_id": str(f.get("connection_id") or ""),
        "connection_name": f.get("connection_name"),
        "source": f.get("source"),
        "bridge_count": f.get("bridge_count", 0),
        "bridges": [{
            "mapping_name": b.get("mapping_name"),
            "side": b.get("side"),
            "peer_connection_name": b.get("peer_connection_name"),
            "peer_form": b.get("peer_form"),
        } for b in (f.get("bridges") or [])],
    } for f in items]
    _output(True, {"items": compact, "count": len(compact),
                   "next": "字段级细节用 apibridge fields --connection-id <ID> "
                           "[--table 表名 | --worksheet-id 工作表ID]；"
                           "表单登记/从源端拉取属管理界面能力，CLI 不提供"})
    return EXIT_OK


# ============================================================
# 参数解析
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apibridge",
        description="APIBridge 联调 CLI（agent 唯一入口；批准仅在管理界面完成）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probe", help="建会话并执行 L1 探测")
    p.add_argument("--source-url", required=True)
    p.add_argument("--source-endpoint", required=True)
    p.add_argument("--source-token", default="")
    p.add_argument("--target-url", required=True)
    p.add_argument("--target-endpoint", required=True)
    p.add_argument("--protocol", default="rest")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("infer", help="推断源字段清单")
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_infer)

    p = sub.add_parser('probe-mapping', help='为已有映射创建复验会话，不建桥')
    p.add_argument('--mapping-id', required=True)
    p.add_argument('--mapping-config-file', default=None)
    p.add_argument('--target-endpoint', default=None)
    p.set_defaults(func=cmd_probe_mapping)

    p = sub.add_parser("draft", help="提交字段对应关系（缺省生成同名退化草案）")
    p.add_argument("--session", required=True)
    p.add_argument("--fields", default=None,
                   help='JSON: [{"source":"a.b","target":"x","transform":"string"}]')
    p.add_argument('--mapping-config-file', default=None, help='完整映射配置JSON，包含协议参数和校验规则')
    p.add_argument("--target-contract-json", default=None,
                   help='JSON: [{"name":"x","type":"string","required":false}]'
                        '（API 目标的字段契约声明）')
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("verify", help="三级验证")
    p.add_argument("--session", required=True)
    p.add_argument("--level", default="L2", choices=["L1", "L2", "L3"])
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("protocol-apply", help="提交至待确认清单（即 protocol apply；不建桥）")
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_protocol_apply)

    p = sub.add_parser("session-show", help="只读：会话详情")
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_session_show)

    p = sub.add_parser("review", help="待确认清单（只读；批准仅在管理界面）")
    rsub = p.add_subparsers(dest="sub", required=True)
    lp = rsub.add_parser("list")
    lp.add_argument("--status", default=None)
    lp.add_argument("--protocol", default=None)
    lp.set_defaults(func=cmd_review_list)
    sp2 = rsub.add_parser("show")
    sp2.add_argument("--session", required=True)
    sp2.set_defaults(func=cmd_review_show)
    ap = rsub.add_parser("approve")
    ap.set_defaults(func=cmd_review_approve)
    p = sub.add_parser("fields",
                       help="只读：连接字段定义（合并字段字典读写标记；path 即运行时行键）")
    p.add_argument("--connection-id", required=True)
    p.add_argument("--table", default=None, help="DB 连接的表名（scope）")
    p.add_argument("--worksheet-id", default=None, help="HAP 连接的工作表 ID（scope）")
    p.set_defaults(func=cmd_fields)

    p = sub.add_parser("forms", help="表单注册表（只读；登记/从源端拉取在管理界面）")
    fsub = p.add_subparsers(dest="sub", required=True)
    fl = fsub.add_parser("list")
    fl.add_argument("--connection-id", default=None, help="按连接筛选（空=全部）")
    fl.add_argument("--search", default=None,
                    help="客户端过滤：匹配显示名/form_key/备注")
    fl.set_defaults(func=cmd_forms_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])

    # task 6.5：拒绝 --token 命令行参数（token 只能来自环境变量）
    if "--token" in raw:
        return _fail("token_arg_forbidden",
                     "不接受 --token 参数：请使用环境变量 APIBRIDGE_ADMIN_TOKEN",
                     next_action="export APIBRIDGE_ADMIN_TOKEN=... 后重试")

    # 二级子命令规范化：protocol apply / session show
    if raw and raw[0] == "protocol":
        if len(raw) >= 2 and raw[1] == "apply":
            raw = ["protocol-apply"] + raw[2:]
        else:
            return _fail("usage", "用法: apibridge protocol apply --session ID")
    if raw and raw[0] == "session":
        if len(raw) >= 2 and raw[1] == "show":
            raw = ["session-show"] + raw[2:]
        else:
            return _fail("usage", "用法: apibridge session show --session ID")

    # task 6.1：环境变量前置检查，缺失退出码 4 并指明缺哪个
    for name in ("APIBRIDGE_SERVER", "APIBRIDGE_ADMIN_TOKEN"):
        if not os.environ.get(name):
            return _fail("missing_env", f"缺少环境变量 {name}",
                         next_action=f"export {name}=... 后重试")

    args = build_parser().parse_args(raw)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
