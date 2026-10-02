"""BI 聚合数据源适配

职责边界（design D4）：
- 只负责**取窗口内原始行**，不写聚合 SQL；
- 分组/度量/分位数/Top N 全部在 Python 侧完成（见 bi_aggregator）；
- 维度解析器（task → 连接）在此预加载为内存字典，避免 SQL join。

JSON 解析成本（design D5）：使用 ORM 的 JSON 列，SQLAlchemy 每行解析一次并缓存为
Python 对象，后续取多个字段是内存字典访问——天然满足「每行只解析一次」。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.sync import (
    ApiConnection,
    DataBuffer,
    ExecutionLog,
    FieldMapping,
    SyncTask,
)
from app.services.bi_defs import DEFAULT_SHARD_THRESHOLD, is_read_only_sql
from app.config import settings
from app.security.desensitize import desensitize_record

logger = logging.getLogger(__name__)

# 单次分片拉取行数（分片兜底，防单窗口超大时一次性入内存）
SHARD_BATCH = 20_000

# form 源拉行硬上限（借鉴 DataEase 查询防护三件套；HAP 适配器自身也封顶 10 万行）
MAX_FORM_ROWS = 100_000

# 明细分页行数警告线（add-bi-detail-query 分档提示）
FORM_ROWS_WARN = 50_000

# 明细单页上限
DETAIL_PAGE_MAX = 200

# form 源 DB 拉行批量（分页往返粒度；大表深翻页成本随 OFFSET 二次增长）
FORM_FETCH_BATCH = 20_000

# form 源行缓存 TTL（秒）：同一表单的多个指标/明细查询每分钟只拉一次源
FORM_CACHE_TTL_SECONDS = 60.0

_form_cache: dict[str, tuple[float, list[dict]]] = {}
_form_locks: dict[str, asyncio.Lock] = {}


class SourceError(Exception):
    """数据源配置或执行错误"""


def parse_form_ref(defn: dict[str, Any]) -> tuple[str, str]:
    """解析 form 源的 source_ref → (connection_id, form_key)"""
    raw = defn.get("source_ref") or ""
    try:
        cfg = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as e:
        raise SourceError(f"form 源 source_ref 不是合法 JSON: {e}") from e
    if not isinstance(cfg, dict) \
            or not str(cfg.get("connection_id") or "").strip() \
            or not str(cfg.get("form_key") or "").strip():
        raise SourceError(
            'form 源 source_ref 必须是 {"connection_id":"…","form_key":"…"} JSON')
    return str(cfg["connection_id"]).strip(), str(cfg["form_key"]).strip()


async def fetch_form_rows(session: AsyncSession, connection_id: str,
                          form_key: str) -> list[dict]:
    """拉取连接表单的全部行（TTL 缓存 + 并发按 key 去重）

    - HAP：当前 V2 工厂适配器 get_worksheet_rows（1000/页自动翻页）；
    - DB：当前数据库连接器（按页读取，凭证在其解密路径内流转）；
    - 行键口径：HAP 行键 = alias||controlId，与字段端点 path 一致，直接作维度/度量字段。
    """
    conn = await session.get(ApiConnection, uuid.UUID(connection_id))
    if conn is None:
        raise SourceError("连接不存在")
    from app.services.field_policy import definitions
    defs = await definitions(session, conn, form_key)
    policy = tuple(sorted((key, field.name, field.readable, field.writable) for key, field in defs.items()))
    key = f"{connection_id}:{form_key}:{conn.updated_at}:{policy}"

    # Bounded cache; removal is safe because locked entries are never evicted.
    now = time.monotonic()
    for old in list(_form_locks):
        if old not in _form_cache and not _form_locks[old].locked():
            _form_locks.pop(old, None)
    for old in list(_form_cache):
        if now - _form_cache[old][0] >= FORM_CACHE_TTL_SECONDS and not _form_locks.get(old, asyncio.Lock()).locked():
            _form_cache.pop(old, None)
            _form_locks.pop(old, None)
    if key not in _form_cache and len(_form_cache) >= settings.bi_cache_entries:
        old = next((k for k in _form_cache if not _form_locks.get(k, asyncio.Lock()).locked()), None)
        if old is not None:
            _form_cache.pop(old, None)
            _form_locks.pop(old, None)
    now = time.monotonic()
    hit = _form_cache.get(key)
    if hit and now - hit[0] < FORM_CACHE_TTL_SECONDS:
        return hit[1]

    lock = _form_locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _form_cache.get(key)
        if hit and time.monotonic() - hit[0] < FORM_CACHE_TTL_SECONDS:
            return hit[1]
        try:
            rows = await _fetch_form_rows_uncached(session, connection_id, form_key)
        except BaseException:
            raise
        _form_cache[key] = (time.monotonic(), rows)
        # 过期/逐出缓存时一起回收未持有的锁。
        return rows


def _sort_rows_in_memory(rows: list[dict], field: str | None,
                         order: str = "asc") -> list[dict]:
    """内存排序：数值优先按数值序，文本按字典序（None 视为空串）"""
    if not field:
        return rows

    def key(r: dict):
        v = r.get(field)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return (0, float(v), "")
        return (1, 0.0, "" if v is None else str(v))

    return sorted(rows, key=key, reverse=str(order).lower() == "desc")


def _filter_rows_in_memory(rows: list[dict], keyword: str) -> list[dict]:
    """内存关键字过滤：任一值包含关键字（大小写不敏感）"""
    kw = keyword.strip().lower()
    if not kw:
        return rows
    hit: list[dict] = []
    for r in rows:
        if any(kw in str(v).lower() for v in r.values() if v is not None):
            hit.append(r)
    return hit


async def fetch_form_rows_page(
    session: AsyncSession, connection_id: str, form_key: str, *,
    page: int = 1, page_size: int = 100,
    keyword: str | None = None, sort_field: str | None = None,
    order: str = "asc",
) -> dict[str, Any]:
    """明细分页查询（add-bi-detail-query）——明细大表逐行分析的服务端能力

    - DB 源：**分页/排序下推**——复用 fetch_data 的 order_by/limit/offset 参数化原语
      （标识符白名单在驱动内），每页只取 page_size+1 行判断 has_more，不拉全量；
    - HAP 源：全量读行（≤10 万行上限）在 60s 缓存内分页/排序/过滤（毫秒级切片）；
    - keyword：DB 源无法安全下推（安全策略禁止动态 SQL 拼接）→ 降级内存过滤并标记
      fallback="keyword_memory"（P3 聚合下推时统一解决）；HAP 源内存过滤。

    返回 {rows, page, page_size, has_more, total|None, fallback|None, warn}
    """
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), DETAIL_PAGE_MAX)
    offset = (page - 1) * page_size

    conn = await _load_connection(session, connection_id)
    if conn.db_type:
        # keyword 与 desc 排序无法复用 fetch_data 原语（前者需 LIKE、后者驱动只支持升序）
        # → 降级 60s 缓存内的内存处理并标记 fallback；P3 聚合下推时统一解决
        need_memory = bool(keyword) or (str(order).lower() == "desc" and sort_field)
        if need_memory:
            rows_all = await fetch_form_rows(session, connection_id, form_key)
            filtered = _filter_rows_in_memory(rows_all, keyword) if keyword else rows_all
            ordered = _sort_rows_in_memory(filtered, sort_field, order)
            window = ordered[offset:offset + page_size]
            total = len(ordered)
            return {
                "rows": window, "page": page, "page_size": page_size,
                "has_more": offset + page_size < total, "total": total,
                "fallback": "keyword_memory" if keyword else "sort_memory",
                "warn": total > FORM_ROWS_WARN,
            }
        from app.security.crypto import decrypt_db_config
        from app.connectors.database import get_database_connector

        connector = get_database_connector(
            conn.db_type, decrypt_db_config(conn.db_config) or {})
        try:
            rows = await connector.fetch_data(
                table=form_key, order_by=sort_field or None,
                limit=page_size + 1, offset=offset)
        except Exception as e:
            raise SourceError(f"分页查询失败: {type(e).__name__}: {e}") from e
        finally:
            await connector.disconnect()
        has_more = len(rows) > page_size
        return {
            "rows": rows[:page_size], "page": page, "page_size": page_size,
            "has_more": has_more, "total": None, "fallback": None, "warn": False,
        }

    # HAP 源：缓存内分页/排序/过滤
    rows_all = await fetch_form_rows(session, connection_id, form_key)
    filtered = _filter_rows_in_memory(rows_all, keyword) if keyword else rows_all
    ordered = _sort_rows_in_memory(filtered, sort_field, order)
    total = len(ordered)
    return {
        "rows": ordered[offset:offset + page_size], "page": page,
        "page_size": page_size, "has_more": offset + page_size < total,
        "total": total, "fallback": "memory_paging",
        "warn": total > FORM_ROWS_WARN,
    }


async def _load_connection(session: AsyncSession, connection_id: str):
    """加载连接（表单取数共用；非法 UUID / 不存在 → SourceError）"""
    try:
        conn = await session.get(ApiConnection, uuid.UUID(connection_id))
    except ValueError:
        raise SourceError(f"connection_id 不是合法 UUID: {connection_id}")
    if conn is None:
        raise SourceError(f"连接不存在: {connection_id}")
    return conn


# ============================================================
# HAP 增量拉取（add-bi-hap-incremental）
#
# 背景：HAP 源原先每轮全量翻页（10 万行 = 100 次往返），而快照聚合每分钟一轮。
# 实测（scripts/hap_incremental_probe.py，真实端点）证明服务端支持 `filters` +
# `sortId/sortType`，且 `utime > 水位线`（严格大于）可精确圈出「新增 + 变更」的行。
#
# 策略：规模达标（total ≥ HAP_INCREMENTAL_MIN_ROWS）才启用——
#   - 首轮/对账超时/无水位线/字段缺失 → 全量拉（与本变更前行为一致）；
#   - 否则 → `utime > (水位线 − 重叠窗口)` 增量拉，按 rowid **幂等合并**进快照；
#   - 单轮增量过大（服务端 total 超阈值）→ 回退全量。
# 让渡（显式记录）：**删除的行在下一次全量对账（默认 ≤600s）后才从快照消失**；
# HAP 源仍受 MAX_FORM_ROWS=10 万硬闸（P3 规格，合并后的快照同样截断并告警）。
# ============================================================

# 启用阈值：工作表行数超过它才走增量（小表全量本就快，且保持 60s 内可见的现状语义）
HAP_INCREMENTAL_MIN_ROWS = int(os.environ.get(
    "APIBRIDGE_HAP_INCREMENTAL_MIN_ROWS", "20000"))
# 全量对账间隔（秒）：决定"删除最迟何时消失"
HAP_INCREMENTAL_RECONCILE_SECONDS = float(os.environ.get(
    "APIBRIDGE_HAP_INCREMENTAL_RECONCILE_SECONDS", "600"))
# 「与水位线同秒」的边界行刷新上限：刷新是为防同秒内的后续变更漏采，
# 但批量导入常让整表落在同一秒（边界集=整表 → 刷新=全量重拉，增量形同虚设）。
# 边界行数超过它即跳过刷新（不合并、标 boundary_skipped），真增量仍照常合并。
HAP_INCREMENTAL_BOUNDARY_MAX_ROWS = int(os.environ.get(
    "APIBRIDGE_HAP_INCREMENTAL_BOUNDARY_MAX_ROWS", "2000"))
# 单轮增量行数上限：超过即改走全量（大范围变更时全量比多次翻页便宜）
HAP_INCREMENTAL_DELTA_ABS_CAP = int(os.environ.get(
    "APIBRIDGE_HAP_INCREMENTAL_DELTA_ABS_CAP", "5000"))
# 一键关闭（排查/对照：设 APIBRIDGE_HAP_INCREMENTAL=0）
HAP_INCREMENTAL_ENABLED = os.environ.get(
    "APIBRIDGE_HAP_INCREMENTAL", "1").strip().lower() not in ("0", "false", "no", "")

# 快照存储：{f"{connection_id}:{form_key}": {...}}（进程内，单 worker 部署，见 AGENTS §6）
_HAP_SNAPSHOT_MAX = 200          # 上限淘汰：快照含全表行集，数量失控即内存事故
_hap_snapshots: dict[str, dict[str, Any]] = {}
# 最近一次拉取的快照信息（供 API 透出：模式/增量行数/水位线/上次全量时间）
_snapshot_info: dict[str, dict[str, Any]] = {}


def snapshot_info(connection_id: str, form_key: str) -> dict[str, Any] | None:
    """读取最近一次 form 拉取的快照信息（无则 None）"""
    return _snapshot_info.get(f"{connection_id}:{form_key}")


def _rows_incremental_ready(rows: list[dict]) -> tuple[bool, str]:
    """行集合是否具备增量条件：每行都有 rowid 与非空 utime（缺一即退化全量）"""
    if not rows:
        return False, "empty"
    for r in rows:
        if not isinstance(r, dict):
            return False, "invalid_row"
        if not str(r.get("rowid") or "").strip() \
                or not str(r.get("utime") or "").strip():
            return False, "missing_rowid_or_utime"
    return True, "ok"


def _max_utime(rows: list[dict]) -> str | None:
    values = [str(r.get("utime")) for r in rows
              if isinstance(r, dict) and str(r.get("utime") or "").strip()]
    return max(values) if values else None


def _build_snapshot(rows: list[dict], total: int | None) -> dict[str, Any]:
    now = time.monotonic()
    return {
        "rows": {str(r["rowid"]): r for r in rows if isinstance(r, dict)},
        "watermark": _max_utime(rows),
        "total": total if isinstance(total, int) else len(rows),
        "full_at": now,
        "last_pull": now,
        "delta_rows": 0,
    }


def _build_adapter(conn: Any):
    from app.adapters.factory import ProtocolAdapterFactory
    from app.services.adapter_config import build_adapter_config

    return ProtocolAdapterFactory.create(conn.protocol, build_adapter_config(conn))


def _full_pull_info(key: str, form_key: str, reason: str, rows: list[dict],
                    total: int | None) -> dict[str, Any]:
    """全量拉取后的处理：达标则重建快照，否则清掉快照；返回并记录快照信息"""
    ready, why = _rows_incremental_ready(rows)
    keep = (HAP_INCREMENTAL_ENABLED and ready
            and (total or len(rows)) >= HAP_INCREMENTAL_MIN_ROWS)
    if keep:
        if len(_hap_snapshots) >= _HAP_SNAPSHOT_MAX and key not in _hap_snapshots:
            _hap_snapshots.clear()      # 与 _pushdown_cache 同一淘汰策略
        _hap_snapshots[key] = _build_snapshot(rows, total)
    else:
        _hap_snapshots.pop(key, None)
    if not keep and reason == "bootstrap":
        # 首轮就判定不达标：区分"字段不具备"（degrade）与"表太小"（below_threshold）
        reason = f"degrade:{why}" if not ready else "below_threshold"
    info = {
        "mode": "full",
        "reason": reason,
        "delta_rows": len(rows), "total_rows": len(rows),
        "boundary_rows": 0, "boundary_skipped": False,
        "watermark": _max_utime(rows), "full_at_ago_s": 0,
        "enabled": HAP_INCREMENTAL_ENABLED,
    }
    _snapshot_info[key] = info
    logger.info("HAP 拉行: form=%s mode=full reason=%s rows=%d",
                form_key, info["reason"], len(rows))
    return info


async def _fetch_hap_rows(conn: Any, form_key: str) -> list[dict]:
    """HAP 读行：规模达标且开启增量时走「增量 + 定期全量对账」，否则全量

    返回**当前完整行集合**（调用方语义不变：永远拿到"当前状态"的全量行）。
    """
    key = f"{conn.id}:{form_key}"
    snap = _hap_snapshots.get(key)
    now = time.monotonic()
    adapter = _build_adapter(conn)

    need_full = (
        not HAP_INCREMENTAL_ENABLED
        or snap is None
        or not snap.get("watermark")
        or now - float(snap.get("full_at") or 0) > HAP_INCREMENTAL_RECONCILE_SECONDS
    )
    if need_full:
        reason = ("disabled" if not HAP_INCREMENTAL_ENABLED
                  else "bootstrap" if snap is None
                  else "no_watermark" if not snap.get("watermark")
                  else "reconcile_due")
        rows, total = await adapter.get_worksheet_rows(form_key, return_total=True)
        _full_pull_info(key, form_key, reason, rows, total)
        return rows

    # ---- 增量：严格大于取真增量 →（可选）刷新水位线同秒的边界行 ----
    try:
        delta, total = await adapter.get_rows_since(form_key, snap["watermark"])
    except Exception as e:                                    # noqa: BLE001
        logger.warning("HAP 增量拉取失败（本轮改全量）: form=%s %s: %s",
                       form_key, type(e).__name__, e)
        rows, total = await adapter.get_worksheet_rows(form_key, return_total=True)
        _full_pull_info(key, form_key, f"incremental_failed:{type(e).__name__}",
                        rows, total)
        return rows

    rows_count = len(snap["rows"])
    delta_total = total if isinstance(total, int) else len(delta)
    if delta_total > max(rows_count // 2, HAP_INCREMENTAL_DELTA_ABS_CAP):
        logger.info("HAP 增量过大（%d 行 vs 快照 %d 行）→ 本轮改全量: form=%s",
                    delta_total, rows_count, form_key)
        rows, total = await adapter.get_worksheet_rows(form_key, return_total=True)
        _full_pull_info(key, form_key, "delta_too_large", rows, total)
        return rows

    # 边界刷新：取「utime >= 水位线」再**减去刚拿到的真增量**，剩下的就是
    # 「与水位线同秒」的边界行（服务端同字段多条件是 OR，做不出 =W 的精确过滤；
    # 客户端差集等价，代价只是多一趟请求）；边界集超上限即跳过并标注
    boundary: list[dict] = []
    boundary_skipped = False
    try:
        ge_rows, _ge_total = await adapter.get_rows_since(
            form_key, snap["watermark"], inclusive=True)
        delta_ids = {str((r or {}).get("rowid") or "") for r in delta}
        boundary = [r for r in ge_rows
                    if str((r or {}).get("rowid") or "") not in delta_ids]
        if len(boundary) > HAP_INCREMENTAL_BOUNDARY_MAX_ROWS:
            boundary_skipped = True
            logger.info("HAP 边界集过大（%d 行）→ 跳过边界刷新: form=%s",
                        len(boundary), form_key)
            boundary = []
    except Exception as e:                                    # noqa: BLE001
        # 边界刷新失败不影响真增量：本轮跳过，下一轮重试
        boundary_skipped = True
        boundary = []
        logger.warning("HAP 边界刷新失败（本轮跳过）: form=%s %s: %s",
                       form_key, type(e).__name__, e)

    merged_rows = snap["rows"]
    for r in list(delta) + list(boundary):
        row_id = str((r or {}).get("rowid") or "").strip()
        if row_id:
            merged_rows[row_id] = r          # 幂等 upsert（边界行重复回传无害）
    new_wm = _max_utime(list(delta) + list(boundary))
    if new_wm and new_wm > str(snap.get("watermark") or ""):
        snap["watermark"] = new_wm
    snap["last_pull"] = now
    snap["delta_rows"] = len(delta) + len(boundary)
    snap["total"] = max(int(snap.get("total") or 0), len(merged_rows))
    _snapshot_info[key] = {
        "mode": "incremental", "reason": None,
        # delta_rows = 严格大于水位线的真增量；boundary_rows = 同秒边界刷新行数
        # （边界刷新为防同秒漏采，超上限即跳过并在 boundary_skipped 标注）
        "delta_rows": len(delta), "boundary_rows": len(boundary),
        "boundary_skipped": boundary_skipped,
        "total_rows": len(merged_rows),
        "watermark": snap["watermark"],
        "full_at_ago_s": int(now - float(snap.get("full_at") or now)),
        "enabled": HAP_INCREMENTAL_ENABLED,
    }
    logger.info("HAP 拉行: form=%s mode=incremental delta=%d boundary=%d%s total=%d wm=%s",
                form_key, len(delta), len(boundary),
                "（已跳过）" if boundary_skipped else "",
                len(merged_rows), snap["watermark"])
    return list(merged_rows.values())


# ============================================================
# 聚合下推判定与取数（add-bi-sql-pushdown）
#
# 边界：只把「定义级筛选 → WHERE + 维度 → GROUP BY + 可下推算子 → 聚合函数」
# 下发；Top N/「其他」归并、ratio 除法、维度值归一仍在 Python 侧（bi_aggregator），
# 保证与内存路径逐项一致。执行点收敛在 app/connectors/grouped_pushdown.py。
# ============================================================

# 可下推的聚合算子（p50/p95/p99 分位口径不可跨库保证 → 内存）
PUSHABLE_AGGS = frozenset({"count", "count_distinct", "sum", "avg", "min", "max"})
# ratio 的分子/分母分量算子（除法仍在 Python 侧）
PUSHABLE_RATIO_AGGS = frozenset({"count", "sum", "avg", "min", "max"})
# 需要数值输入的算子：文本列在源库会被隐式转 0/字面序，与内存"仅数值"语义不同
NUMERIC_MEASURE_AGGS = frozenset({"sum", "avg", "min", "max"})
NUMERIC_FIELD_TYPES = frozenset({"number", "duration"})
# 可下推的过滤算子（contains/not_contains/empty/not_empty 无等价 SQL 语义）
PUSHABLE_FILTER_OPS = frozenset({
    "eq", "ne", "in", "not_in", "between", "not_between", "gt", "ge", "lt", "le",
})
# 区间类比较：文本比较受源库 collation 影响（大小写/重音敏感度），与 Python 码点序不等价
RANGE_FILTER_OPS = frozenset({"between", "not_between", "gt", "ge", "lt", "le"})

# 判定与失败标记缓存 TTL（与行缓存同寿命：定义改错/源库恢复了 60s 内自愈）
PUSHDOWN_DECISION_TTL_SECONDS = FORM_CACHE_TTL_SECONDS
# 缓存条目上限（键 = 定义指纹 × db_type，量级小；超限直接清空重建）
_PUSHDOWN_CACHE_MAX = 1000

# value = (记录时刻, 是否可下推, 原因)；reason 用于日志（不重复刷屏）
_pushdown_cache: dict[str, tuple[float, bool, str]] = {}


def can_pushdown(defn: dict[str, Any], conn: Any) -> tuple[bool, str]:
    """可下推判定（纯函数，无 IO）：返回 (是否可下推, 原因)

    回退清单（design.md §3）：非 form 源 / 非 DB 连接 / 驱动无能力标志（Oracle 默认关）、
    派生计算字段、resolver·normalize·groups 维度、非 ASCII 标识符、分位算子、
    度量级 filter（条件聚合 = CASE WHEN，本期不做）、非数值字段的数值算子、
    文本模糊过滤、非数值字段的区间比较。
    """
    if not isinstance(defn, dict):
        return False, "invalid_defn"
    if defn.get("source_type") != "form":
        return False, f"source_type:{defn.get('source_type')}"
    db_type = getattr(conn, "db_type", None)
    if not db_type:
        return False, "source_not_db"

    from app.connectors.database import is_safe_identifier, supports_group_pushdown

    if not supports_group_pushdown(str(db_type)):
        return False, f"driver:{db_type}"
    if defn.get("computed"):
        return False, "computed_fields"
    if not (defn.get("dimensions") or []) and not (defn.get("measures") or []):
        # 无维度且无度量 = 没有可聚合的内容（detail 卡就是这种形态：它的数据来自
        # 明细查询路径）——下推会生成 "SELECT FROM t" 的非法 SQL（实测每轮刷警告日志）
        return False, "empty_aggregation"

    for d in defn.get("dimensions") or []:
        if not isinstance(d, dict):
            return False, "invalid_dimension"
        if d.get("resolver"):
            return False, "dimension_resolver"
        if d.get("normalize"):
            return False, "dimension_normalize"
        if d.get("groups"):
            return False, "dimension_groups"
        field = d.get("field")
        if not field:
            return False, "dimension_field_missing"
        if not is_safe_identifier(field):
            return False, f"identifier:{field}"

    for m in defn.get("measures") or []:
        if not isinstance(m, dict):
            return False, "invalid_measure"
        if m.get("filter"):
            return False, "measure_filter"
        agg = m.get("agg")
        if agg == "ratio":
            for side in ("numerator", "denominator"):
                comp = m.get(side) if isinstance(m.get(side), dict) else {}
                cagg = comp.get("agg", "sum")
                if cagg not in PUSHABLE_RATIO_AGGS:
                    return False, f"ratio_agg:{cagg}"
                cfield = comp.get("field")
                if cagg == "count":
                    continue
                if not cfield:
                    return False, "ratio_field_missing"
                if not is_safe_identifier(cfield):
                    return False, f"identifier:{cfield}"
            continue
        if agg not in PUSHABLE_AGGS:
            return False, f"agg:{agg}"
        field = m.get("field")
        if agg == "count":
            continue
        if not field:
            return False, "measure_field_missing"
        if not is_safe_identifier(field):
            return False, f"identifier:{field}"
        if agg in NUMERIC_MEASURE_AGGS \
                and str(m.get("field_type") or "number") not in NUMERIC_FIELD_TYPES:
            return False, f"measure_field_type:{m.get('field_type')}"

    filters = defn.get("filters")
    if filters:
        if isinstance(filters, dict):
            # 旧形态：{字段: 期望值} 等值（NULL 行内存也不匹配 → 无补偿需求）
            for k, v in filters.items():
                if not is_safe_identifier(k):
                    return False, f"identifier:{k}"
                if v is None:
                    return False, "filter_null_value"
        elif isinstance(filters, list):
            for r in filters:
                if not isinstance(r, dict) or not r.get("field"):
                    return False, "invalid_filter"
                if not is_safe_identifier(r["field"]):
                    return False, f"identifier:{r['field']}"
                op = str(r.get("op") or "eq")
                if op not in PUSHABLE_FILTER_OPS:
                    return False, f"filter_op:{op}"
                if r.get("value") is None:
                    return False, "filter_null_value"
                if op in ("in", "not_in"):
                    values = r["value"] if isinstance(r["value"], (list, tuple)) \
                        else [r["value"]]
                    if not values or any(v is None for v in values):
                        return False, "filter_null_value"
                if op in RANGE_FILTER_OPS:
                    if str(r.get("field_type") or "") not in NUMERIC_FIELD_TYPES:
                        return False, f"filter_field_type:{r.get('field_type')}"
                    if op in ("between", "not_between") and (
                            not isinstance(r["value"], (list, tuple))
                            or len(r["value"]) != 2):
                        return False, "filter_value_shape"
        else:
            return False, "invalid_filters"

    # 列名命中脱敏提示词（phone/email/token/app_key…）→ 下推聚合会把原始维度值
    # 或未脱敏口径直接带出，绕过 desensitize（P0-4）；内存路径同样不读这类列的原文。
    sensitive = _sensitive_col(_pushdown_columns(defn))
    if sensitive:
        return False, f"sensitive_column:{sensitive}"
    return True, "ok"


def pushdown_group_fields(defn: dict[str, Any]) -> list[str]:
    """下推分组列（can_pushdown 已保证非空且过白名单）"""
    return [str(d.get("field")) for d in (defn.get("dimensions") or [])
            if isinstance(d, dict) and d.get("field")]


def _sensitive_col(columns: set[str]) -> str | None:
    """列名命中脱敏提示词（归一化匹配，与 desensitize 同一判定）→ 返回首个命中列"""
    from app.security.desensitize import key_hits_sensitive
    for col in sorted(columns):
        if key_hits_sensitive(col):
            return col
    return None


def _pushdown_columns(defn: dict[str, Any]) -> set[str]:
    """收集下推 SQL 会触达的全部列名（分组/度量/比率分量/过滤）"""
    from app.connectors.database import is_safe_identifier
    columns: set[str] = set()
    for d in defn.get("dimensions") or []:
        if isinstance(d, dict) and d.get("field") and is_safe_identifier(str(d["field"])):
            columns.add(str(d["field"]))
    for m in defn.get("measures") or []:
        if not isinstance(m, dict):
            continue
        if m.get("agg") == "ratio":
            for side in ("numerator", "denominator"):
                comp = m.get(side) if isinstance(m.get(side), dict) else {}
                if comp.get("field") and is_safe_identifier(str(comp["field"])):
                    columns.add(str(comp["field"]))
        elif m.get("field") and is_safe_identifier(str(m["field"])):
            columns.add(str(m["field"]))
    for r in pushdown_filters(defn) or []:
        if r.get("field") and is_safe_identifier(str(r["field"])):
            columns.add(str(r["field"]))
    return columns


def pushdown_aggs(defn: dict[str, Any]) -> tuple[list[dict], dict[str, dict[str, str]]]:
    """构造下推聚合列与 ratio 逻辑键映射

    返回 (aggs, ratio_keys)：
    - aggs：`[{"key","func","field"}]`，直接交给构造器（别名 a0..an 由构造器分配）
    - ratio_keys：`{度量 key: {"numerator": SQL 侧逻辑键, "denominator": …}}`——
      ratio 拆成两列聚合，除法与 scale 仍由内存侧（entries_from_grouped）完成；
      列键用位置序号生成（不掺用户 key，杜绝与普通度量 key 撞键）。
    """
    aggs: list[dict] = []
    ratio_keys: dict[str, dict[str, str]] = {}
    for i, m in enumerate(defn.get("measures") or []):
        if not isinstance(m, dict):
            continue
        key = str(m.get("key") or f"m{i}")
        if m.get("agg") == "ratio":
            num = m.get("numerator") if isinstance(m.get("numerator"), dict) else {}
            den = m.get("denominator") if isinstance(m.get("denominator"), dict) else {}
            num_key, den_key = f"__ratio{i}__num", f"__ratio{i}__den"
            aggs.append({"key": num_key, "func": num.get("agg", "sum"),
                         "field": num.get("field")})
            aggs.append({"key": den_key, "func": den.get("agg", "sum"),
                         "field": den.get("field")})
            ratio_keys[key] = {"numerator": num_key, "denominator": den_key}
        else:
            aggs.append({"key": key, "func": m.get("agg"), "field": m.get("field")})
    return aggs, ratio_keys


def pushdown_filters(defn: dict[str, Any]) -> list[dict]:
    """构造下推过滤规则（含 NULL 行等价标记）

    null_match：SQL 三值逻辑把 NULL 行判为不匹配，而内存匹配器对 ne/not_in/
    not_between（以及空串参与比较的 lt/le）会返回 True——这里用**同一份内存
    匹配器**对 NULL 行求值，为真时让构造器补 `OR IS NULL`，两条路径逐行等价。
    """
    filters = defn.get("filters")
    rules: list[dict] = []
    if isinstance(filters, dict):
        rules = [{"field": str(k), "op": "eq", "value": v}
                 for k, v in filters.items()]
    elif isinstance(filters, list):
        rules = [{"field": r.get("field"), "op": str(r.get("op") or "eq"),
                  "value": r.get("value")}
                 for r in filters if isinstance(r, dict) and r.get("field")]
    if not rules:
        return []
    from app.services.bi_aggregator import _rule_match   # 延迟导入：避免模块环
    for r in rules:
        r["null_match"] = bool(_rule_match({}, r))
    return rules


def _pushdown_cache_key(defn: dict[str, Any], db_type: str) -> str:
    """判定缓存键：只纳入影响下推结论的定义片段（定义改动即失效）"""
    payload = json.dumps({
        "src": defn.get("source_ref"),
        "db": db_type,
        "dims": defn.get("dimensions"),
        "meas": defn.get("measures"),
        "filters": defn.get("filters"),
        "computed": defn.get("computed"),
    }, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


async def try_pushdown_rows(
    session: AsyncSession, defn: dict[str, Any]
) -> list[dict] | None:
    """尝试聚合下推：返回分组行（逻辑键），不可下推/失败 → None（调用方回退内存）

    - 判定与失败标记进程内缓存（TTL 60s）——同一指标不每窗重试；
    - 执行点收敛在 app/connectors/grouped_pushdown.py（单一入口，能力标志可关）；
    - 任何异常都不上抛：记原因 + 标记 + 回退内存路径（聚合不中断），
      连接不存在 / source_ref 非法等源定义问题也不例外——错误语义与改造前一致，
      仍由内存路径抛出同样的 SourceError。
    """
    if not isinstance(defn, dict) or defn.get("source_type") != "form":
        return None
    try:
        return await _try_pushdown_rows_inner(session, defn)
    except SourceError as e:
        logger.info("聚合下推跳过（回退内存路径，错误交原路径抛出）: %s %s",
                    defn.get("metric_key"), e)
        return None


async def _unreadable_pushdown_column(session: AsyncSession, conn: Any,
                                      form_key: str,
                                      defn: dict[str, Any]) -> str | None:
    """字段字典把下推会触达的列标为不可读 → 返回首个命中列（P0-4）

    字典是可选的：scope 下没有登记时一律放行（与 enrich_fields 的缺省 readable=True
    同口径）；字典本身读不出来按 fail-closed 处理——只损失下推性能，不放开检查。
    """
    from app.services import field_policy
    try:
        defs = await field_policy.definitions(session, conn, form_key)
    except Exception as e:
        logger.warning("字段字典读取失败（聚合下推按不可用处理）: %s", type(e).__name__)
        return "<field_policy_error>"
    if not defs:
        return None
    for col in sorted(_pushdown_columns(defn)):
        d = field_policy.lookup(defs, conn, col)
        if d is not None and not d.readable:
            return col
    return None


async def _try_pushdown_rows_inner(
    session: AsyncSession, defn: dict[str, Any]
) -> list[dict] | None:
    connection_id, form_key = parse_form_ref(defn)     # 非法 ref → SourceError
    conn = await _load_connection(session, connection_id)
    if not getattr(conn, "db_type", None):
        return None                                    # HAP 源：内存路径

    key = _pushdown_cache_key(defn, str(conn.db_type))
    now = time.monotonic()
    hit = _pushdown_cache.get(key)
    if hit is not None and now - hit[0] < PUSHDOWN_DECISION_TTL_SECONDS:
        if not hit[1]:
            return None
    else:
        ok, reason = can_pushdown(defn, conn)
        if len(_pushdown_cache) >= _PUSHDOWN_CACHE_MAX:
            _pushdown_cache.clear()
        _pushdown_cache[key] = (now, ok, reason)
        if not ok:
            logger.info("聚合下推不可用（回退内存路径）: %s ← %s",
                        defn.get("metric_key"), reason)
            return None

    # 字段字典禁读列检查（有 IO，不进 can_pushdown 纯函数；结果同样进判定缓存）
    unreadable = await _unreadable_pushdown_column(session, conn, form_key, defn)
    if unreadable:
        if len(_pushdown_cache) >= _PUSHDOWN_CACHE_MAX:
            _pushdown_cache.clear()
        _pushdown_cache[key] = (time.monotonic(), False,
                                f"unreadable_column:{unreadable}")
        logger.info("聚合下推不可用（字段字典禁读，回退内存路径）: %s ← %s",
                    defn.get("metric_key"), unreadable)
        return None

    from app.connectors.database import get_database_connector
    from app.connectors.grouped_pushdown import fetch_grouped_rows
    from app.security.crypto import decrypt_db_config

    aggs, _ = pushdown_aggs(defn)
    connector = get_database_connector(
        conn.db_type, decrypt_db_config(conn.db_config) or {})
    try:
        rows = await fetch_grouped_rows(
            connector, table=form_key,
            group_fields=pushdown_group_fields(defn),
            aggs=aggs, filters=pushdown_filters(defn) or None)
    except Exception as e:      # 含 PushdownTooLarge/源库报错：一律回退，不中断聚合
        logger.warning("聚合下推执行失败（回退内存路径）: %s %s: %s",
                       defn.get("metric_key"), type(e).__name__, e)
        _pushdown_cache[key] = (time.monotonic(), False,
                                f"pushdown_failed:{type(e).__name__}")
        return None
    finally:
        await connector.disconnect()
    return rows


async def _fetch_form_rows_uncached(session: AsyncSession,
                                    connection_id: str,
                                    form_key: str) -> list[dict]:
    try:
        conn = await session.get(ApiConnection, uuid.UUID(connection_id))
    except ValueError:
        raise SourceError(f"connection_id 不是合法 UUID: {connection_id}")
    if conn is None:
        raise SourceError(f"连接不存在: {connection_id}")

    cap = min(MAX_FORM_ROWS, settings.bi_max_rows)
    try:
        async with asyncio.timeout(settings.bi_query_timeout_seconds):
            if conn.db_type:
                from app.connectors.database import get_database_connector

                # 分页取数：批大小对齐 FORM_FETCH_BATCH（默认 1000 会让百万行表产生千次
                # 深翻页往返，OFFSET 成本随表二次增长），并按 cap+1 早停——超限表不整表
                # 读进内存，读满即报错（不把部分数据当完整结果）
                connector = get_database_connector(conn.db_type, conn.db_config or {})
                rows: list[dict] = []
                try:
                    while len(rows) <= cap:
                        want = min(FORM_FETCH_BATCH, cap + 1 - len(rows))
                        page = await connector.fetch_data(
                            table=form_key, offset=len(rows), limit=want)
                        rows.extend(page)
                        if len(page) < want:            # 已到表尾
                            break
                finally:
                    await connector.disconnect()
            elif conn.protocol == "hap":
                rows = await _fetch_hap_rows(conn, form_key)   # 增量/全量策略见下方专节
            else:
                raise SourceError(
                    f"连接「{conn.name}」的协议 {conn.protocol} 不支持表单读行")
            if len(rows) > cap:
                raise SourceError("表单超过 BI 读取上限，请缩小数据范围")
            # 行键归一到字段字典 path（HAP = alias||controlId；DB = 列名，大小写互认）
            # 并脱敏；不缓存凭证。
            from app.api.connections import get_connection_fields
            meta = await get_connection_fields(
                conn_id=conn.id, table=form_key if conn.db_type else None,
                worksheet_id=form_key if conn.protocol == "hap" else None, session=session)
            fields = [f for f in meta.get("fields") or [] if f.get("readable", True)]
            output = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                values = {}
                for f in fields:
                    keys = [f["path"], f.get("field_id"), f.get("alias")]
                    for key in keys:
                        if key in row:
                            values[f["path"]] = row[key]
                            break
                        if conn.db_type and key:
                            match = next((k for k in row if k.lower() == key.lower()), None)
                            if match is not None:
                                values[f["path"]] = row[match]
                                break
                output.append(desensitize_record(values))
            return output
    except SourceError:
        raise
    except Exception as e:
        # 上游失败（鉴权/网络/表不存在/超时）带原因记日志；对外只给可操作提示——
        # 驱动异常原文可能携带凭证、DSN 和响应体，不直接返回给客户端
        logger.warning("BI 表单读取失败（%s/%s）: %s: %s",
                       getattr(conn, "name", connection_id), form_key,
                       type(e).__name__, e)
        raise SourceError("BI 表单读取失败，请检查连接、字段定义及查询范围") from None


# ============================================================
# 窗口扫描
# ============================================================

async def scan_window(
    session: AsyncSession,
    defn: dict[str, Any],
    start: datetime,
    end: datetime,
    shard_threshold: int = DEFAULT_SHARD_THRESHOLD,
) -> list[dict[str, Any]]:
    """扫描 [start, end) 窗口内的原始行（按数据源类型分发）

    shard_threshold 控制单次拉取的行数（游标分片），保证超大窗口不会一次性入内存。
    """
    batch_size = max(int(shard_threshold), 1)
    source_type = defn.get("source_type")
    if source_type == "form":
        return await _scan_form(session, defn)
    if source_type == "data_buffer":
        return await _scan_data_buffer(session, start, end, batch_size)
    if source_type == "execution_logs":
        return await _scan_execution_logs(session, start, end, batch_size)
    if source_type == "custom_sql":
        return await _scan_custom_sql(session, defn, start, end)
    raise SourceError(f"不支持的数据源类型: {source_type}")


async def _scan_form(
    session: AsyncSession, defn: dict[str, Any],
) -> list[dict[str, Any]]:
    """扫描连接表单（快照语义：每次取当前全量行，时间窗参数不参与过滤）

    行统一为 {"data": 原始行, "fetched_at": 拉取时刻}——聚合侧 get_field
    顶层未命中即进 data 取值；窗口归属由调用方（快照语义只算最新完整窗）决定。
    """
    connection_id, form_key = parse_form_ref(defn)
    rows = await fetch_form_rows(session, connection_id, form_key)
    fetched_at = datetime.now(timezone.utc)
    return [{"data": r, "fetched_at": fetched_at} for r in rows]


async def _scan_data_buffer(
    session: AsyncSession, start: datetime, end: datetime,
    batch_size: int = SHARD_BATCH,
) -> list[dict[str, Any]]:
    """扫描中间数据（data_buffer）

    输出统一形状：{"id","created_at","status","task_id","data"}
    `data` 优先取 transformed_data（转换后），为空回退 source_data。
    """
    out: list[dict[str, Any]] = []
    last_id = None
    while True:
        stmt = select(
            DataBuffer.id,
            DataBuffer.created_at,
            DataBuffer.status,
            DataBuffer.task_id,
            DataBuffer.transformed_data,
            DataBuffer.source_data,
        ).where(DataBuffer.created_at >= start, DataBuffer.created_at < end)
        if last_id is not None:
            stmt = stmt.where(DataBuffer.id > last_id)
        # 以主键全序做游标分片：确定性枚举，无重复无遗漏
        stmt = stmt.order_by(DataBuffer.id).limit(batch_size)
        batch = (await session.execute(stmt)).all()
        if not batch:
            break
        for row in batch:
            payload = row[4] if row[4] is not None else row[5]
            for record in decode_buffer_records(payload):
                out.append({"id": row[0], "created_at": row[1], "status": row[2],
                            "task_id": row[3], "data": desensitize_record(record)})
                if len(out) > settings.bi_max_rows:
                    raise SourceError("BI 窗口超过读取上限")
        if len(out) > settings.bi_max_rows:
            raise SourceError("BI 窗口超过读取上限")
        last_id = batch[-1][0]
        if len(batch) < batch_size:
            break
    return out


async def _scan_execution_logs(
    session: AsyncSession, start: datetime, end: datetime,
    batch_size: int = SHARD_BATCH,
) -> list[dict[str, Any]]:
    """扫描执行元数据（execution_logs）"""
    out: list[dict[str, Any]] = []
    last_id = None
    while True:
        stmt = select(
            ExecutionLog.id,
            ExecutionLog.created_at,
            ExecutionLog.task_id,
            ExecutionLog.action,
            ExecutionLog.status,
            ExecutionLog.duration_ms,
            ExecutionLog.error_message,
        ).where(ExecutionLog.created_at >= start, ExecutionLog.created_at < end)
        if last_id is not None:
            stmt = stmt.where(ExecutionLog.id > last_id)
        stmt = stmt.order_by(ExecutionLog.id).limit(batch_size)
        batch = (await session.execute(stmt)).all()
        if not batch:
            break
        for row in batch:
            out.append({
                "id": row[0],
                "created_at": row[1],
                "task_id": row[2],
                "action": row[3],
                "status": row[4],
                "duration_ms": row[5],
                "error_message": row[6],
            })
        if len(out) > settings.bi_max_rows:
            raise SourceError("BI 窗口超过读取上限")
        last_id = batch[-1][0]
        if len(batch) < batch_size:
            break
    return out


async def _scan_custom_sql(session, defn, start, end):
    """Use an independent read-only connection, never the API write transaction."""
    sql = defn.get("source_ref") or ""
    if not is_read_only_sql(sql):
        raise SourceError("custom_sql 必须为单条只读查询")
    async with session.bind.connect() as conn:
        dialect = conn.dialect.name
        driver = None
        try:
            async with asyncio.timeout(settings.bi_query_timeout_seconds):
                if dialect == "sqlite":
                    await conn.execute(text("PRAGMA query_only = ON"))
                    raw = await conn.get_raw_connection()
                    driver = raw.driver_connection
                    deadline = time.monotonic() + settings.bi_query_timeout_seconds
                    await driver.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                elif dialect == "postgresql":
                    await conn.execute(text("SET TRANSACTION READ ONLY"))
                    await conn.execute(text(f"SET LOCAL statement_timeout = {int(settings.bi_query_timeout_seconds * 1000)}"))
                else:
                    raise SourceError("自定义 SQL 仅支持 SQLite/PostgreSQL")
                stream = await conn.stream(text(sql), {"start": start, "end": end})
                try:
                    rows = await stream.mappings().fetchmany(settings.bi_max_rows + 1)
                    if len(rows) > settings.bi_max_rows:
                        raise SourceError("SQL 结果超过 BI 读取上限")
                    return [desensitize_record(dict(row)) for row in rows]
                finally:
                    await stream.close()
        except SourceError:
            raise
        except Exception:
            raise SourceError("BI 只读 SQL 执行失败或超时") from None
        finally:
            await conn.rollback()
            if dialect == "sqlite":
                if driver:
                    await driver.set_progress_handler(None, 0)
                await conn.execute(text("PRAGMA query_only = OFF"))
                await conn.rollback()


def decode_buffer_records(payload):
    """Decode current encrypted batches without mutating persisted snapshots."""
    if isinstance(payload, dict) and payload.get("hap_preparing"):
        return []
    if isinstance(payload, dict) and "encrypted" in payload:
        from app.services.incremental import unseal
        try:
            payload = unseal(payload["encrypted"])
        except Exception:
            raise SourceError("BI 无法解密缓冲批次，请检查加密密钥") from None
    if payload is None:
        return []
    if isinstance(payload, dict):
        for key in ("records", "rows", "items", "data", "list", "results"):
            if isinstance(payload.get(key), list):
                return decode_buffer_records(payload[key])
        return [payload]
    if isinstance(payload, list) and all(isinstance(row, dict) for row in payload):
        return payload
    raise SourceError("BI 缓冲批次不是记录对象或数组")


# ============================================================
# 维度解析器（跨表维度，内存字典，避免 SQL join）
# ============================================================

async def load_resolvers(
    session: AsyncSession, defn: dict[str, Any]
) -> dict[str, dict[str, dict[str, Any]]]:
    """按定义中出现的 resolver 名称预加载映射表"""
    names = {
        d.get("resolver")
        for d in (defn.get("dimensions") or [])
        if isinstance(d, dict) and d.get("resolver")
    }
    out: dict[str, dict[str, dict[str, Any]]] = {}
    if "task_connection" in names:
        out["task_connection"] = await _resolve_task_connection(session)
    return out


async def _resolve_task_connection(
    session: AsyncSession,
) -> dict[str, dict[str, Any]]:
    """task_id → {"source_name","target_name","pair"}

    经 sync_tasks → field_mappings → api_connections(源/目标) 关联推导。
    这些表数量级小，一次性预加载为内存字典。
    """
    src = aliased(ApiConnection)
    tgt = aliased(ApiConnection)
    stmt = (
        select(SyncTask.id, src.name, tgt.name)
        .join(FieldMapping, SyncTask.mapping_id == FieldMapping.id)
        .join(src, FieldMapping.source_connection_id == src.id)
        .join(tgt, FieldMapping.target_connection_id == tgt.id)
    )
    rows = (await session.execute(stmt)).all()
    out: dict[str, dict[str, Any]] = {}
    for task_id, src_name, tgt_name in rows:
        src_name = src_name or "未知源"
        tgt_name = tgt_name or "未知目标"
        out[str(task_id)] = {
            "source_name": src_name,
            "target_name": tgt_name,
            "pair": f"{src_name} → {tgt_name}",
        }
    return out
