"""BI 只读访问授权（add-bi-access-scope）

一份**授权档案** = 一个只读令牌 + 一块可见范围（看板白名单 / 行过滤 / 列隐藏）。
本模块只放"纯逻辑 + 身份解析"，端点接线在 `app/api/bi.py`：

- `issue_token()`：生成令牌（明文只在创建/轮换响应出现一次）
- `hash_token()`：sha256 十六进制（库内只存哈希；恒定时间比较）
- `resolve_viewer()`：FastAPI 依赖——管理令牌=全权，档案令牌=受限只读，其余 401
- 范围纯函数：`row_filter_allows` / `scope_buckets` / `scope_defn` / `scope_rows` /
  `inject_row_filters` / `field_allowed`（全部无 IO，便于单测）

**安全规则**（design §3）：
1. 行过滤字段必须是卡片维度之一，否则卡片对该档案**不可见**（桶按维度组合存储，
   不按该维度分组的卡片无法在桶层隔离行 → 宁可少给，不可多漏）；
2. 即席查询的授权行过滤是**前置条件**（同名字段以授权值为准，客户端无法放宽）；
3. 隐藏字段在响应里**不存在**（服务端剥离，不是前端不显示）；被即席请求引用 → 403；
4. 范围外的看板/卡片一律 404 `not_visible`（不泄露存在性）。
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bi import BiAccessProfile
from app.security.auth import check_admin_token, extract_token
from app.services.bi_expr import extract_refs

# 令牌前缀：便于在日志/界面上识别"这是一把 BI 只读令牌"
TOKEN_PREFIX = "bit_"

# 档案身份放行的**只读白名单**（默认拒绝：不在表内一律 403）
# 注意 adhoc 是 POST 但语义只读（不落库，见 add-bi-adhoc-analysis design §2）
READONLY_BI_ENDPOINTS: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/api/v1/bi/status"),
    ("GET", "/api/v1/bi/dimensions"),
    ("GET", "/api/v1/bi/form-fields"),
    ("GET", "/api/v1/bi/form-rows"),
    ("GET", "/api/v1/bi/metrics"),
    ("GET", "/api/v1/bi/screens"),
    ("GET", "/api/v1/bi/screen-settings"),
    ("POST", "/api/v1/bi/adhoc/query"),
})
# /status 只回是否启用（前端 BiGate 每次进 BI 页面都先问它），受限身份必须可读：
# 否则投放链接打开后卡在"无法读取 BI 状态，请检查登录与连接"，永远到不了屏幕页（实测）。


def issue_token() -> str:
    """生成令牌明文（只在创建/轮换响应里出现一次）"""
    return f"{TOKEN_PREFIX}{secrets.token_hex(16)}"


def hash_token(token: str) -> str:
    """令牌哈希（库内不存明文；128bit 随机源的 sha256 足够）"""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def valid_token_format(token: str | None) -> bool:
    tok = str(token or "")
    return tok.startswith(TOKEN_PREFIX) and len(tok) == len(TOKEN_PREFIX) + 32


@dataclass
class Viewer:
    """请求身份：admin=全权；profile=受限只读"""

    role: str = "admin"
    profile: BiAccessProfile | None = None
    profile_key: str | None = None
    name: str = "管理员"

    @property
    def restricted(self) -> bool:
        return self.role != "admin"

    # ---- 范围读取（admin 恒为全放行） ----
    @property
    def boards(self) -> list[str]:
        if self.profile is None:
            return []
        return [str(b) for b in (self.profile.boards or [])]

    @property
    def row_filters(self) -> dict[str, Any]:
        if self.profile is None:
            return {}
        return {str(k): v for k, v in (self.profile.row_filters or {}).items()}

    @property
    def hidden_fields(self) -> set[str]:
        if self.profile is None:
            return set()
        return {str(f) for f in (self.profile.hidden_fields or [])}

    def board_allowed(self, board: str) -> bool:
        if not self.restricted:
            return True
        keys = self.boards
        return (not keys) or (str(board) in keys)      # 空 = 不限看板


ADMIN_VIEWER = Viewer()


def _unauthorized(code: str, message: str) -> HTTPException:
    return HTTPException(401, {"code": code, "message": message})


async def resolve_viewer(request: Request,
                         session: AsyncSession) -> Viewer:
    """解析请求身份（FastAPI 依赖）

    - 管理令牌命中 → 全权；
    - `X-BI-Token` 命中有效档案 → 受限只读（**每次实时查库，停用/轮换立即生效**）；
    - 其余 → 401。档案身份的端点白名单在本函数内校验（默认拒绝，写操作一律 403）。
    """
    if check_admin_token(extract_token(request.headers))[0]:
        return ADMIN_VIEWER

    bi_token = request.headers.get("x-bi-token")
    if not bi_token:
        raise _unauthorized("unauthorized", "缺少管理令牌或 BI 访问令牌")

    row = (await session.execute(
        select(BiAccessProfile).where(
            BiAccessProfile.token_hash == hash_token(bi_token))
    )).scalar_one_or_none()
    if row is None:
        raise _unauthorized("bi_token_invalid", "BI 访问令牌无效")
    if not row.enabled:
        raise _unauthorized("bi_token_disabled", "BI 访问令牌已停用")

    method = request.method.upper()
    path = request.url.path.rstrip("/") or request.url.path
    allowed = (method, path) in READONLY_BI_ENDPOINTS or any(
        method == m and path.startswith(prefix + "/")
        for m, prefix in (("GET", "/api/v1/bi/metrics"),)
    )
    if not allowed:
        raise HTTPException(403, {
            "code": "readonly_token",
            "message": "BI 访问令牌为只读：该端点需要管理令牌",
        })
    return Viewer(role="profile", profile=row, profile_key=row.key, name=row.name)


def touch_profile(session: AsyncSession, viewer: Viewer) -> bool:
    """标记最近使用（是否需要由调用方提交；失败不影响请求）"""
    if viewer.profile is None:
        return False
    from datetime import datetime, timezone

    viewer.profile.last_used_at = datetime.now(timezone.utc)
    return True


# ============================================================
# 范围计算（纯函数）
# ============================================================

def field_allowed(field: str | None, viewer: Viewer) -> bool:
    """字段对档案是否可见（admin 恒可见；隐藏字段判定用字符串等值）"""
    if field is None or not viewer.restricted:
        return True
    return str(field) not in viewer.hidden_fields


def _measure_uses_hidden(m: dict[str, Any], hidden: set[str]) -> bool:
    """度量是否触达隐藏字段：key 与 field 双口径，ratio 分量同样检查

    只按 key 过滤挡不住 `{key:"总成本", field:"cost"}` 这种改名引用
    （审查 P2：保存卡路径可绕过列隐藏）。
    """
    if not isinstance(m, dict):
        return False
    if str(m.get("key")) in hidden or str(m.get("field")) in hidden:
        return True
    for side in ("numerator", "denominator"):
        comp = m.get(side) if isinstance(m.get(side), dict) else {}
        if str(comp.get("field")) in hidden:
            return True
    return False


def _computed_uses_hidden(computed: Any, hidden: set[str]) -> str | None:
    """派生列是否引用隐藏字段（表达式引用或列名本身）→ 返回命中的 key"""
    from app.services.bi_expr import extract_refs
    for c in computed or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("key")) in hidden or str(c.get("name")) in hidden:
            return str(c.get("key"))
        for ref in extract_refs(str(c.get("expr") or "")):
            if ref in hidden:
                return str(c.get("key"))
    return None


def scope_defn(defn: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
    """剥离隐藏的度量/维度/派生列（用于列表与响应；不修改入参）

    与 adhoc 侧（adhoc_hidden_field_used）同三口径：度量 key+field、
    维度 field+name、computed 表达式引用——保存卡路径不得比即席路径宽松。
    """
    if not viewer.restricted or not viewer.hidden_fields:
        return defn
    hidden = set(viewer.hidden_fields)
    out = dict(defn)
    out["dimensions"] = [d for d in (defn.get("dimensions") or [])
                         if not (isinstance(d, dict)
                                 and (str(d.get("field")) in hidden
                                      or str(d.get("name")) in hidden))]
    out["measures"] = [m for m in (defn.get("measures") or [])
                       if not _measure_uses_hidden(m, hidden)]
    used = _computed_uses_hidden(defn.get("computed"), hidden)
    if used:
        # 引用隐藏字段的派生列整列剥离（该列的聚合值可反推隐藏字段）
        out["computed"] = [c for c in (defn.get("computed") or [])
                           if not (isinstance(c, dict)
                                   and str(c.get("key")) == used)]
    return out


def _bucket_dim_value(bucket: dict[str, Any], field: str) -> Any:
    """桶里按维度名/字段名取值（维度键是显示名 name，同时兼容 field 口径）"""
    dims = bucket.get("dimensions") or {}
    if field in dims:
        return dims[field]
    return None


def row_filter_allows(defn: dict[str, Any], viewer: Viewer) -> tuple[bool, str]:
    """行过滤是否可安全施加：每个过滤字段都必须是该定义的**维度之一**

    桶按「维度组合」存储——卡片不按该维度分组时，平台无法在桶层隔离行
    （退化为"临时重算"代价高且易错）→ 判定为不可见（宁可少给，不可多漏）。

    **例外**：`detail` 卡不走预聚合桶（实时读行后按行过滤），不受此限制；
    即席查询同理（行过滤在聚合条件里生效）。
    """
    if not viewer.restricted:
        return True, ""
    if str(defn.get("chart_type") or "") == "detail":
        return True, ""
    dim_names = {str((d or {}).get("name")) for d in (defn.get("dimensions") or [])}
    dim_fields = {str((d or {}).get("field")) for d in (defn.get("dimensions") or [])}
    for f in viewer.row_filters:
        if f not in dim_names and f not in dim_fields:
            return False, f"row_filter_dimension_missing:{f}"
    return True, ""


def scope_buckets(buckets: list[dict[str, Any]], viewer: Viewer,
                  defn: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """读桶范围：按行过滤等值筛选 + 剥离隐藏度量/维度值

    维度键是显示名（name）；调用方传 defn 以便按 field 口径匹配过滤字段。
    值层按 **剥离后定义的度量 key 白名单** 过滤：桶值以度量 key 为键，原实现
    拿 key 与隐藏字段名比对，改名度量（key≠field）会从 raw/table 输出泄漏
    （审查 P2）——调用方需先 scope_defn 再传入。
    """
    if not viewer.restricted:
        return buckets
    # 剥离后定义的可见度量 key 集合：内部先 scope_defn（幂等），调用方无论
    # 传原始还是已剥离定义都得到正确白名单
    if defn is not None:
        defn = scope_defn(defn, viewer)
    filters = viewer.row_filters
    hidden = viewer.hidden_fields
    visible_measures: set[str] | None = None
    if defn is not None:
        visible_measures = {str(m.get("key")) for m in (defn.get("measures") or [])
                            if isinstance(m, dict) and m.get("key")}
    # field → 该维度的显示名（桶内键）
    field_to_name: dict[str, str] = {}
    for d in ((defn or {}).get("dimensions") or []):
        if isinstance(d, dict) and d.get("field"):
            field_to_name[str(d["field"])] = str(d.get("name") or d["field"])
    out: list[dict[str, Any]] = []
    for b in buckets:
        dims = b.get("dimensions") or {}
        ok = True
        for f, val in filters.items():
            key = field_to_name.get(f, f)
            actual = dims.get(key)
            if str("" if actual is None else actual) != str("" if val is None else val):
                ok = False
                break
        if not ok:
            continue
        if visible_measures is not None:
            values = {k: v for k, v in (b.get("values") or {}).items()
                      if k in visible_measures}
        else:
            values = {k: v for k, v in (b.get("values") or {}).items()
                      if k not in hidden}
        out.append({**b, "dimensions": {k: v for k, v in dims.items() if k not in hidden},
                    "values": values})
    return out


def scope_metric_list(defns: list[dict[str, Any]], viewer: Viewer,
                      metric_keys_by_board: dict[str, set[str]],
                      allowed_boards: list[str] | None = None,
                      ) -> tuple[list[dict[str, Any]], list[str]]:
    """卡片范围：返回 (可见定义列表, 因范围被隐藏的 metric_key 列表)

    - 看板白名单非空时：卡片必须摆在授权看板上（"摆放即归属"）；
    - 行过滤维度缺失 → 不可见；
    - 剥离隐藏字段后若无可见度量（或 detail 卡无可列）→ 不可见。
    """
    visible: list[dict[str, Any]] = []
    hidden_keys: list[str] = []
    for defn in defns:
        key = str(defn.get("metric_key"))
        if viewer.restricted and allowed_boards:
            placed = set()
            for b in allowed_boards:
                placed |= metric_keys_by_board.get(b, set())
            # fail-closed（与 _ensure_metric_visible 同口径）：授权看板一张卡都没摆
            # → 全部不可见；`if placed and ...` 会放行整张指标列表（含 source_ref）
            if not placed or key not in placed:
                hidden_keys.append(key)
                continue
        ok, _reason = row_filter_allows(defn, viewer)
        if not ok:
            hidden_keys.append(key)
            continue
        scoped = scope_defn(defn, viewer)
        if viewer.restricted and defn.get("chart_type") != "detail" \
                and not (scoped.get("measures") or []):
            hidden_keys.append(key)
            continue
        visible.append(scoped)
    return visible, hidden_keys


def scope_rows(rows: list[dict[str, Any]], viewer: Viewer) -> list[dict[str, Any]]:
    """明细行范围：按行过滤（等值，多字段 AND）+ 剥离隐藏列（键不存在于响应）"""
    if not viewer.restricted:
        return rows
    filters = viewer.row_filters
    hidden = viewer.hidden_fields
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ok = True
        for f, val in filters.items():
            actual = r.get(f)
            if str("" if actual is None else actual) != str("" if val is None else val):
                ok = False
                break
        if not ok:
            continue
        out.append({k: v for k, v in r.items() if k not in hidden})
    return out


def inject_row_filters(filters: Any, viewer: Viewer) -> Any:
    """即席：把授权行过滤并入查询条件（**授权侧优先**：同名字段以授权值替换）

    - 入参为结构化规则列表（list）或旧等值字典（dict）或空；
    - 授权过滤的字段类型按字符串比较即可（等值语义，与内存匹配器一致）。
    """
    if not viewer.restricted or not viewer.row_filters:
        return filters
    rules = list(filters) if isinstance(filters, list) else []
    if isinstance(filters, dict):
        rules = [{"field": k, "op": "eq", "value": v} for k, v in filters.items()]
    auth_rules = [{"field": f, "op": "eq", "value": v, "field_type": "string",
                   "source": "access_profile"}
                  for f, v in viewer.row_filters.items()]
    auth_fields = {r["field"] for r in auth_rules}
    kept = [r for r in rules
            if not (isinstance(r, dict) and str(r.get("field")) in auth_fields)]
    return auth_rules + kept


def adhoc_hidden_field_used(payload: dict[str, Any], viewer: Viewer) -> str | None:
    """即席请求是否引用了隐藏字段（度量 key / 维度 field / 计算字段 expr 引用）
    → 返回首个命中的字段名"""
    if not viewer.restricted or not viewer.hidden_fields:
        return None
    hidden = viewer.hidden_fields
    for m in (payload.get("measures") or []):
        if not isinstance(m, dict):
            continue
        if str(m.get("key")) in hidden or str(m.get("field")) in hidden:
            return str(m.get("key") or m.get("field"))
        # ratio 分子/分母引用隐藏字段同样放行不出门（与 scope 侧三口径对齐，
        # 复审修正：key≠field 的 ratio 可借道绕过列隐藏）
        for side in ("numerator", "denominator"):
            comp = m.get(side) if isinstance(m.get(side), dict) else {}
            if str(comp.get("field")) in hidden:
                return str(comp.get("field"))
    for d in (payload.get("dimensions") or []):
        if isinstance(d, dict) and (str(d.get("field")) in hidden
                                    or str(d.get("name")) in hidden):
            return str(d.get("field") or d.get("name"))
    for f in ([payload.get("filters")] if isinstance(payload.get("filters"), dict)
              else (payload.get("filters") or [])):
        if isinstance(f, dict) and str(f.get("field")) in hidden:
            return str(f.get("field"))
    # 计算字段（P0 修复）：`computed[].expr` 里的 [引用名] 会把隐藏字段的原始值
    # 算进派生列（再作维度/度量即绕过列隐藏）——引用名与 key/name 双口径检查
    for c in (payload.get("computed") or []):
        if not isinstance(c, dict):
            continue
        expr = str(c.get("expr") or "")
        for ref in extract_refs(expr):
            if ref in hidden:
                return str(c.get("key") or ref)
        if str(c.get("name")) in hidden:
            return str(c.get("name"))
    return None
