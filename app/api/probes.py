"""探针 HTTP 面（task 3.15）— CLI 与前端唯一依赖的后端契约

六个端点即可把一个会话从 draft 推到 pending_review：
    POST /probes                      建会话（内联配置 / connection_id 双形态）
    POST /probes/{id}/probe           L1 可达性
    POST /probes/{id}/infer           源/目标 Schema 推断
    POST /probes/{id}/draft           提交字段对应关系（服务端确定性校验）
    POST /probes/{id}/verify          L2（零写入）/ L3（需人工授权）
    POST /probes/{id}/submit          提交至待确认清单（门槛校验）
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from app.schemas import MappingValidation
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.probe.timestamps import as_east_eight
from app.config import settings
from app.models.sync import ApiConnection, FieldMapping, ProbeSession, SyncTask
from app.probe import prober
from app.probe.mapping_draft import generate_degenerate_draft, validate_draft
from app.probe.schema_infer import infer_schema
from app.probe.session import (
    InvalidTransition,
    ensure_expires_at,
    expire_stale_sessions,
    transition,
)
from app.security.desensitize import desensitize_connection, desensitize_record
from app.security.probe_guard import ProbeBudgetExhausted, TargetNotAllowed

router = APIRouter()

# task 3.13：内联配置的明文凭证不落库，仅驻进程内存供草稿期复用
# （infer/verify 需要真实凭据；approve 时由批准人提供最终配置）
# 结构: {session_id: {"source": conn_obj, "target": conn_obj, "at": ts}}
_INLINE_CONN_CACHE: dict = {}
_INLINE_CACHE_MAX = 200


# ============================================================
# 输入模型
# ============================================================

class SideInput(BaseModel):
    """探针侧输入：connection_id（复验既有桥接）或内联配置（裸 URL 起步）"""
    connection_id: UUID | None = None
    # 内联配置字段（明文凭证仅驻内存，落库前脱敏为摘要）
    protocol: str | None = None
    base_url: str | None = None
    auth_type: str | None = None
    auth_config: dict | None = None
    headers: dict | None = None
    timeout_ms: int = 30000
    db_type: str | None = None
    db_config: dict | None = None

    table: str | None = None
    endpoint: str | None = None

    @field_validator("auth_type", mode="before")
    @classmethod
    def _auth_type_whitelist(cls, value):
        # 与 ConnectionValidation.normalize_auth 同白名单：未知/错拼类型会让
        # 脱敏与加密双双失效（审查 P1 的根源之一），入口直接拒绝
        if value in (None, "", "none"):
            return None
        from app.security.crypto import KNOWN_AUTH_TYPES
        if str(value) not in KNOWN_AUTH_TYPES:
            raise ValueError(f"不支持的认证类型: {value}；"
                             f"可用: {sorted(KNOWN_AUTH_TYPES)}")
        return str(value)

    @field_validator("endpoint", mode="before")
    @classmethod
    def _endpoint_must_be_relative(cls, value):
        # SSRF 防线（审查 P1）：探针端点必须是相对路径——绝对 URL 会经 httpx
        # 覆盖 base_url，把已存连接的凭据送往任意主机并绕过 validate_target
        if not value:
            return value
        from urllib.parse import urlsplit
        parsed = urlsplit(str(value))
        if parsed.scheme or parsed.netloc or str(value).startswith("//"):
            raise ValueError("端点必须是相对路径（不要带协议/主机），"
                             "目标主机由连接的 base_url 决定")
        return str(value)


class ProbeCreate(MappingValidation):
    mapping_id: UUID | None = None
    source: SideInput
    target: SideInput
    source_endpoint: str | None = None
    target_endpoint: str | None = None
    mapping_config: dict | None = None
    via: str = Field("api", description="触发来源: cli|ui|api")


class DraftInput(MappingValidation):
    mapping_config: dict | None = None
    fields: list[dict] | None = Field(
        None, description="提议的字段对应关系；为空则生成同名退化草案")
    target_contract: list[dict] | None = Field(
        None, description="API 目标的字段契约声明 [{name,type,required?}]，"
                          "随草案落 mapping_config['target_contract']")
    via: str = "api"


class VerifyInput(BaseModel):
    level: str = Field("L2", description="L2 | L3")
    via: str = "api"


class SubmitInput(BaseModel):
    via: str = "api"
    decided_by: str | None = None


def _inline_conn(data: SideInput):
    """内联配置 → conn 形对象（不落库，明文凭证仅驻内存）"""
    return SimpleNamespace(
        protocol=data.protocol or ("rest" if not data.db_type else data.db_type),
        base_url=data.base_url or "",
        auth_type=data.auth_type,
        auth_config=data.auth_config,
        headers=data.headers or {},
        timeout_ms=data.timeout_ms,
        retry_count=3,
        db_type=data.db_type,
        db_config=data.db_config,
    )


def _conn_summary(data: SideInput, conn) -> dict:
    """脱敏后的配置摘要（内联形态不落明文凭证）"""
    summary = {
        "protocol": conn.protocol,
        "base_url": conn.base_url,
        "auth_type": conn.auth_type,
        "auth_config": conn.auth_config,
        "db_type": conn.db_type,
        "db_config": conn.db_config,
        "endpoint": data.endpoint,
        "table": data.table,
    }
    return desensitize_connection(summary)


# 明文凭据内存驻留上限：与会话 TTL 对齐（天级），到期惰性回收（审查 P3）
_INLINE_TTL_SECONDS = 24 * 3600


def _drop_inline(probe_id) -> None:
    """会话进入终态（approved/rejected/expired）后立即清除内存中的明文凭证"""
    _INLINE_CONN_CACHE.pop(probe_id, None)


def _cache_inline(probe_id, role: str, conn) -> None:
    import time as _time

    now = _time.time()
    # TTL 惰性回收：过期内联凭据（含 db_config.password/API token）不留驻内存
    stale = [k for k, v in _INLINE_CONN_CACHE.items()
             if now - v.get("at", 0) > _INLINE_TTL_SECONDS]
    for k in stale:
        _INLINE_CONN_CACHE.pop(k, None)
    cache = _INLINE_CONN_CACHE.setdefault(probe_id, {})
    cache[role] = conn
    cache["at"] = now
    # 简单容量上限：最旧会话淘汰
    if len(_INLINE_CONN_CACHE) > _INLINE_CACHE_MAX:
        oldest = min(_INLINE_CONN_CACHE, key=lambda k: _INLINE_CONN_CACHE[k].get("at", 0))
        _INLINE_CONN_CACHE.pop(oldest, None)


async def _side_for(session: AsyncSession, probe: ProbeSession, role: str) -> prober.Side:
    """构造会话某一侧的 Side：内存缓存 → 已存连接 → 脱敏快照兜底"""
    cached = (_INLINE_CONN_CACHE.get(probe.id) or {}).get(role)
    conn_id = probe.source_connection_id if role == "source" else probe.target_connection_id
    snapshot = probe.source_snapshot if role == "source" else probe.target_snapshot
    endpoint = probe.source_endpoint if role == "source" else probe.target_endpoint
    table = (snapshot or {}).get("table") or ""
    headers = (probe.mapping_draft or {}).get(f'{role}_headers') or {}
    config = (probe.mapping_draft or {}).get('mapping_config') or {}
    if cached is not None:
        return prober.Side(conn=cached, endpoint=endpoint or "", table=table, mapping_headers=headers, mapping_config=config)
    if conn_id:
        conn = await _conn_by_id(session, conn_id)
        return prober.Side(conn=conn, endpoint=endpoint or "", table=table,
                           connection_id=conn_id, mapping_headers=headers, mapping_config=config)
    return prober.Side(conn=_snapshot_conn(snapshot), endpoint=endpoint or "", table=table, mapping_headers=headers, mapping_config=config)


async def _resolve_side(session: AsyncSession, data: SideInput,
                        default_endpoint: str | None) -> prober.Side:
    """双输入形态（task 3.13）：connection_id 或内联配置，同一构造逻辑"""
    if data.connection_id:
        conn = await session.get(ApiConnection, data.connection_id)
        if conn is None:
            raise HTTPException(404, f"连接不存在: {data.connection_id}")
        snapshot = desensitize_connection({
            "protocol": conn.protocol, "base_url": conn.base_url,
            "auth_type": conn.auth_type, "auth_config": conn.auth_config,
            "db_type": conn.db_type, "db_config": conn.db_config,
            "endpoint": data.endpoint, "table": data.table,
        })
        return prober.Side(conn=conn, endpoint=data.endpoint or default_endpoint or "",
                           table=data.table or "", connection_id=conn.id,
                           snapshot=snapshot)
    conn = _inline_conn(data)
    return prober.Side(conn=conn, endpoint=data.endpoint or default_endpoint or "",
                       table=data.table or "", snapshot=_conn_summary(data, conn))


async def _get_session_or_404(session: AsyncSession, probe_id: UUID) -> ProbeSession:
    probe = await session.get(ProbeSession, probe_id)
    if probe is None:
        raise HTTPException(404, "探针会话不存在")
    return probe


def _session_dict(probe: ProbeSession) -> dict:
    return {
        "id": str(probe.id),
        "status": probe.status,
        "source_snapshot": probe.source_snapshot,
        "target_snapshot": probe.target_snapshot,
        "source_connection_id": str(probe.source_connection_id) if probe.source_connection_id else None,
        "target_connection_id": str(probe.target_connection_id) if probe.target_connection_id else None,
        "source_endpoint": probe.source_endpoint,
        "target_endpoint": probe.target_endpoint,
        "source_schema": probe.source_schema,
        "target_schema": probe.target_schema,
        "mapping_draft": desensitize_record(probe.mapping_draft),
        "draft_issues": probe.draft_issues,
        "l1_result": probe.l1_result,
        "l2_result": probe.l2_result,
        "l3_result": probe.l3_result,
        "source_sample": desensitize_record(probe.source_sample),
        "comparison": {
            "contract_issues": (probe.l2_result or {}).get("contract_issues", []),
            "l3_diffs": (probe.l3_result or {}).get("diffs", []),
            "l3_summary": (probe.l3_result or {}).get("summary"),
        },
        "bridge_ids": probe.bridge_ids,
        "level": _probe_level(probe),
        "e2e_authorized": probe.e2e_authorized,
        "probe_calls": probe.probe_calls,
        "created_via": probe.created_via,
        "expires_at": as_east_eight(probe.expires_at).isoformat() if probe.expires_at else None,
    }


# ============================================================
# 端点
# ============================================================

@router.post("")
async def create_probe(payload: ProbeCreate, session: AsyncSession = Depends(get_session)):
    """建会话（task 3.13 双输入形态；内联凭证不落库）"""
    await expire_stale_sessions(session)
    source = await _resolve_side(session, payload.source, payload.source_endpoint)
    target = await _resolve_side(session, payload.target, payload.target_endpoint)

    draft_config = {'mapping_config':payload.mapping_config} if payload.mapping_config else {}
    if payload.mapping_id:
        existing = await session.get(FieldMapping, payload.mapping_id)
        if existing is None:
            raise HTTPException(404, '映射不存在')
        if source.connection_id != existing.source_connection_id or target.connection_id != existing.target_connection_id:
            raise HTTPException(422, '映射与复验连接不一致')
        draft_config.setdefault('mapping_config', existing.mapping_config)
        draft_config.update(source_headers=existing.source_headers, target_headers=existing.target_headers)

    probe = ProbeSession(
        created_via=payload.via,
        source_snapshot=source.snapshot,
        target_snapshot=target.snapshot,
        source_connection_id=source.connection_id,
        target_connection_id=target.connection_id,
        source_endpoint=source.endpoint or None,
        target_endpoint=target.endpoint or None,
        mapping_draft=draft_config or None,
    )
    ensure_expires_at(probe)
    session.add(probe)
    await session.commit()
    await session.refresh(probe)
    # 内联配置：明文凭据仅驻内存缓存（task 3.13）
    if source.connection_id is None:
        _cache_inline(probe.id, "source", source.conn)
    if target.connection_id is None:
        _cache_inline(probe.id, "target", target.conn)
    return _session_dict(probe)


@router.get("")
async def list_probes(
    status: str | None = None,
    protocol: str | None = None,
    level: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    """待确认清单（task 4.1）：默认仅 pending_review，支持协议/级别/时间过滤"""
    await expire_stale_sessions(session)
    effective_status = status or "pending_review"
    stmt = select(ProbeSession).order_by(ProbeSession.created_at.desc())
    if effective_status != "all":
        stmt = stmt.where(ProbeSession.status == effective_status)
    result = await session.execute(stmt)
    probes = result.scalars().all()

    items = []
    for p in probes:
        item = _session_dict(p)
        item["level"] = _probe_level(p)
        item["protocol"] = (p.source_snapshot or {}).get("protocol")
        item["target_protocol"] = (p.target_snapshot or {}).get("protocol")
        item["created_at"] = as_east_eight(p.created_at).isoformat() if p.created_at else None
        items.append(item)

    # status 的精确过滤在 DB 侧完成；此处做一层防御性复检
    if effective_status != "all":
        items = [i for i in items if i["status"] == effective_status]
    if protocol:
        items = [i for i in items if protocol.lower() in
                 {str(i.get("protocol", "")).lower(), str(i.get("target_protocol", "")).lower()}]
    if level:
        items = [i for i in items if i["level"] == level]
    try:
        since_at = as_east_eight(since) if since else None
        until_at = as_east_eight(until) if until else None
    except (TypeError, ValueError):
        raise HTTPException(400, "时间筛选须为 ISO 日期时间，无时区时按东八区解释") from None
    if since_at:
        items = [i for i in items if i["created_at"] and as_east_eight(i["created_at"]) >= since_at]
    if until_at:
        items = [i for i in items if i["created_at"] and as_east_eight(i["created_at"]) <= until_at]
    # 分页（审查 P2 + 复审修正）：切片必须在**全部过滤之后**——level/protocol 等
    # 是 Python 侧派生属性无法下推 SQL，先切页再过滤会静默漏项；
    # 无后置过滤时 items 即 DB 过滤结果，量级与旧实现相同
    return items[offset:offset + limit]


_LEVEL_BY_STATUS = {
    "draft": "none", "probing": "none", "failed": "none", "expired": "none",
    "L1_reachable": "L1",
    "L2_partial_no_contract": "L2_partial_no_contract",
    "transform_verified": "L2",
    "e2e_verified": "L3", "L3_partial": "L3_partial",
    "pending_review": "L2", "approved": "L2", "committed": "L2",
    "rejected": "none",
}


def _probe_level(p: ProbeSession) -> str:
    """会话达到的最高验证级别（列表/审计用）"""
    if p.l3_result and p.l3_result.get("level") in ("L3", "L3_partial"):
        return p.l3_result["level"]
    return _LEVEL_BY_STATUS.get(p.status, "none")


@router.get("/{probe_id}")
async def get_probe(probe_id: UUID, session: AsyncSession = Depends(get_session)):
    probe = await _get_session_or_404(session, probe_id)
    return _session_dict(probe)


@router.post("/{probe_id}/probe")
async def probe_l1(probe_id: UUID, session: AsyncSession = Depends(get_session),
                   via: str = "api"):
    """L1 可达性验证（不搬运业务数据）"""
    probe = await _get_session_or_404(session, probe_id)
    source = await _side_for(session, probe, "source")
    target = await _side_for(session, probe, "target")
    try:
        prober.consume_budget(probe, "source")
        prober.consume_budget(probe, "target")
        transition(probe, "probing", via=via, summary="L1 探测")
        l1 = await prober.run_l1(source, target)
        probe.l1_result = l1
        ok = prober.l1_ok(l1)
        transition(probe, "L1_reachable" if ok else "failed", via=via,
                   summary=f"L1 {'通过' if ok else '失败'}: "
                           f"source={l1['source'].get('status')}, target={l1['target'].get('status')}")
        await session.commit()
        return _session_dict(probe)
    except InvalidTransition as e:
        await session.rollback()
        raise HTTPException(409, detail={"code": "invalid_transition",
                                         "current": e.current, "message": str(e)})
    except ProbeBudgetExhausted as e:
        await session.rollback()
        raise HTTPException(429, detail={"code": e.reason, "message": str(e)})
    except TargetNotAllowed as e:
        # SSRF 防线（APIBRIDGE_PROBE_SSRF_ENFORCE=true 时生效）：目标被拒 → 会话失败
        # 而非 500；目标详情（host）不进响应，避免探测内网拓扑。
        # 顺序（复审修正）：先在存活会话上 transition/commit，再 rollback——
        # rollback 会 expire 掉 probe，之后读属性抛 MissingGreenlet → 500
        transition(probe, "failed", via=via,
                   summary="L1 失败: 目标被安全策略拒绝")
        await session.commit()
        await session.rollback()
        raise HTTPException(422, detail={"code": e.reason,
                                         "message": "目标地址被安全策略拒绝，"
                                                    "如确为内网目标请加入 APIBRIDGE_PROBE_ALLOWED_HOSTS"})


def _snapshot_conn(snapshot: dict | None):
    """从脱敏摘要重建 conn 形对象（仅用于重放场景；凭证已掩码不可解）"""
    data = snapshot or {}
    return SimpleNamespace(
        protocol=data.get("protocol") or "rest",
        base_url=data.get("base_url") or "",
        auth_type=data.get("auth_type"),
        auth_config=data.get("auth_config"),
        headers={},
        timeout_ms=30000,
        retry_count=3,
        db_type=data.get("db_type"),
        db_config=data.get("db_config"),
    )


async def _conn_by_id(session: AsyncSession, conn_id: UUID):
    conn = await session.get(ApiConnection, conn_id)
    if conn is None:
        raise HTTPException(404, f"连接不存在: {conn_id}")
    return conn


@router.post("/{probe_id}/infer")
async def infer(probe_id: UUID, session: AsyncSession = Depends(get_session),
                via: str = "api"):
    """源/目标 Schema 推断（要求已 L1_reachable）"""
    probe = await _get_session_or_404(session, probe_id)
    if probe.status != "L1_reachable":
        raise HTTPException(409, detail={
            "code": "invalid_state", "current": probe.status,
            "message": "infer 需要 L1_reachable 状态，请先执行 probe"})
    try:
        prober.consume_budget(probe, "source")
    except ProbeBudgetExhausted as e:
        raise HTTPException(429, detail={"code": e.reason, "message": str(e)})

    try:
        source = await _side_for(session, probe, "source")
        target = await _side_for(session, probe, "target")

        source.mapping_config = (probe.mapping_draft or {}).get('mapping_config') or {}
        sample = await prober.fetch_sample(source, limit=prober.DEFAULT_SAMPLE_SIZE)
    except TargetNotAllowed:
        # infer 拉样本同样过 SSRF 防线；拒绝即 422，host 不回显
        raise HTTPException(422, detail={"code": "target_not_allowed",
                                         "message": "目标地址被安全策略拒绝，"
                                                    "如确为内网目标请加入 APIBRIDGE_PROBE_ALLOWED_HOSTS"})
    # task 3.11：样本落库前脱敏
    probe.source_sample = desensitize_record(sample[:5])
    probe.source_schema = {"fields": infer_schema(sample)}

    contract, _required = await prober.get_target_contract(
        target, (probe.mapping_draft or {}).get("mapping_config"))
    probe.target_schema = {"fields": contract} if contract is not None else None
    await session.commit()
    # 返回完整会话字典：前端 setSession 需要统一结构（id/status 等）
    result = _session_dict(probe)
    result["sample_preview"] = probe.source_sample
    return result


@router.post("/{probe_id}/draft")
async def draft(probe_id: UUID, payload: DraftInput,
                session: AsyncSession = Depends(get_session)):
    """提交字段对应关系，服务端确定性校验（task 3.5/3.6）"""
    probe = await _get_session_or_404(session, probe_id)
    if probe.source_schema is None:
        raise HTTPException(409, detail={
            "code": "invalid_state", "current": probe.status,
            "message": "draft 需要先执行 infer 获取源 Schema"})

    source_fields = probe.source_schema.get("fields", [])
    # 目标侧校验依据：DB 表结构，或随本请求声明的契约（API 目标常见）
    target_fields = (probe.target_schema or {}).get("fields") or payload.target_contract or (payload.mapping_config or {}).get('target_contract') or None

    config = payload.mapping_config or {}
    from app.services.sync_config import validate_mapping_config
    try:
        validate_mapping_config(config)
    except ValueError:
        raise HTTPException(422, 'mapping_config 配置不合法') from None
    proposal = payload.fields or config.get('fields')
    if proposal:
        validated = validate_draft(source_fields, target_fields, proposal)
    else:
        # 无模型退化：同名 + 类型兼容（task 3.6）
        fields = generate_degenerate_draft(source_fields, target_fields or [])
        validated = validate_draft(source_fields, target_fields, fields)

    previous_headers = {k:v for k,v in (probe.mapping_draft or {}).items() if k in ('source_headers','target_headers')}
    probe.mapping_draft = {
        **previous_headers,
        "mapping_config": {
            **config,
            "fields": validated["fields"],
            **({"target_contract": payload.target_contract}
               if payload.target_contract else {}),
            **({"validations": (payload.fields or [{}])[0].get("validations")}
               if payload.fields and payload.fields[0].get("validations") else {}),
        },
        "items": validated["items"],
    }
    # 保留通过确定性校验的字段级缺失策略、默认值和稳定引用。
    original = {(f['source'],f['target']):f for f in proposal or []}
    for f in probe.mapping_draft['mapping_config']['fields']:
        entry = original.get((f['source'],f['target']), {})
        for key in ('on_missing','default_value','source_ref'):
            if key in entry:
                f[key] = entry[key]
    probe.draft_issues = {"issues": validated["issues"],
                          "missing_required": validated["missing_required"]}
    await session.commit()
    result = _session_dict(probe)
    result.update({
        "valid": validated["valid"],
        "fields": validated["fields"],
        "issues": validated["issues"],
        "missing_required": validated["missing_required"],
    })
    return result


@router.post("/{probe_id}/verify")
async def verify(probe_id: UUID, payload: VerifyInput,
                 session: AsyncSession = Depends(get_session)):
    """L2（零写入）/ L3（需人工授权）"""
    probe = await _get_session_or_404(session, probe_id)
    source = await _side_for(session, probe, "source")
    target = await _side_for(session, probe, "target")
    # task 3.10：L3 授权闸门最先检查——未授权时不暴露任何后续要求
    if payload.level.upper() == "L3" and not probe.e2e_authorized:
        raise HTTPException(403, detail={
            "code": "requires_human_authorization",
            "message": "L3 需要先在待确认清单中人工授权"})

    mapping_config = (probe.mapping_draft or {}).get("mapping_config", {})
    if not mapping_config.get("fields"):
        raise HTTPException(409, detail={
            "code": "invalid_state", "message": "verify 需要先提交 draft"})

    from app.services.field_policy import effective_config
    from types import SimpleNamespace
    effective = await effective_config(session, SimpleNamespace(mapping_config=mapping_config, source_endpoint=source.endpoint, target_endpoint=target.endpoint, source_table=source.table, target_table=target.table, incremental_field=None, sync_mode='full'), source.conn, target.conn)
    if payload.level.upper() == 'L3' and mapping_config.get('field_policy') == 'inherit' and (probe.l2_result or {}).get('effective_digest') != _mapping_digest(effective):
        raise HTTPException(409, '字段字典已变化，请重新执行 L2')
    previous_status = probe.status
    try:
        if payload.level.upper() == "L3":
            if effective.get('hap_write_mode') == 'upsert' and target.conn.protocol == 'hap':
                from sqlalchemy.ext.asyncio import async_sessionmaker
                factory = async_sessionmaker(session.bind, expire_on_commit=False)
                await session.commit()
                async def consume():
                    prober.consume_budget(probe, 'target')
                    await session.commit()
                result = await prober.run_l3(source, target, effective, session=probe,
                                            plan_factory=factory, budget_callback=consume)
            else:
                result = await prober.run_l3(source, target, effective, session=probe)
            probe.l3_result = result
            to_status = result["level"]  # e2e_verified | L3_partial
            if to_status in ("e2e_verified", "L3_partial"):
                if previous_status == "pending_review":
                    probe.l3_result = result  # 清单中复验：保留状态
                else:
                    transition(probe, to_status, via=payload.via,
                               summary=f"L3 {'比对一致' if result['valid'] else '存在缺口'}")
            await session.commit()
            return _session_dict(probe)

        # L2
        result = await prober.run_l2(source, target, effective)
        result["verified_at"] = datetime.now(timezone.utc).isoformat()
        result["mapping_digest"] = _mapping_digest(mapping_config)
        result["effective_digest"] = _mapping_digest(effective)
        probe.l2_result = desensitize_record(result)
        to_status = ("transform_verified" if result["level"] == "L2"
                     else result["level"])
        if previous_status == "pending_review":
            # 映射被改后的复验（4.6）：保持清单状态，更新结果
            pass
        else:
            transition(probe, to_status, via=payload.via,
                       summary=f"L2 {'通过' if result['valid'] else '未通过'}"
                               f"（样本 {result['sample_size']} 条）")
        await session.commit()
        return _session_dict(probe)
    except prober.RequiresHumanAuthorization as e:
        raise HTTPException(403, detail={"code": e.reason,
                                         "message": "L3 需要先在待确认清单中人工授权"})
    except TargetNotAllowed:
        # L2/L3 执行前同样过 SSRF 防线（fetch_sample/读回都走 _api_adapter）
        await session.rollback()
        raise HTTPException(422, detail={"code": "target_not_allowed",
                                         "message": "目标地址被安全策略拒绝，"
                                                    "如确为内网目标请加入 APIBRIDGE_PROBE_ALLOWED_HOSTS"})
    except ValueError as e:
        from app.adapters.errors import AdapterError
        detail = e.as_dict() if isinstance(e, AdapterError) else {'code':'verification_failed', 'message':'验证失败，请检查配置和源字段'}
        raise HTTPException(422, detail=detail) from None
    except InvalidTransition as e:
        await session.rollback()
        raise HTTPException(409, detail={"code": "invalid_transition",
                                         "current": e.current, "message": str(e)})


@router.post("/{probe_id}/submit")
async def submit(probe_id: UUID, payload: SubmitInput,
                 session: AsyncSession = Depends(get_session)):
    """提交至待确认清单（门槛：达到 L2 且非 no_contract；task 4.3）"""
    probe = await _get_session_or_404(session, probe_id)
    cfg = (probe.mapping_draft or {}).get('mapping_config') or {}
    if cfg.get('field_policy') == 'inherit':
        source = await _side_for(session, probe, 'source')
        target = await _side_for(session, probe, 'target')
        from app.services.field_policy import effective_config
        from types import SimpleNamespace
        effective = await effective_config(session, SimpleNamespace(mapping_config=cfg,
            source_endpoint=source.endpoint, target_endpoint=target.endpoint,
            source_table=source.table, target_table=target.table, incremental_field=None, sync_mode='full'), source.conn, target.conn)
        if (probe.l2_result or {}).get('effective_digest') != _mapping_digest(effective):
            raise HTTPException(409, '字段字典已变化，请重新执行 L2')
    if probe.l2_result is None or probe.l2_result.get("level") == "L2_partial_no_contract":
        raise HTTPException(409, detail={
            "code": "below_l2_threshold",
            "message": "未达到 L2（或目标无契约），不能进入待确认清单",
            "next_action": "先执行 verify 使结果为 transform_verified；"
                           "API 目标需在 mapping_config['target_contract'] 声明契约",
        })
    probe.decided_by = payload.decided_by
    try:
        transition(probe, "pending_review", via=payload.via, summary="提交待确认清单")
    except InvalidTransition as e:
        raise HTTPException(409, detail={"code": "invalid_transition",
                                         "current": e.current, "message": str(e)})
    await session.commit()
    return _session_dict(probe)


# ============================================================
# 待确认清单：批准 / 驳回 / L3 授权（task 4.4-4.10）
# ============================================================

import hashlib  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from app.security.crypto import CryptoError, encrypt_auth_config, encrypt_db_config  # noqa: E402


def _mapping_digest(mapping_config: dict) -> str:
    """映射配置指纹：变更检测（task 4.5/4.6）"""
    canonical = repr(sorted((mapping_config or {}).items(), key=str))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class ApproveInput(BaseModel):
    decided_by: str | None = None
    via: str = "ui"
    sync_mode: str = Field("sync", description="sync|async|auto")
    # 已存在的连接直接引用；新连接由批准人在界面填写最终配置（含明文凭据）
    source_connection_id: UUID | None = None
    target_connection_id: UUID | None = None
    source_connection: SideInput | None = None
    target_connection: SideInput | None = None
    mapping_config: dict | None = Field(
        None, description="修改后的映射（触发确定性重校验）")


class RejectInput(BaseModel):
    reason: str = Field(..., min_length=1, description="驳回原因（必填）")
    decided_by: str | None = None
    via: str = "ui"


class AuthorizeInput(BaseModel):
    authorized_by: str | None = None
    via: str = "ui"


def _final_mapping_config(probe: ProbeSession,
                          override: dict | None) -> tuple[dict, bool]:
    """取最终映射配置；返回 (配置, 是否相对草案被修改)"""
    draft_cfg = (probe.mapping_draft or {}).get("mapping_config", {})
    if override is None:
        return draft_cfg, False
    return override, _mapping_digest(override) != _mapping_digest(draft_cfg)


async def approve_probe(probe_id: UUID, payload: ApproveInput,
                        session: AsyncSession = Depends(get_session)):
    """批准：单事务创建 连接+映射+任务（task 4.4-4.7；agent 无此入口）"""
    probe = await _get_session_or_404(session, probe_id)
    if probe.status == "rejected":
        raise HTTPException(409, detail={
            "code": "already_rejected", "current": probe.status,
            "message": "已驳回的会话不可再批准"})
    if probe.status != "pending_review":
        raise HTTPException(409, detail={
            "code": "invalid_state", "current": probe.status,
            "message": "仅 pending_review 状态可批准"})

    # 并发/双击守卫（审查）：条件 UPDATE 占住行锁并复检状态——第二个并发
    # approve 在第一个提交后 claim 落空 → 409，不会重复建桥
    claimed = await session.execute(
        update(ProbeSession)
        .where(ProbeSession.id == probe.id,
               ProbeSession.status == "pending_review")
        .values(reject_reason=probe.reject_reason)
        .execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        raise HTTPException(409, detail={
            "code": "already_processing", "current": probe.status,
            "message": "该会话正在被处理或状态已变化，请刷新后重试"})

    # task 4.5：携带修改后的映射 → 强制确定性重校验
    final_cfg, modified = _final_mapping_config(probe, payload.mapping_config)
    if modified:
        source_fields = (probe.source_schema or {}).get("fields", [])
        target_fields = ((probe.target_schema or {}).get("fields")
                         or final_cfg.get("target_contract") or [])
        revalidated = validate_draft(source_fields, target_fields,
                                     final_cfg.get("fields", []))
        if not revalidated["valid"]:
            raise HTTPException(409, detail={
                "code": "mapping_revalidation_failed",
                "current": probe.status,
                "issues": revalidated["issues"],
                "message": "修改后的映射未通过确定性校验，保持 pending_review"})

    # task 4.6：L2 过期或映射被修改 → 要求重跑 L2
    l2 = probe.l2_result or {}
    if final_cfg.get('field_policy') == 'inherit':
        from app.services.field_policy import effective_config
        from types import SimpleNamespace
        src = await _side_for(session, probe, 'source')
        tgt = await _side_for(session, probe, 'target')
        cfg = await effective_config(session, SimpleNamespace(mapping_config=final_cfg, source_endpoint=src.endpoint, target_endpoint=tgt.endpoint, source_table=src.table, target_table=tgt.table, incremental_field=None, sync_mode='full'), src.conn, tgt.conn)
        if l2.get('effective_digest') != _mapping_digest(cfg):
            raise HTTPException(409, '字段字典已变化，请重新执行 L2')
    verified_at = l2.get("verified_at")
    try:
        verified_time = as_east_eight(verified_at) if verified_at else None
    except (TypeError, ValueError):
        verified_time = None  # Invalid evidence requires re-verification, never approval.
    stale = (
        not l2 or l2.get("level") != "L2"
        or verified_time is None
        or datetime.now(timezone.utc)
        - verified_time
        > timedelta(minutes=settings.probe_l2_ttl_minutes)
        or modified
    )
    if stale:
        raise HTTPException(409, detail={
            "code": "l2_stale",
            "message": "L2 结果过期或映射已被修改，请重跑 L2 后再批准",
            "next_action": "POST /probes/{id}/verify 重新执行 L2"})

    try:
        # ---- 连接：优先引用已存在，其次会话内引用，最后按批准人输入创建 ----
        connection_ids: dict[str, str] = {}
        for role, side_in in (("source", payload.source_connection),
                              ("target", payload.target_connection)):
            existing_id = (payload.source_connection_id if role == "source"
                           else payload.target_connection_id) \
                or (probe.source_connection_id if role == "source"
                    else probe.target_connection_id)
            if existing_id:
                # 悬挂引用守卫（审查）：待批准期间连接被删 → 422 而不是建出坏桥
                if await session.get(ApiConnection, existing_id) is None:
                    raise HTTPException(422, detail={
                        "code": "connection_not_found",
                        "message": f"{role} 引用的连接不存在（可能已被删除），"
                                   "请在批准请求中提供该侧的最终连接配置"})
                connection_ids[role] = str(existing_id)
                continue
            if side_in is None:
                raise HTTPException(409, detail={
                    "code": "connection_config_required",
                    "message": f"{role} 连接既未引用也未提供最终配置"})
            conn = ApiConnection(
                id=uuid.uuid4(),
                name=f"probe-{str(probe.id)[:8]}-{role}",
                connection_type=role,
                protocol=side_in.protocol or "rest",
                base_url=side_in.base_url,
                auth_type=side_in.auth_type,
                auth_config=encrypt_auth_config(side_in.auth_type,
                                                side_in.auth_config),
                headers=side_in.headers,
                timeout_ms=side_in.timeout_ms,
                db_type=side_in.db_type,
                db_config=encrypt_db_config(side_in.db_config),
            )
            session.add(conn)
            await session.flush()
            connection_ids[role] = str(conn.id)

        # ---- 映射 ----
        mapping = FieldMapping(
            id=uuid.uuid4(),
            name=f"probe-{str(probe.id)[:8]}",
            source_connection_id=UUID(connection_ids["source"]),
            target_connection_id=UUID(connection_ids["target"]),
            source_endpoint=probe.source_endpoint,
            target_endpoint=probe.target_endpoint,
            source_table=(probe.source_snapshot or {}).get("table"),
            target_table=(probe.target_snapshot or {}).get("table"),
            mapping_config=final_cfg,
        )
        session.add(mapping)
        await session.flush()

        # ---- 任务 ----
        task = SyncTask(
            id=uuid.uuid4(),
            mapping_id=mapping.id,
            task_type="realtime",
            sync_mode=payload.sync_mode,
        )
        session.add(task)
        await session.flush()

        # ---- 全部创建成功后才置状态（任一失败上面已抛，事务回滚，4.7）----
        transition(probe, "approved", via=payload.via,
                   summary=f"批准 by {payload.decided_by or 'unknown'}，"
                           f"级别 {l2.get('level')}")
        transition(probe, "committed", via=payload.via, summary="单事务建桥完成")
        probe.decided_by = payload.decided_by
        probe.bridge_ids = {
            "connection_ids": [connection_ids["source"], connection_ids["target"]],
            "mapping_id": str(mapping.id),
            "task_id": str(task.id),
            # task 4.10 审计：操作者/时间/来源/批准时级别/最终映射摘要
            "approved_by": payload.decided_by,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "approved_via": payload.via,
            "approved_level": l2.get("level"),
            "mapping_digest": _mapping_digest(final_cfg),
        }
        await session.commit()
        _drop_inline(probe.id)      # 终态即清内存明文凭据
        return _session_dict(probe)
    except HTTPException:
        await session.rollback()
        raise
    except CryptoError as e:
        await session.rollback()
        raise HTTPException(422, detail={"code": "encrypt_key_missing",
                                         "message": str(e)})
    except Exception as e:  # task 4.7：任一步失败全回滚，状态保持 pending_review
        await session.rollback()
        raise HTTPException(500, detail={"code": "approve_failed",
                                         "message": f"建桥事务失败已回滚: {e}"})


@router.post("/{probe_id}/approve")
async def approve(probe_id: UUID, payload: ApproveInput,
                  session: AsyncSession = Depends(get_session)):
    """批准入口（管理界面专用；CLI/agent 无此能力——spec bridge-review-queue）"""
    return await approve_probe(probe_id, payload, session)


@router.post("/{probe_id}/reject")
async def reject(probe_id: UUID, payload: RejectInput,
                 session: AsyncSession = Depends(get_session)):
    """驳回（必填原因；rejected 不可再批准——task 4.8）"""
    probe = await _get_session_or_404(session, probe_id)
    try:
        transition(probe, "rejected", via=payload.via,
                   summary=f"驳回 by {payload.decided_by or 'unknown'}: {payload.reason}")
    except InvalidTransition as e:
        raise HTTPException(409, detail={"code": "invalid_transition",
                                         "current": e.current, "message": str(e)})
    probe.decided_by = payload.decided_by
    probe.reject_reason = payload.reason
    await session.commit()
    _drop_inline(probe.id)
    return _session_dict(probe)


@router.post("/{probe_id}/authorize-e2e")
async def authorize_e2e(probe_id: UUID, payload: AuthorizeInput,
                        session: AsyncSession = Depends(get_session)):
    """人工授权 L3（task 4.9）：授权后 verify --level L3 才会真正写目标"""
    probe = await _get_session_or_404(session, probe_id)
    if probe.status not in ("transform_verified", "pending_review"):
        raise HTTPException(409, detail={
            "code": "invalid_state", "current": probe.status,
            "message": "仅达到 L2 的会话可授权端到端验证"})
    probe.e2e_authorized = True
    probe.authorized_by = payload.authorized_by
    audit = list(probe.audit_log or [])
    audit.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "action": "authorize_e2e", "via": payload.via,
        "by": payload.authorized_by,
        "summary": "人工授权 L3：将向目标写入 1 条真实数据",
    })
    probe.audit_log = audit
    await session.commit()
    return _session_dict(probe)
