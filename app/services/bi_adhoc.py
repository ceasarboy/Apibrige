"""BI 即席分析（add-bi-adhoc-analysis）：任意维度组合即时聚合

分层（design.md §2）：
- `build_adhoc_defn`：请求 → **标准化定义**（与看板卡片同一形状，可直接保存）；
- `query_adhoc_defn`：定义校验（与保存端点同一套规则）→ 取值（下推优先 / 内存回退，
  引擎与回退原因如实透出）→ 排序 → 单桶包装 → `convert`（与 `/bi/metrics` 输出同构）；
- **不落库**：即席查询不写任何表，只有显式"另存为卡片"才落到指标定义。

复用而非重写是本模块的原则：校验 `bi_defs.validate_metric_def`、取值
`bi_sources.try_pushdown_rows`（P3 单一执行点）、内存聚合 `bi_aggregator.aggregate_rows`、
格式 `bi_format.convert`——即席与看板两条路径的结果口径由构造保证一致。

计算字段引用（`[中文显示名]` → 字段键）的归一化在 API 层完成（与定义保存端点共用
`_normalize_calc_refs`，需要表单元数据），故本模块的入口要求传入**已归一**的定义。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.bi_aggregator import aggregate_rows, entries_from_grouped
from app.services.bi_defs import validate_metric_def
from app.services.bi_format import FORMATS, convert
from app.services import bi_sources
from app.services.bi_sources import (
    _load_connection,
    can_pushdown,
    parse_form_ref,
    scan_window,
    try_pushdown_rows,
)

logger = logging.getLogger(__name__)

# 图表类型 → 缺省输出格式（未显式传 format 时使用；与前端 FORMAT_BY_CHART 对齐）
FORMAT_BY_CHART: dict[str, str] = {
    "line": "echarts-line", "bar": "echarts-bar", "pie": "echarts-pie",
    "heatmap": "echarts-heatmap", "table": "table", "number": "datav-number",
    "detail": "bi-detail", "scatter": "echarts-scatter", "radar": "echarts-radar",
    "sankey": "echarts-sankey", "graph": "echarts-graph", "flow": "echarts-flow",
    "tree": "echarts-tree", "funnel": "echarts-funnel", "gauge": "echarts-gauge",
    "time_line": "echarts-line", "bar3d": "echarts-bar3d",
}

# 即席聚合的粒度（form 源为快照语义，粒度只影响桶时间戳，无业务含义）
ADHOC_GRANULARITY = "1min"


class AdhocError(Exception):
    """即席请求不合法（问题列表结构与定义保存端点一致）"""

    def __init__(self, issues: list[dict[str, Any]]):
        self.issues = issues
        super().__init__("; ".join(str(i.get("message")) for i in issues[:3]))


def default_format(chart_type: str) -> str:
    """图表类型 → 缺省格式（未知类型回落 raw，由 convert 再校验）"""
    return FORMAT_BY_CHART.get(str(chart_type or ""), "raw")


def build_adhoc_defn(payload: dict[str, Any]) -> dict[str, Any]:
    """请求 → 标准化定义（可直接作为 `POST /bi/metrics` 的请求体）

    - `source_ref` 由 connection_id + form_key 合成（与看板卡片同一 JSON 形状）；
    - `granularity` 固定 1min（见上）；
    - `sort` 不写进定义（看板卡片无排序字段）：仅即席查询内生效，另存时由调用方丢弃。
    """
    connection_id = str(payload.get("connection_id") or "").strip()
    form_key = str(payload.get("form_key") or "").strip()
    top_n = payload.get("top_n")
    return {
        "metric_key": str(payload.get("metric_key") or "adhoc"),
        "name": str(payload.get("name") or "即席分析"),
        "source_type": "form",
        "source_ref": json.dumps(
            {"connection_id": connection_id, "form_key": form_key},
            ensure_ascii=False),
        "time_field": payload.get("time_field") or "created_at",
        "granularity": ADHOC_GRANULARITY,
        "dimensions": payload.get("dimensions") or [],
        "measures": payload.get("measures") or [],
        "filters": payload.get("filters"),
        "computed": payload.get("computed") or [],
        "chart_type": str(payload.get("chart_type") or "bar"),
        "top_n": int(top_n) if isinstance(top_n, (int, float)) else 100,
        "display": {},
        "style": payload.get("style") or {},
    }


def validate_adhoc_defn(defn: dict[str, Any],
                        ref_names: set[str] | None = None) -> None:
    """校验即席定义：error 级问题汇总为 AdhocError

    与定义保存端点共用 `validate_metric_def`——避免"分析页能出的图，另存后校验失败"。
    """
    try:
        ref = json.loads(defn.get("source_ref") or "{}")
    except json.JSONDecodeError:
        ref = {}
    if not str((ref or {}).get("connection_id") or "").strip() \
            or not str((ref or {}).get("form_key") or "").strip():
        raise AdhocError([{
            "level": "error", "code": "invalid_source_ref",
            "message": "数据源不完整：需要 connection_id 与 form_key",
        }])
    errors = [i for i in validate_metric_def(defn, ref_names)
              if i.get("level") == "error"]
    if errors:
        raise AdhocError(errors)


def _validate_sort(defn: dict[str, Any], sort: Any) -> None:
    """排序字段校验：`by` 必须是本定义的度量 key 或维度名（否则 400，不静默忽略）"""
    if not isinstance(sort, dict) or not sort.get("by"):
        return
    by = str(sort["by"])
    known = {str(m.get("key")) for m in (defn.get("measures") or [])
             if isinstance(m, dict)}
    known |= {str(d.get("name")) for d in (defn.get("dimensions") or [])
              if isinstance(d, dict)}
    if by not in known:
        raise AdhocError([{
            "level": "error", "code": "invalid_sort",
            "message": f"排序字段 {by} 不在本分析的维度/度量中；可用: {sorted(known)}",
        }])


async def query_adhoc_defn(
    session: AsyncSession,
    defn: dict[str, Any],
    *,
    fmt: str | None = None,
    sort: dict[str, Any] | None = None,
    ref_names: set[str] | None = None,
) -> dict[str, Any]:
    """即席聚合：返回 {engine, fallback_reason, row_count, truncated, elapsed_ms,
    defn, format, data}

    - 下推命中 → `engine="pushdown"`（源库 GROUP BY，不拉全量行）；
    - 否则 → `engine="memory"` + `fallback_reason`（`can_pushdown` 的原因，
      如 HAP 源 / 含 computed / 分位算子）；内存路径达行数上限时标 `truncated`。
    """
    validate_adhoc_defn(defn, ref_names)
    _validate_sort(defn, sort)
    out_format = str(fmt or default_format(defn.get("chart_type")))
    if out_format not in FORMATS:
        raise AdhocError([{
            "level": "error", "code": "invalid_format",
            "message": f"不支持的输出格式: {out_format}；可用: {sorted(FORMATS)}",
        }])
    # 排序下推给聚合引擎（在 Top N 之前生效，见 bi_aggregator._apply_sort）
    defn = {**defn, "sort": sort} if sort else defn

    connection_id, _ = parse_form_ref(defn)
    conn = await _load_connection(session, connection_id)

    started = time.perf_counter()
    pushable, reason = can_pushdown(defn, conn)
    truncated = False
    group_rows = await try_pushdown_rows(session, defn) if pushable else None
    if group_rows is not None:
        engine = "pushdown"
        reason = None
        row_count = len(group_rows)
        entries = entries_from_grouped(defn, group_rows)
    else:
        engine = "memory"
        if pushable:
            # 判定可下推但执行失败/超限：try_pushdown_rows 已记原因并缓存
            reason = "pushdown_failed"
        now = datetime.now(timezone.utc)
        rows = await scan_window(session, defn, now, now)
        # 运行时读取（非导入期绑定）：上限常量可被配置/测试覆写
        truncated = len(rows) >= bi_sources.MAX_FORM_ROWS
        row_count = len(rows)
        entries = aggregate_rows(rows, defn)

    # 单桶包装：**所有条目共用同一个桶时间戳**——快照源在格式化层"只取最新桶"
    # （跨桶求和会翻倍），逐条 now() 会因微秒差把除最后一条外的结果全筛掉
    from app.services.bi_aggregator import display_dims
    bucket_ts = datetime.now(timezone.utc)
    buckets = [{
        "bucket_start": bucket_ts,
        # 归并哨兵 → 展示值「其他」（与桶读取路径同一出口口径）
        "dimensions": display_dims(e["dimensions"]),
        "values": e["values"],
        "is_others": bool(e.get("is_others")),
    } for e in entries]
    data = convert(defn, buckets, out_format)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    return {
        "engine": engine,
        "fallback_reason": reason,
        "row_count": row_count,          # 下推=分组行数；内存=参与聚合的源行数
        "group_count": len(entries),     # 结果条目数（含「其他」归并项）
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
        "defn": {k: v for k, v in defn.items() if k != "sort"},
        "format": out_format,
        "data": data,
    }
