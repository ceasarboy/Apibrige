"""探针内核 — 三级验证（L1 可达 / L2 转换已验证 / L3 端到端已验证）

铁律（design.md 决策2）：
    - 本模块不自行发 HTTP / 不直连数据库，全部经
      ProtocolAdapterFactory / get_database_connector 与运行时同一条路；
    - L2 绝不写入目标；L3 需人工授权且只写 1 条；
    - 外部调用受预算约束（复用 probe_guard 语义）。
"""
from __future__ import annotations

import time

from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from app.config import settings
from app.adapters.factory import ProtocolAdapterFactory
from app.connectors.batch import batch_fetch
from app.connectors.database import get_database_connector
from app.metrics_collector import record_api_call
from app.models.sync import ProbeSession
from app.security.desensitize import desensitize_record
from app.security.probe_guard import ProbeBudgetExhausted
from app.services.adapter_config import build_adapter_config
from app.services.transformer import transform_data, transform_payload
from app.adapters.errors import AdapterError
from app.services.validator import validate_batch
from app.testing.comparator import DataComparator

DEFAULT_SAMPLE_SIZE = 20


class RequiresHumanAuthorization(Exception):
    """L3 未获人工授权"""

    def __init__(self):
        self.reason = "requires_human_authorization"
        super().__init__("requires_human_authorization: L3 需要先在待确认清单中人工授权")


@dataclass
class Side:
    """探针一侧（源/目标）的输入描述（task 3.13 双输入形态）"""

    conn: Any  # ApiConnection 或同形内联对象
    endpoint: str = ""
    table: str = ""
    connection_id: UUID | None = None  # 内联形态为 None
    snapshot: dict = field(default_factory=dict)  # 脱敏后的配置摘要
    mapping_config: dict = field(default_factory=dict)
    mapping_headers: dict = field(default_factory=dict)

    @property
    def is_db(self) -> bool:
        return bool(getattr(self.conn, "db_type", None))

    @property
    def role(self) -> str:
        return "source" if self.endpoint or not self.table else "target"


def consume_budget(session: ProbeSession, side_role: str) -> None:
    """按（会话, 目标）计数消耗预算，耗尽抛 probe_budget_exhausted（task 2.9/3.x）"""
    calls = dict(session.probe_calls or {})
    used = calls.get(side_role, 0)
    if used >= settings.probe_budget:
        raise ProbeBudgetExhausted(side_role)
    calls[side_role] = used + 1
    session.probe_calls = calls


def _api_adapter(side: Side, db_side: str = "source"):
    """API 侧适配器：与运行时同一构造函数（同源原则）

    探针会话没有 FieldMapping ORM，映射级 Header 覆盖不适用；
    凭据解密在 build_adapter_config 内完成。
    probe_guard 接线：开 APIBRIDGE_PROBE_SSRF_ENFORCE 时探针目标先过
    validate_target（运行时同步路径不受影响，由部署网络边界管控）。
    """
    from app.security.probe_guard import validate_target
    base = getattr(side.conn, "base_url", None)
    if base:
        validate_target(str(base))
    mapping = SimpleNamespace(mapping_config=side.mapping_config, source_headers=side.mapping_headers if db_side == 'source' else None,
                              target_headers=side.mapping_headers if db_side == 'target' else None)
    config = build_adapter_config(side.conn, mapping, db_side)
    adapter = ProtocolAdapterFactory.create(side.conn.protocol, config)
    return adapter


def _db_connector(side: Side):
    """数据库侧连接器：与运行时同一工厂

    SSRF 防线覆盖 DB 侧（审查 P3）：开启 probe_ssrf_enforce 时对 db_config.host
    套用与 API 侧相同的目标校验（IP 字面量/DNS 解析 + 拒绝段 + 白名单豁免）。
    """
    from app.security.probe_guard import validate_host
    db_config = side.conn.db_config or {}
    host = db_config.get("host")
    if host:
        validate_host(host)   # 裸主机校验（tcp:// 硬造 URL 会被 scheme 门无条件拒）
    return get_database_connector(side.conn.db_type, db_config)


def _test_side(side: Side, role: str = 'source') -> dict:
    """单侧连通性测试（不搬运业务数据）"""
    if side.is_db:
        return _db_connector(side).test_connection()
    return _api_adapter(side, role).test_connection()


# ============================================================
# L1 端点可达（task 3.3）
# ============================================================

async def run_l1(source: Side, target: Side) -> dict:
    """L1：源/目标两侧可达且认证有效；不写任何业务数据"""
    t0 = time.time()
    source_result = await _test_side(source)
    record_api_call(source.conn.base_url or "source",
                    source_result.get("status") or "unknown", time.time() - t0)
    t0 = time.time()
    target_result = await _test_side(target, 'target')
    record_api_call(target.conn.base_url or "target",
                    target_result.get("status") or "unknown", time.time() - t0)
    return {
        "level": "L1",
        "source": source_result,
        "target": target_result,
    }


def l1_ok(l1_result: dict) -> bool:
    sides = [l1_result.get("source", {}), l1_result.get("target", {})]
    return all(s.get("status") == "ok" for s in sides)


# ============================================================
# 样本拉取（task 3.8：batch_fetch 分页取前 N，默认 20）
# ============================================================

async def fetch_sample(side: Side, limit: int = DEFAULT_SAMPLE_SIZE) -> list[dict]:
    """拉取源数据小样本；DB 经 batch_fetch 分页只取首页，不一次性全量入内存"""
    if side.is_db:
        connector = _db_connector(side)

        async def fetch_page(offset: int, batch: int) -> list[dict]:
            return await connector.fetch_data(
                side.table, offset=offset, limit=batch
            )

        pages = batch_fetch(fetch_page, batch_size=limit)
        first = await pages.__anext__()
        await pages.aclose()
        return first[:limit]

    adapter = _api_adapter(side)
    if side.conn.protocol in ('hap', 'hapv3'):
        adapter.config['sample_limit'] = limit
    data = await adapter.fetch_data(side.endpoint)
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)][:limit]
    if isinstance(data, dict):
        # 常见包装键优先，取第一个列表值
        for key in ("data", "items", "results", "list", "records"):
            value = data.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)][:limit]
        return [data]
    return []


# ============================================================
# 目标契约（task 3.7 / 3.14）
# ============================================================

def _required_from_schema(schema: list[dict]) -> set[str]:
    """DB 表结构中的必填列（非可空），主键视为必填"""
    required = set()
    for col in schema or []:
        name = col.get("name")
        if not name:
            continue
        if col.get("primary_key") or col.get("nullable") is False:
            required.add(name)
    return required


async def get_target_contract(
    target: Side, mapping_config: dict | None
) -> tuple[list[dict] | None, set[str]]:
    """取目标契约。返回 (契约字段清单, 必填集合)；API 无契约时 (None, set())

    契约声明约定：mapping_config["target_contract"] = [{"name","type","required"?}]
    """
    if target.is_db:
        connector = _db_connector(target)
        schema = await connector.get_table_schema(target.table)
        return schema, _required_from_schema(schema)

    declared = (mapping_config or {}).get("target_contract")
    if isinstance(declared, list) and declared:
        required = {d.get("name") for d in declared if d.get("required")}
        return declared, required
    return None, set()


# ============================================================
# L2 转换已验证（task 3.7：真实转换 + 真实校验 + 契约比对，零写入）
# ============================================================

async def run_l2(
    source: Side, target: Side, mapping_config: dict,
    sample: list[dict] | None = None,
    limit: int = DEFAULT_SAMPLE_SIZE,
) -> dict:
    """L2：真实拉样本 → transform_data → validate_batch → 目标契约比对

    不向目标发起任何创建/更新请求。
    """
    from app.probe.mapping_draft import types_compatible

    source = replace(source, mapping_config=mapping_config)
    target = replace(target, mapping_config=mapping_config)
    if sample is None:
        try:
            sample = await fetch_sample(source, limit)
        except AdapterError as exc:
            return {'level':'L2', 'valid':False, 'sample_size':0, 'zero_write':True, 'contract_issues':[exc.as_dict()]}
    sample = sample[:limit]

    try:
        transformed = transform_payload(sample, mapping_config)
    except AdapterError as exc:
        return {'level':'L2', 'valid':False, 'sample_size':len(sample), 'zero_write':True, 'contract_issues':[exc.as_dict()]}

    validations = (mapping_config or {}).get("validations")
    validation: dict = {"total": len(transformed), "failed": 0, "valid": True, "errors": []}
    if validations:
        validation = validate_batch(transformed, validations)

    contract, required = await get_target_contract(target, mapping_config)

    result: dict = {
        "level": "L2",
        "sample_size": len(transformed),
        "transformed_sample": transformed[:3],
        "validation": {
            "total": validation.get("total", 0),
            "failed": validation.get("failed", 0),
        },
        "zero_write": True,
    }

    if contract is None:
        # task 3.14：API 目标无契约 → 降级，不得视为达到 L2
        result.update({
            "level": "L2_partial_no_contract",
            "valid": False,
            "self_consistent": all(isinstance(r, dict) and r for r in transformed),
            "next_action": "补齐目标契约：在 mapping_config['target_contract'] 声明目标字段清单，"
                           "或改用可查询表结构的目标（数据库连接）",
        })
        return result

    # 字段存在性 + 类型可行性比对（确定性，非模型判断；Oracle 列名大小写不敏感）
    t_fields = {
        str((c.get("name") or c.get("path"))).lower(): c
        for c in contract
        if c.get("name") or c.get("path")
    }
    contract_issues: list[dict] = []
    if not target.is_db and target.conn.protocol in ('rest', 'http'):
        try:
            adapter = _api_adapter(target, 'target')
            prepared = adapter.prepare_push_request(target.endpoint, transformed)
            from app.security.headers import mask_headers
            prepared['headers'] = mask_headers(prepared['headers'])
            result['prepared_request'] = desensitize_record(prepared)
        except ValueError as exc:
            contract_issues.append(exc.as_dict() if isinstance(exc, AdapterError) else {'code':'invalid_config', 'message':'请求配置不合法'})
    for record in transformed:
        record_keys = {k.lower() for k in record}
        for req in required:
            value = next((v for k, v in record.items() if k.lower() == str(req).lower()), None)
            if value is None or (isinstance(value, str) and not value.strip()):
                contract_issues.append({'code':'missing_required', 'path':req, 'message':f'目标必填字段为空: {req}'})
        for name in record:
            if name.lower() not in t_fields:
                contract_issues.append({
                    "code": "unknown_target_field", "path": name,
                    "message": f"映射输出字段不在目标契约中: {name}",
                })
        for lname, col in t_fields.items():
            if lname in record_keys and record[
                next(k for k in record if k.lower() == lname)
            ] is not None:
                value = record[next(k for k in record if k.lower() == lname)]
                if not types_compatible(type(value).__name__, col.get("type")):
                    contract_issues.append({
                        "code": "type_infeasible", "path": lname,
                        "message": f"输出类型 {type(value).__name__} 与目标 "
                                   f"{col.get('type')} 不兼容: {lname}",
                    })
    output_keys = {k.lower() for r in transformed for k in r}
    missing_required = sorted(
        req for req in required if str(req).lower() not in output_keys
    )
    for req in missing_required:
        contract_issues.append({"code": "missing_required", "path": req,
                                "message": f"目标必填字段未覆盖: {req}"})

    result["level"] = "L2"
    result["contract_issues"] = contract_issues[:50]
    result["valid"] = (
        not contract_issues
        and validation.get("failed", 0) == 0
        and bool(transformed)
    )
    if result['valid'] and target.conn.protocol == 'hap' and mapping_config.get('hap_write_mode') == 'upsert':
        from app.services.hap_bulk import preview
        candidate = SimpleNamespace(mapping_config=mapping_config, target_endpoint=target.endpoint)
        try:
            result['hap_operations'] = await preview(_api_adapter(target, 'target'), candidate, transformed)
        except AdapterError as exc:
            result['valid'] = False
            result['contract_issues'].append(exc.as_dict())
    return result


# ============================================================
# L3 端到端已验证（task 3.9/3.10：人工授权 → 写 1 条 → 回读 → 比对）
# ============================================================

def _get_case_insensitive(record: dict, name: str) -> tuple[bool, Any]:
    """按大小写不敏感取字段（Oracle 列名大写、映射键任意的互认）"""
    for k, v in record.items():
        if k.lower() == name.lower():
            return True, v
    return False, None


async def _readback(target: Side, record: dict, mapping_config: dict,
                    push_body: Any) -> tuple[list[dict] | None, str | None]:
    """回读目标数据。返回 (读到的记录列表|None, 缺口说明)"""
    if target.is_db:
        connector = _db_connector(target)
        key_field = (mapping_config or {}).get("key_field")
        if not key_field:
            schema = await connector.get_table_schema(target.table)
            key_field = next((c["name"] for c in schema if c.get("primary_key")), None)
        found, key_value = _get_case_insensitive(record, key_field or "")
        if not key_field or not found:
            return None, "缺少 key_field（mapping_config['key_field']），无法按 ID 回读"
        rows = await connector.fetch_data(target.table, where={key_field: key_value})
        return (rows if isinstance(rows, list) else [rows] if rows else []), None

    # API 目标：推送响应体即表示则用之；否则尝试 GET {endpoint}/{id}
    if isinstance(push_body, dict) and push_body and set(record).issubset(push_body):
        return [push_body], None
    key_field = (mapping_config or {}).get("key_field")
    key_value = record.get(key_field) if key_field else None
    if key_field and key_value is not None:
        adapter = _api_adapter(target, db_side="target")
        try:
            data = await adapter.fetch_data(f"{target.endpoint.rstrip('/')}/{key_value}")
            if isinstance(data, dict):
                return [data], None
        except Exception:
            pass
    return None, "目标未返回可回读表示，且无按 ID 查询接口"


async def run_l3(
    source: Side, target: Side, mapping_config: dict,
    session: ProbeSession | None = None,
    plan_factory=None,
    budget_callback=None,
) -> dict:
    """L3：人工授权后写 1 条 → 回读 → DataComparator 比对

    未授权抛 RequiresHumanAuthorization，且不触碰目标（task 3.10）。
    """
    if session is not None and not session.e2e_authorized:
        raise RequiresHumanAuthorization()

    source = replace(source, mapping_config=mapping_config)
    target = replace(target, mapping_config=mapping_config)
    sample = await fetch_sample(source, limit=1)
    if not sample:
        return {"level": "L3", "valid": False,
                "error": "源无可用样本，无法执行端到端验证"}

    record = transform_data(sample[0], mapping_config)
    from app.services.hap_bulk import is_upsert
    candidate = SimpleNamespace(mapping_config=mapping_config, target_endpoint=target.endpoint, sync_mode='full')
    if target.conn.protocol == 'hap' and is_upsert(candidate):
        if session is None or not session.e2e_authorized:
            raise RequiresHumanAuthorization()
        if plan_factory is None:
            raise AdapterError('hap_plan_required', 'HAP 新增更新 L3 需要可持久化的授权探针入口', 'validate')
        from app.services.hap_bulk import execute
        adapter = _api_adapter(target, 'target')
        checked = await run_l2(source, target, mapping_config, sample=sample, limit=1)
        if not checked.get('valid'):
            return {'level':'L3_partial','valid':False,'written':False,'gap':'写入前校验失败','validation':checked}
        try:
            result = await execute(plan_factory, None, adapter, candidate, [record],
                                   before_call=budget_callback, probe_id=session.id)
            body = result['body']
            written = bool(body['added'] or body['updated'])
            return {'level':'e2e_verified' if written else 'L3_partial', 'valid':written, 'written':written,
                    'summary':body, 'gap':None if written else '样本字段无变化，未验证真实写入'}
        except AdapterError as exc:
            return {'level':'L3_partial','valid':False,'written':None,'outcome_unknown':True,
                    'gap':str(exc), 'error':exc.as_dict()}
    if mapping_config.get('version') == 2:
        checked = await run_l2(source, target, mapping_config, sample=sample, limit=1)
        if not checked.get('valid'):
            return {'level':'L3_partial', 'valid':False, 'written':False, 'gap':'写入前契约验证未通过', 'validation':checked}
    if session is not None:
        consume_budget(session, "target")

    # 写 1 条
    if target.is_db:
        connector = _db_connector(target)
        key_field = (mapping_config or {}).get("key_field")
        pk = key_field or next(
            (c["name"] for c in await connector.get_table_schema(target.table)
             if c.get("primary_key")), None)
        await connector.push_data(
            target.table, [record], key_field=pk, mode="upsert")
        push_body = None
    else:
        adapter = _api_adapter(target, db_side="target")
        from app.services.hap_target import enabled, push
        candidate = SimpleNamespace(mapping_config=mapping_config, target_endpoint=target.endpoint, sync_mode='full')
        if enabled(candidate):
            from app.services.hap_target import validate_policy
            validate_policy(candidate, target.conn)
            # 与运行时共用去重写入；L3 始终不执行删除对账。
            config = build_adapter_config(target.conn, candidate, 'target')
            response = await push(adapter, config, candidate, [record])
            if not response.get('body', {}).get('row_ids'):
                return {'level':'L3_partial', 'valid':False, 'written':False,
                        'gap':'目标已有业务键，去重跳过；请使用新的单条测试记录验证写入'}
        else:
            response = await adapter.push_data(target.endpoint, [record] if mapping_config.get('version') == 2 else record)
        push_body = response.get("body") if isinstance(response, dict) else response

    # 回读 + 比对
    readback_rows, gap = await _readback(target, record, mapping_config, push_body)
    if readback_rows is None:
        return {"level": "L3_partial", "valid": False, "written": True, "gap": gap}
    if not readback_rows:
        return {"level": "L3_partial", "valid": False, "written": True,
                "gap": "写入后未按 ID 读回记录"}

    comparator = DataComparator()
    # 只比对映射写入的字段（键统一小写：Oracle 回读小写、映射目标可大写）；
    # 目标侧的其他列（默认值、时间戳等）不算差异
    expected_row = {k.lower(): v for k, v in record.items()}
    readback_row = {k.lower(): v for k, v in readback_rows[0].items()
                    if k.lower() in expected_row}
    compare = comparator.compare(expected_row, readback_row)
    return {
        "level": "L3",
        "valid": compare.matched,
        "written": True,
        "match_rate": compare.match_rate,
        "summary": compare.summary(),
        "diffs": [vars(d) for d in compare.diffs[:20]],
    }
