"""BI 聚合查询与指标定义管理 API

设计依据：design D6/D8/D11；spec「预聚合结果查询」「图表格式转换契约」
「聚合定义配置化」「业务维度与度量由用户配置」。

查询走**预聚合表**，不扫描明细；定义管理在写入前做确定性校验。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.bi import (
    BiAccessProfile,
    BiAggregationWatermark,
    BiMetricBucket,
    BiMetricDef,
    BiScreenSettings,
)
from app.models.sync import ApiConnection
from app.bi_schemas import (
    BiAdhocQueryIn,
    BiDimensionOut,
    BiMetricDefIn,
    BiMetricDefOut,
)
from app.services.bi_aggregator import (
    backfill,
    bucket_start_for,
    estimate_cardinality,
    run_metric,
    to_utc,
)
from app.services.bi_defs import (
    DIMENSION_CANDIDATES,
    classify_bi_type,
    has_errors,
    normalize_computed,
    validate_metric_def,
)
from app.services.bi_access import (
    READONLY_BI_ENDPOINTS as _READONLY_BI_ENDPOINTS,      # noqa: F401（文档用，见模块注释）
    Viewer,
    adhoc_hidden_field_used,
    inject_row_filters,
    issue_token,
    field_allowed,
    hash_token,
    resolve_viewer,
    row_filter_allows,
    scope_buckets,
    scope_defn,
    scope_metric_list,
    scope_rows,
    touch_profile,
    valid_token_format,
)
from app.services.bi_adhoc import (
    AdhocError,
    build_adhoc_defn,
    query_adhoc_defn,
)
from app.services.bi_expr import ExprError
from app.services.bi_format import FORMATS, convert
from app.services.bi_sources import (
    SourceError,
    fetch_form_rows,
    fetch_form_rows_page,
    parse_form_ref,
    snapshot_info,
)

logger = logging.getLogger(__name__)

# ============================================================
# 身份依赖（add-bi-access-scope）：管理令牌=全权；BI 令牌=受限只读
# ============================================================


async def get_viewer(request: Request,
                     session: AsyncSession = Depends(get_session)) -> Viewer:
    """解析请求身份（含只读白名单校验：档案身份访问非白名单端点 → 403）"""
    viewer = await resolve_viewer(request, session)
    # last_used_at 落库（审查 P3）：只读端点不 commit 曾让该字段永不写入，
    # 运维据"最近使用"判断令牌是否还在用会误判；admin 身份无 profile 是 no-op
    if touch_profile(session, viewer):
        try:
            await session.commit()
        except Exception:
            await session.rollback()
    return viewer


# 路由级身份依赖：**所有 /bi 端点**（含写）都先过身份解析与只读白名单——档案身份访问
# 写端点在此 403（默认拒绝）；FastAPI 在同一请求内按依赖缓存，故只解析一次身份。
# 另叠加：
# - require_bi：BI 未启用时端点整体不可见（404），/status 例外（前端据此隐藏入口）；
# - route_class=BiRoute：BI 请求预算（30s + 最多 2 个并发），与同步链路互不阻塞。
from app.config import settings
from app.services.bi_runtime import BiRoute, loop_lock


async def require_bi(request: Request):
    if request.url.path.endswith("/status"):
        return
    if not settings.bi_enabled:
        raise HTTPException(404, "BI 未启用")


router = APIRouter(
    dependencies=[Depends(require_bi), Depends(get_viewer)],
    route_class=BiRoute,
)


@router.get("/status")
async def bi_status():
    """BI 启用状态（前端据此隐藏/显示入口；BI 关闭时仍可访问）"""
    return {"enabled": settings.bi_enabled,
            "aggregation_enabled": settings.bi_enabled and settings.bi_aggregation_enabled}


async def _get_def(session: AsyncSession, metric_key: str) -> BiMetricDef:
    row = (await session.execute(
        select(BiMetricDef).where(BiMetricDef.metric_key == metric_key)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"指标不存在: {metric_key}")
    return row


def _to_out(row: BiMetricDef) -> BiMetricDefOut:
    """ORM → 响应模型（集中一处，避免字段遗漏）"""
    return BiMetricDefOut(
        metric_key=row.metric_key, name=row.name, description=row.description,
        source_type=row.source_type, source_ref=row.source_ref,
        granularity=row.granularity,
        dimensions=row.dimensions or [], measures=row.measures or [],
        filters=row.filters, chart_type=row.chart_type, top_n=row.top_n,
        enabled=row.enabled, style=row.style, builtin=row.builtin,
        computed=row.computed or [], display=row.display or {},
    )


async def _normalize_calc_refs(session: AsyncSession, defn: dict[str, Any]) -> dict[str, Any]:
    """计算字段引用规范化（add-bi-calc-fields）

    form 源且声明了 computed 时：取表单字段元数据（中文显示名 → 字段键），
    把表达式里的 [引用名] 重写为 [字段键]（派生列引用重写为其 key）。
    引用不存在 → 400；连接/表单元数据不可用 → 502。
    返回可能重写后的 defn（其余情况原样返回）。
    """
    computed = defn.get("computed") or []
    if defn.get("source_type") != "form" or not computed:
        return defn
    try:
        ref = json.loads(defn.get("source_ref") or "{}")
    except json.JSONDecodeError:
        return defn  # 结构错误交由 validate 报 invalid_form_source
    if not isinstance(ref, dict) or not ref.get("connection_id") or not ref.get("form_key"):
        return defn
    _, fields = await _load_form_field_meta(
        session, str(ref["connection_id"]), str(ref["form_key"]))
    name_to_path: dict[str, str] = {}
    for f in fields:
        name_to_path[f["name"]] = f["path"]
        name_to_path.setdefault(f["path"], f["path"])
    try:
        rewritten = normalize_computed(computed, name_to_path)
    except ExprError as e:
        raise HTTPException(400, {"code": "invalid_computed_expr", "message": str(e)})
    return {**defn, "computed": rewritten}


async def _calc_ref_names(session: AsyncSession, defn: dict[str, Any]) -> set[str] | None:
    """精确引用集合（表单字段 name/path + 派生列 key/name）；非 form 或无 computed 返回 None"""
    computed = defn.get("computed") or []
    if defn.get("source_type") != "form" or not computed:
        return None
    try:
        ref = json.loads(defn.get("source_ref") or "{}")
        _, fields = await _load_form_field_meta(
            session, str(ref["connection_id"]), str(ref["form_key"]))
    except (json.JSONDecodeError, KeyError, HTTPException):
        return None  # 元数据不可用时退宽松（结构错误另行报错）
    names = {f["name"] for f in fields} | {f["path"] for f in fields}
    for c in computed:
        if isinstance(c, dict):
            if c.get("key"):
                names.add(str(c["key"]))
            if c.get("name"):
                names.add(str(c["name"]))
    return names


def _parse_time(value: str | None, field: str) -> datetime | None:
    if not value:
        return None
    try:
        return to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        raise HTTPException(400, f"{field} 不是合法的 ISO 时间: {value}")


def _parse_dim_filters(raw: list[str] | None) -> dict[str, str]:
    """解析 `?dim=name:value` 形式的维度过滤"""
    out: dict[str, str] = {}
    for item in raw or []:
        if ":" not in item:
            raise HTTPException(400, f"维度过滤格式应为 name:value，收到: {item}")
        name, _, value = item.partition(":")
        out[name.strip()] = value.strip()
    return out


async def _load_buckets(
    session: AsyncSession,
    defn: BiMetricDef,
    start: datetime | None,
    end: datetime | None,
    dim_filters: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """从预聚合表读桶（按时间升序）；维度过滤在内存侧完成（桶数量级小）

    时间边界必须**对齐到桶**：`from=10:00:12` 而 1h 桶起点是 `10:00:00` 时，
    若按精确时间比较会把整个粗粒度桶排除，导致查询恒空（已实测踩到）。
    """
    metric_key = defn.metric_key
    granularity = defn.granularity or "1min"
    stmt = select(BiMetricBucket).where(BiMetricBucket.metric_key == metric_key)
    if start is not None:
        stmt = stmt.where(
            BiMetricBucket.bucket_start >= bucket_start_for(start, granularity))
    if end is not None:
        stmt = stmt.where(BiMetricBucket.bucket_start < end)
    stmt = stmt.order_by(BiMetricBucket.bucket_start)
    rows = (await session.execute(stmt)).scalars().all()

    out: list[dict[str, Any]] = []
    from app.services.bi_aggregator import OTHERS_KEY, display_dims

    def _dim_eq(actual: Any, expected: str) -> bool:
        # 维度值归一比较（审查 P2）：桶里数值维度保留原类型（5.0），前端候选值
        # 是 JSON 数字字符串化（"5"）——纯 str() 比较会整卡筛空
        if str("" if actual is None else actual) == expected:
            return True
        try:
            return float(actual) == float(expected)   # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    for r in rows:
        dims = r.dimensions or {}
        # Top-N 归并项在存储层是哨兵值（__others__）：读取时还原为「其他」并带上
        # is_others 标记（format 层的 truncated 提示依赖它）
        is_others = any(v == OTHERS_KEY for v in (dims or {}).values())
        if is_others:
            dims = display_dims(dims)
        if dim_filters and any(not _dim_eq(dims.get(k), v)
                               for k, v in dim_filters.items()):
            continue
        out.append({
            "bucket_start": r.bucket_start,
            "dimensions": dims,
            "values": r.values or {},
            **({"is_others": True} if is_others else {}),
        })
    return out


# ============================================================
# 候选维度
# ============================================================

@router.get("/dimensions", response_model=list[BiDimensionOut])
async def list_dimensions():
    """返回业务维度候选及其基数等级与用法提示（D11）"""
    return DIMENSION_CANDIDATES


# ============================================================
# 表单数据源元数据与明细行（2026-09-25 重定位新增）
# ============================================================

@router.get("/form-fields")
async def form_fields(connection_id: str, form_key: str,
                      session: AsyncSession = Depends(get_session),
                      viewer: Viewer = Depends(get_viewer)) -> dict[str, Any]:
    """表单字段元数据 + BI 逻辑类型（定义页维度/度量候选的数据源）

    复用字段端点同款取数口径（path = 运行时行键），并叠加 classify_bi_type
    归一（借鉴 DataEase deType+groupType：dimension/number/time），
    定义页据此自动分类维度与度量候选。
    """
    kind, fields = await _load_form_field_meta(session, connection_id, form_key)
    if viewer.restricted:
        # 字段树也不泄露隐藏字段（"看不见的列"在字段树里就不该出现）
        fields = [f for f in fields if field_allowed(str(f.get("path")), viewer)]
    return {"kind": kind, "form_key": form_key, "fields": fields}


async def _load_form_field_meta(
    session: AsyncSession, connection_id: str, form_key: str,
) -> tuple[str, list[dict[str, Any]]]:
    """取表单字段元数据（path/name/type/bi_type），返回 (kind, fields)

    form-fields 端点与计算字段的引用规范化（normalize_computed）共用。
    """
    import uuid as _uuid

    try:
        conn_id = _uuid.UUID(connection_id)
    except ValueError:
        raise HTTPException(400, f"connection_id 不是合法 UUID: {connection_id}")
    conn = await session.get(ApiConnection, conn_id)
    if conn is None:
        raise HTTPException(404, f"连接不存在: {connection_id}")

    # 统一走字段端点同款取数（目标发行包的字段策略重构后，字段字典/别名由
    # get_connection_fields → field_policy.enrich_fields 解析，不再直接读旧字段字典）
    from app.api.connections import get_connection_fields
    result = await get_connection_fields(
        conn_id=conn_id, table=form_key if conn.db_type else None,
        worksheet_id=form_key if conn.protocol == "hap" else None, session=session)
    fields = [dict(f) for f in result.get("fields") or [] if f.get("readable", True)]
    for f in fields:
        f["bi_type"] = classify_bi_type(f.get("display_type") or f["type"])
        if f["bi_type"] == "dimension":
            f["bi_type"] = classify_bi_type(f["type"])
    kind = result["kind"]

    return kind, fields


@router.get("/form-rows")
async def form_rows(connection_id: str, form_key: str,
                    limit: int = Query(50, ge=1, le=500),
                    page: int | None = Query(None, ge=1,
                                             description="分页页码（提供时走分页下推）"),
                    page_size: int = Query(100, ge=1, le=200),
                    keyword: str | None = Query(None, description="关键字过滤"),
                    sort_field: str | None = Query(None, description="排序字段"),
                    order: str = Query("asc", pattern="^(asc|desc)$"),
                    session: AsyncSession = Depends(get_session),
                    viewer: Viewer = Depends(get_viewer)) -> dict[str, Any]:
    """表单原始行明细（明细表图表数据源）

    - 不带 page：**旧调用兼容**语义（全量读取并按 limit 截断，total=读取到的行数）；
      ⚠️ 大表不要这样调：全量读会触读取上限并 502（实测百万行表 + limit=200）——
      大表逐行分析一律带 page（分页下推），前端筛选栏候选值也走分页；
    - 带 page：分页查询（add-bi-detail-query）——DB 源分页/排序下推（不拉全量）、
      HAP 源缓存内切片；keyword 在 DB 源降级内存过滤并标 fallback。
    """
    try:
        if page is not None:
            if viewer.restricted:
                # 行过滤必须先于分页：走内存分支（缓存内过滤再切片），
                # 与既有 keyword 降级路径同构（add-bi-access-scope design §4）
                rows_all = await fetch_form_rows(session, connection_id, form_key)
                rows_all = scope_rows(rows_all, viewer)
                if keyword:
                    from app.services.bi_sources import _filter_rows_in_memory
                    rows_all = _filter_rows_in_memory(rows_all, keyword)
                from app.services.bi_sources import _sort_rows_in_memory
                ordered = _sort_rows_in_memory(rows_all, sort_field, order)
                offset = (max(1, page) - 1) * page_size
                window = ordered[offset:offset + page_size]
                return {
                    "form_key": form_key, "rows": window, "page": page,
                    "page_size": page_size,
                    "has_more": offset + page_size < len(ordered),
                    "total": len(ordered), "fallback": "row_permission",
                    "warn": len(ordered) > 50000,
                    "snapshot": snapshot_info(connection_id, form_key),
                }
            result = await fetch_form_rows_page(
                session, connection_id, form_key, page=page,
                page_size=page_size, keyword=keyword,
                sort_field=sort_field, order=order)
            result["form_key"] = form_key
            result["snapshot"] = snapshot_info(connection_id, form_key)
            return result
        rows = await fetch_form_rows(session, connection_id, form_key)
    except SourceError as e:
        # 详情在日志里，对外只给可操作提示（驱动异常原文可能含 DSN/凭证）
        logger.warning("BI 明细读取失败（%s/%s）: %s", connection_id, form_key, e)
        raise HTTPException(502, "BI 数据读取失败，请检查数据源与配置") from None
    if viewer.restricted:
        rows = scope_rows(rows, viewer)
    return {"form_key": form_key, "total": len(rows), "rows": rows[:limit],
            # HAP 增量拉取的可观测性（add-bi-hap-incremental）：模式/增量行数/水位线/
            # 上次全量时间——"删除最迟何时消失"由此可读；DB 源为 None
            "snapshot": snapshot_info(connection_id, form_key)}


# ============================================================
# 即席分析（add-bi-adhoc-analysis）
# ============================================================

@router.post("/adhoc/query")
async def adhoc_query(payload: BiAdhocQueryIn,
                      session: AsyncSession = Depends(get_session),
                      viewer: Viewer = Depends(get_viewer)) -> dict[str, Any]:
    """分析工作台：任意维度组合即时聚合（不落库）

    - DB 源且可下推 → 源库 SQL GROUP BY（不拉全量行，`engine="pushdown"`）；
      否则走内存快照并如实标 `engine="memory"` + `fallback_reason`；
    - 校验与定义保存端点同一套规则（`validate_metric_def`）——分析页能出的图，
      另存为卡片必然合法；`defn` 即回传可直接保存的定义；
    - 计算字段引用归一（中文显示名 → 字段键）复用保存端点的 `_normalize_calc_refs`。
    """
    body = payload.model_dump()
    if viewer.restricted:
        # 隐藏字段被显式引用 → 403（明确告知，便于集成方排查）
        hidden = adhoc_hidden_field_used(body, viewer)
        if hidden:
            raise HTTPException(403, {"code": "hidden_field",
                                      "message": f"字段 {hidden} 对该访问令牌不可见"})
        # 授权行过滤是前置条件：并入 filters（同名字段以授权值为准，客户端无法放宽）
        body["filters"] = inject_row_filters(body.get("filters"), viewer)
    defn = build_adhoc_defn(body)
    try:
        defn = await _normalize_calc_refs(session, defn)
        ref_names = await _calc_ref_names(session, defn)
        result = await query_adhoc_defn(
            session, defn, fmt=body.get("format"), sort=body.get("sort"),
            ref_names=ref_names)
        if viewer.restricted:
            result["defn"] = scope_defn(result["defn"], viewer)
        return result
    except AdhocError as e:
        raise HTTPException(400, {"code": "invalid_adhoc", "issues": e.issues})
    except SourceError as e:
        logger.warning("BI 即席查询读取失败（%s/%s）: %s",
                       body.get("connection_id"), body.get("form_key"), e)
        raise HTTPException(502, "BI 数据读取失败，请检查数据源与配置") from None


# ============================================================
# 访问授权辅助（add-bi-access-scope）
# ============================================================

async def _metric_keys_by_board(session: AsyncSession) -> dict[str, set[str]]:
    """看板 → 该看板上的卡片 key 集合（"摆放即归属"）"""
    rows = (await session.execute(select(BiScreenSettings))).scalars().all()
    out: dict[str, set[str]] = {}
    for r in rows:
        cells = ((r.data or {}).get("cells") or {})
        out[str(r.id)] = {str(k) for k in cells.keys()}
    return out


async def _scope_cells(session: AsyncSession, cells: dict[str, Any],
                       viewer: Viewer) -> tuple[dict[str, Any], list[str]]:
    """看板设置里的 cards 裁剪：剔除"对档案不可见"的卡片（行过滤维度缺失/无可见度量）"""
    keys = list(cells.keys())
    if not keys:
        return {}, []
    rows = (await session.execute(
        select(BiMetricDef).where(BiMetricDef.metric_key.in_(keys)))).scalars().all()
    by_key = {str(r.metric_key): _to_out(r).model_dump() for r in rows}
    visible: dict[str, Any] = {}
    hidden: list[str] = []
    for key, cell in cells.items():
        defn = by_key.get(str(key))
        if defn is None:
            hidden.append(str(key))          # 定义已删除：不泄露
            continue
        ok, _reason = row_filter_allows(defn, viewer)
        scoped = scope_defn(defn, viewer)
        if not ok or (defn.get("chart_type") != "detail"
                      and not (scoped.get("measures") or [])):
            hidden.append(str(key))
            continue
        visible[str(key)] = cell
    return visible, hidden


async def _ensure_metric_visible(session: AsyncSession, defn: BiMetricDef,
                                 viewer: Viewer) -> None:
    """卡片可见性校验（不可见一律 404 not_visible，不区分"不存在"与"无权限"）"""
    full = defn.to_def_dict()
    ok, _reason = row_filter_allows(full, viewer)
    if not ok:
        raise HTTPException(404, {"code": "not_visible",
                                  "message": "卡片不存在或无权访问"})
    if viewer.boards:                       # 授权看板非空 → 卡片必须摆在其中之一
        by_board = await _metric_keys_by_board(session)
        placed = set()
        for b in viewer.boards:
            placed |= by_board.get(str(b), set())
        # fail-closed：授权看板一张卡都没摆（placed 空）→ 什么都不可见；
        # 原写法 `if placed and ...` 会放行全部卡片（摆位为空 = 白给）
        if not placed or str(defn.metric_key) not in placed:
            raise HTTPException(404, {"code": "not_visible",
                                      "message": "卡片不存在或无权访问"})


def _profile_out(row: BiAccessProfile, *, token: str | None = None) -> dict[str, Any]:
    """档案响应（**永不回显 token_hash**；明文仅在创建/轮换时一次性返回）"""
    out = {
        "key": row.key, "name": row.name, "enabled": bool(row.enabled),
        "boards": row.boards or [], "row_filters": row.row_filters or {},
        "hidden_fields": row.hidden_fields or [], "note": row.note,
        "token_prefix": row.token_prefix,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if token:
        out["token"] = token               # 一次性：仅创建/轮换响应
    return out


@router.get("/access/profiles")
async def list_access_profiles(
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """列出访问授权档案（仅管理令牌可访问：档案身份被只读白名单拦下）"""
    rows = (await session.execute(
        select(BiAccessProfile).order_by(BiAccessProfile.created_at))).scalars().all()
    return [_profile_out(r) for r in rows]


@router.post("/access/profiles", status_code=201)
async def create_access_profile(payload: dict[str, Any],
                                session: AsyncSession = Depends(get_session)
                                ) -> dict[str, Any]:
    """新建授权档案：返回体含**一次性**令牌明文（库内只存哈希）"""
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, {"code": "missing_name", "message": "缺少 name"})
    boards = [str(b) for b in (payload.get("boards") or [])]
    row_filters = {str(k): v for k, v in (payload.get("row_filters") or {}).items()}
    hidden = [str(f) for f in (payload.get("hidden_fields") or [])]
    token = issue_token()
    row = BiAccessProfile(
        key=uuid.uuid4().hex, name=name, token_hash=hash_token(token),
        token_prefix=token[:12], enabled=bool(payload.get("enabled", True)),
        boards=boards, row_filters=row_filters, hidden_fields=hidden,
        note=(str(payload.get("note") or "") or None),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _profile_out(row, token=token)


@router.put("/access/profiles/{key}")
async def update_access_profile(key: str, payload: dict[str, Any],
                                session: AsyncSession = Depends(get_session)
                                ) -> dict[str, Any]:
    """更新档案（名称/看板/行过滤/隐藏字段/启用态）；不改令牌"""
    row = await session.get(BiAccessProfile, key)
    if row is None:
        raise HTTPException(404, f"授权档案不存在: {key}")
    if "name" in payload:
        row.name = str(payload["name"]).strip() or row.name
    if "boards" in payload:
        row.boards = [str(b) for b in (payload.get("boards") or [])]
    if "row_filters" in payload:
        row.row_filters = {str(k): v
                           for k, v in (payload.get("row_filters") or {}).items()}
    if "hidden_fields" in payload:
        row.hidden_fields = [str(f) for f in (payload.get("hidden_fields") or [])]
    if "enabled" in payload:
        row.enabled = bool(payload["enabled"])
    if "note" in payload:
        row.note = str(payload.get("note") or "") or None
    from datetime import datetime, timezone

    row.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(row)
    return _profile_out(row)


@router.post("/access/profiles/{key}/rotate")
async def rotate_access_token(key: str,
                              session: AsyncSession = Depends(get_session)
                              ) -> dict[str, Any]:
    """轮换令牌（旧令牌立即失效；新明文只在此响应出现一次）"""
    row = await session.get(BiAccessProfile, key)
    if row is None:
        raise HTTPException(404, f"授权档案不存在: {key}")
    token = issue_token()
    row.token_hash = hash_token(token)
    row.token_prefix = token[:12]
    await session.commit()
    await session.refresh(row)
    return _profile_out(row, token=token)


@router.delete("/access/profiles/{key}")
async def delete_access_profile(key: str,
                                session: AsyncSession = Depends(get_session)
                                ) -> dict[str, Any]:
    """删除档案（令牌立即失效）"""
    row = await session.get(BiAccessProfile, key)
    if row is None:
        raise HTTPException(404, f"授权档案不存在: {key}")
    await session.delete(row)
    await session.commit()
    return {"message": "已删除", "key": key}


# ============================================================
# 大屏看板设置（布局顺序/宽度 + 筛选元素；布局与图表定义分离存储）
# ============================================================



@router.get("/screen-settings")
async def get_screen_settings(
    board: str = Query("default", description="看板 key，缺省为默认看板"),
    session: AsyncSession = Depends(get_session),
    viewer: Viewer = Depends(get_viewer),
) -> dict[str, Any]:
    """读取看板设置（cells 布局、筛选元素、看板名）；无设置时返回空对象

    受限身份：范围外的看板一律 404 `not_visible`（不泄露存在性）。
    """
    if not viewer.board_allowed(board):
        raise HTTPException(404, {"code": "not_visible",
                                  "message": "看板不存在或无权访问"})
    row = await session.get(BiScreenSettings, board)
    data = (row.data if row else {}) or {}
    if viewer.restricted:
        # 只暴露授权看板上的卡片（cells），其余键（布局/主题等）照旧
        cells = data.get("cells") or {}
        visible, _hidden = await _scope_cells(session, cells, viewer)
        data = {**data, "cells": visible}
    return data


def _validate_screen_payload(payload: dict[str, Any]) -> None:
    """看板设置形状护栏（P1）

    整字段覆盖 × 任意 JSON 的组合曾把默认看板的 cells 与看板名整块覆盖掉
    （2026-09-29 实测）——保存前验形状，不合形状直接 422 不落库。
    只做结构校验不约束语义：未知 metric_key 照旧由前端渲染时忽略。
    """
    name = payload.get("name")
    if name is not None and not isinstance(name, str):
        raise HTTPException(422, detail={"code": "invalid_screen_settings",
                                         "message": "name 必须是字符串"})
    cells = payload.get("cells")
    if cells is not None:
        if not isinstance(cells, dict):
            raise HTTPException(422, detail={"code": "invalid_screen_settings",
                                             "message": "cells 必须是 {metric_key: {x,y,w,h}} 对象"})
        for k, v in cells.items():
            if not isinstance(k, str) or not isinstance(v, dict):
                raise HTTPException(422, detail={"code": "invalid_screen_settings",
                                                 "message": f"cells[{k!r}] 必须是坐标对象"})
            for axis in ("x", "y", "w", "h"):
                if axis in v and (isinstance(v[axis], bool)
                                  or not isinstance(v[axis], (int, float))):
                    raise HTTPException(422, detail={
                        "code": "invalid_screen_settings",
                        "message": f"cells[{k!r}].{axis} 必须是数值"})
    fdims = payload.get("filter_dims")
    if fdims is not None and not (isinstance(fdims, list)
                                  and all(isinstance(x, str) for x in fdims)):
        raise HTTPException(422, detail={"code": "invalid_screen_settings",
                                         "message": "filter_dims 必须是字符串列表"})


@router.put("/screen-settings")
async def put_screen_settings(
    payload: dict[str, Any],
    board: str = Query("default", description="看板 key，缺省为默认看板"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """保存看板设置（整字段覆盖）

    接受任意 JSON 对象，当前约定键：
    - name: 看板显示名（大屏标题）
    - cells: {metric_key: {x, y, w, h}}——**在 cells 里即上屏**（摆放即归属）
    - filter_dims: [维度名...]
    - layout: {"order": [...], "spans": {...}}（旧版半宽/全宽布局，仅迁移用）
    未知的 metric_key 由前端渲染时忽略，不做后端强校验；形状不合直接 422。
    """
    _validate_screen_payload(payload)
    row = await session.get(BiScreenSettings, board)
    if row is None:
        row = BiScreenSettings(id=board, data=payload)
        session.add(row)
    else:
        row.data = payload
    await session.commit()
    return {"saved": True}


# ============================================================
# 看板管理（多看板：bi_screen_settings 每行一块画布，id 即 board key）
# ============================================================

@router.get("/screens")
async def list_screens(
    session: AsyncSession = Depends(get_session),
    viewer: Viewer = Depends(get_viewer),
) -> list[dict[str, Any]]:
    """列出全部看板（含无设置的空看板；default 为默认看板，不可删除）

    default 行尚不存在时补一条虚拟项——默认看板永远可见可用
    （首次保存设置时才真正落库，与旧行为兼容）。
    """
    rows = (await session.execute(
        select(BiScreenSettings).order_by(BiScreenSettings.updated_at))).scalars().all()
    out = [
        {
            "key": r.id,
            "name": (r.data or {}).get("name")
                    or ("默认看板" if r.id == "default" else r.id),
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]
    if "default" not in {s["key"] for s in out}:
        out.insert(0, {"key": "default", "name": "默认看板", "updated_at": None})
    # 访问授权（add-bi-access-scope）：档案身份只看到授权内的看板（空=不限）
    if viewer.restricted:
        out = [s for s in out if viewer.board_allowed(str(s["key"]))]
    return out


@router.post("/screens", status_code=201)
async def create_screen(
    payload: dict[str, Any],
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """新建看板：{key?: 唯一标识（缺省自动生成）, name: 显示名}"""
    key = str(payload.get("key") or "").strip() or uuid.uuid4().hex[:12]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", key):
        raise HTTPException(400, "key 仅允许字母/数字/下划线/中划线，长度 1-50")
    if key == "default":
        raise HTTPException(400, "default 为默认看板保留名")
    if await session.get(BiScreenSettings, key) is not None:
        raise HTTPException(409, f"看板已存在: {key}")
    name = str(payload.get("name") or "").strip() or key
    row = BiScreenSettings(id=key, data={
        "name": name, "layout": {}, "filter_dims": [], "cells": {}})
    session.add(row)
    await session.commit()
    return {"key": key, "name": name}


@router.put("/screens/{key}")
async def rename_screen(
    key: str, payload: dict[str, Any],
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """重命名看板：{name: 显示名}"""
    row = await session.get(BiScreenSettings, key)
    if row is None:
        raise HTTPException(404, f"看板不存在: {key}")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name 不能为空")
    data = dict(row.data or {})
    data["name"] = name
    row.data = data
    await session.commit()
    return {"key": key, "name": name}


@router.delete("/screens/{key}")
async def delete_screen(
    key: str, session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """删除看板（画布设置连带删除；指标定义不受影响）。默认看板不可删除"""
    if key == "default":
        raise HTTPException(400, "默认看板不可删除")
    row = await session.get(BiScreenSettings, key)
    if row is None:
        raise HTTPException(404, f"看板不存在: {key}")
    await session.delete(row)
    await session.commit()
    return {"deleted": key}


# ============================================================
# 指标定义管理
# ============================================================

@router.get("/metrics", response_model=list[BiMetricDefOut])
async def list_metrics(enabled: bool | None = Query(None),
                       session: AsyncSession = Depends(get_session),
                       viewer: Viewer = Depends(get_viewer)):
    """列出指标定义

    受限身份（add-bi-access-scope）：只返回"授权看板上 + 行过滤维度齐备 + 剥离隐藏字段
    后仍有用度量"的卡片；不可见原因不下发给受限身份（管理侧可见）。
    """
    stmt = select(BiMetricDef)
    if enabled is not None:
        stmt = stmt.where(BiMetricDef.enabled.is_(enabled))
    rows = (await session.execute(stmt.order_by(BiMetricDef.metric_key))).scalars().all()
    defns = [_to_out(r).model_dump() for r in rows]
    if viewer.restricted:
        by_board = await _metric_keys_by_board(session)
        defns, _hidden = scope_metric_list(defns, viewer, by_board, viewer.boards)
    return defns


@router.post("/metrics", status_code=201, response_model=BiMetricDefOut)
async def create_metric(payload: BiMetricDefIn,
                        session: AsyncSession = Depends(get_session)):
    """创建指标定义（写入前做确定性校验）"""
    defn = payload.model_dump()
    if not defn.get("metric_key"):
        raise HTTPException(400, {"code": "missing_metric_key", "message": "缺少 metric_key"})

    # 计算字段引用规范化（中文显示名 → 字段键）；随后按精确引用集合校验
    defn = await _normalize_calc_refs(session, defn)
    issues = validate_metric_def(defn, ref_names=await _calc_ref_names(session, defn))
    if has_errors(issues):
        raise HTTPException(400, {"code": "invalid_definition", "issues": issues})

    exists = (await session.execute(
        select(BiMetricDef).where(BiMetricDef.metric_key == defn["metric_key"])
    )).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(409, f"指标已存在: {defn['metric_key']}")

    row = BiMetricDef(**defn, builtin=False)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _to_out(row)


@router.put("/metrics/{metric_key}", response_model=BiMetricDefOut)
async def update_metric(metric_key: str, payload: BiMetricDefIn,
                        session: AsyncSession = Depends(get_session)):
    """更新指标定义（校验后整字段覆盖）"""
    row = await _get_def(session, metric_key)
    defn = payload.model_dump()
    defn["metric_key"] = metric_key  # 路径为准，不允许改名

    defn = await _normalize_calc_refs(session, defn)
    issues = validate_metric_def(defn, ref_names=await _calc_ref_names(session, defn))
    if has_errors(issues):
        raise HTTPException(400, {"code": "invalid_definition", "issues": issues})

    # 定义变化 → 历史桶与新定义不可比（改 granularity 还会让水位锚在旧偏移上、
    # 回填后同窗两桶 → 可加算子跨桶求和翻倍）：清空该指标全部桶并删水位线，
    # 下次聚合按"最新完整窗口"重新起算（iter_windows 无水位线语义）（审查 P2）
    _AFFECTING = ("source_type", "source_ref", "time_field", "granularity",
                  "dimensions", "measures", "filters", "chart_type", "computed",
                  "top_n")
    old_snap = json.dumps({k: row.to_def_dict().get(k) for k in _AFFECTING},
                          sort_keys=True, ensure_ascii=False, default=str)
    new_snap = json.dumps({k: defn.get(k) for k in _AFFECTING},
                          sort_keys=True, ensure_ascii=False, default=str)
    definition_changed = old_snap != new_snap

    for field in ("name", "description", "source_type", "source_ref", "time_field",
                  "granularity", "dimensions", "measures", "filters", "chart_type",
                  "top_n", "enabled", "style", "computed", "display"):
        setattr(row, field, defn.get(field))
    if definition_changed:
        await session.execute(
            delete(BiMetricBucket).where(BiMetricBucket.metric_key == metric_key))
        wm = await session.get(BiAggregationWatermark, metric_key)
        if wm is not None:
            await session.delete(wm)
    await session.commit()
    await session.refresh(row)
    return _to_out(row)


@router.delete("/metrics/{metric_key}")
async def delete_metric(metric_key: str,
                        session: AsyncSession = Depends(get_session)):
    """删除指标定义及其预聚合结果"""
    row = await _get_def(session, metric_key)
    await session.delete(row)
    await session.execute(
        delete(BiMetricBucket).where(BiMetricBucket.metric_key == metric_key))
    await session.commit()
    return {"message": "已删除", "metric_key": metric_key}


# ============================================================
# 结果查询
# ============================================================

@router.get("/metrics/{metric_key}")
async def query_metric(
    metric_key: str,
    start: str | None = Query(None, alias="from"),
    end: str | None = Query(None, alias="to"),
    fmt: str = Query("raw", alias="format"),
    dim: list[str] | None = Query(None, description="维度过滤，形如 name:value，可重复"),
    session: AsyncSession = Depends(get_session),
    viewer: Viewer = Depends(get_viewer),
) -> dict[str, Any]:
    """查询预聚合结果（支持时间范围、维度过滤与格式转换）"""
    if fmt not in FORMATS:
        raise HTTPException(400, f"不支持的格式: {fmt}；可用: {sorted(FORMATS)}")

    defn = await _get_def(session, metric_key)

    # 访问授权（add-bi-access-scope）：行过滤维度缺失或不在授权看板 → 不可见（404）
    if viewer.restricted:
        await _ensure_metric_visible(session, defn, viewer)

    # 明细表图表：不走预聚合桶，实时读表单行（与聚合共享行缓存）
    if defn.chart_type == "detail":
        if defn.source_type != "form":
            raise HTTPException(400, "明细表图表仅支持 form 数据源")
        conn_id, form_key = parse_form_ref(defn.to_def_dict())
        try:
            rows = await fetch_form_rows(session, conn_id, form_key)
        except SourceError as e:
            logger.warning("BI 明细卡读取失败（%s/%s）: %s", conn_id, form_key, e)
            raise HTTPException(502, "BI 数据读取失败，请检查数据源与配置") from None
        if viewer.restricted:
            rows = scope_rows(rows, viewer)
        # 上限保护（审查 P2）：旧实现一次回吐全部行（最多 10 万行 JSON），
        # 与明细卡"服务端分页"的既定方向相悖——明细卡前端已走 /bi/form-rows
        from app.services.bi_sources import DETAIL_PAGE_MAX
        rows = rows[:DETAIL_PAGE_MAX]
        return {
            "metric_key": metric_key,
            "format": fmt,
            "bucket_count": 0,
            "data": {"rows": rows},
        }

    buckets = await _load_buckets(
        session, defn,
        _parse_time(start, "from"), _parse_time(end, "to"),
        _parse_dim_filters(dim),
    )
    scoped_defn = defn.to_def_dict()
    if viewer.restricted:
        # 先 scope_defn（剥离隐藏度量/维度/派生列）再 scope_buckets：
        # 值层白名单依据剥离后的度量 key 集合（顺序反了会整表放行）
        scoped_defn = scope_defn(scoped_defn, viewer)
        buckets = scope_buckets(buckets, viewer, scoped_defn)
    data = convert(scoped_defn, buckets, fmt)
    return {
        "metric_key": metric_key,
        "format": fmt,
        "bucket_count": len(buckets),
        "data": data,
    }


@router.post("/metrics/{metric_key}/estimate")
async def estimate_metric_cardinality(
    metric_key: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """预估最近窗口的维度组合数（保存前的基数告警，D11）"""
    defn = await _get_def(session, metric_key)
    return await estimate_cardinality(session, defn.to_def_dict())


# ============================================================
# 手动触发 / 回填
# ============================================================

@router.post("/aggregate/run")
async def run_aggregation(
    metric_key: str | None = Query(None, alias="metric"),
    start: str | None = Query(None, alias="from"),
    end: str | None = Query(None, alias="to"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """手动触发一轮聚合；给定 from/to 时执行区间回填"""
    if metric_key:
        rows = [await _get_def(session, metric_key)]
    else:
        rows = (await session.execute(
            select(BiMetricDef).where(BiMetricDef.enabled.is_(True))
        )).scalars().all()

    start_dt, end_dt = _parse_time(start, "from"), _parse_time(end, "to")
    results = []
    for row in rows:
        defn = row.to_def_dict()
        try:
            if start_dt and end_dt:
                r = await backfill(session, defn, start_dt, end_dt)
                r["mode"] = "backfill"
            else:
                r = await run_metric(session, defn, now=datetime.now(timezone.utc))
                r["mode"] = "incremental"
            results.append(r)
        except Exception as e:  # 单指标失败不阻断其余
            logger.exception("手动聚合失败: %s", defn.get("metric_key"))
            results.append({"metric_key": defn.get("metric_key"), "error": str(e)})

    return {"metrics": len(rows), "results": results}
