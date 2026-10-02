"""BI 聚合引擎：窗口计算 + 幂等写入 + 水位线 + Top N

设计依据（design.md）：
- D1 每分钟增量批聚合（固定窗口，成本不随历史增长）
- D2 幂等：窗口**整体覆盖**（先删该桶再插入，而非累加）
- D3 水位线：断点续跑 / 区间回填
- D4 聚合在 Python 侧完成（分位数与过滤度量跨库一致）
- D11 维度基数保护：维度数 ≤3（校验层）+ 结果按主度量 Top N 截断
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bi import BiAggregationWatermark, BiMetricBucket
from app.services.bi_defs import (
    FILTER_OPS,
    GRANULARITY_SECONDS,
    normalize_error,
)
from app.services.bi_expr import compile_expr, eval_compiled, extract_refs
from app.services.bi_sources import (
    SourceError,
    load_resolvers,
    scan_window,
    try_pushdown_rows,
)

logger = logging.getLogger(__name__)

# 一次 run 最多补齐的窗口数（防滞后时一次跑爆）
MAX_WINDOWS_PER_RUN = 60

# 「其他」归并项使用的维度值文案
OTHERS_LABEL = "其他"
# Top-N 归并项在**存储层**的哨兵值：真实维度值可能恰好叫「其他」，若归并项也写
# 「其他」，两行 dim_hash 相同会撞 (metric_key, bucket_start, dim_hash) 唯一索引，
# 整窗聚合失败且水位永不前进（审查 P1）。存储/计算用哨兵，展示与过滤前经
# display_dims() 还原为 OTHERS_LABEL。
OTHERS_KEY = "__others__"


def display_dims(dimensions: Any) -> Any:
    """桶里的归并哨兵 → 展示值「其他」（读取端点与即席出口统一走此还原）"""
    if not isinstance(dimensions, dict) or OTHERS_KEY not in dimensions.values():
        return dimensions
    return {k: (OTHERS_LABEL if v == OTHERS_KEY else v)
            for k, v in dimensions.items()}

# form 快照源保留的桶数上限：快照语义下每轮累积一个"当前状态"桶，
# 无限累积会让图例/时间轴堆满历史噪音——只保留最近 N 个
FORM_SNAPSHOT_KEEP_BUCKETS = 60

# 在飞重查询上限（扫源 + 内存聚合段；投放页读预聚合桶不经此路径，不受限）
MAX_INFLIGHT_HEAVY_QUERIES = 4

# 走线程池做内存聚合的最小行数（小聚合微秒级、同步执行零调度开销；
# 大聚合移出事件循环避免挤占投放请求）
HEAVY_THREAD_MIN_ROWS = 2000


class _LoopBoundSemaphore:
    """绑定事件循环的信号量代理

    模块级 asyncio.Semaphore 在 3.10+ 不再绑定循环，但跨循环复用（测试的
    asyncio.run 多次调用）仍会有等待者泄漏风险——按当前循环懒建、循环变化重建。
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._sem: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        if self._sem is None or self._loop is not loop:
            self._sem = asyncio.Semaphore(self._limit)
            self._loop = loop
        await self._sem.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._sem is not None:
            self._sem.release()


_HEAVY_QUERY_SEMAPHORE = _LoopBoundSemaphore(MAX_INFLIGHT_HEAVY_QUERIES)


# ============================================================
# 时间工具
# ============================================================

def to_utc(ts: datetime) -> datetime:
    """统一为 UTC 时区感知时间"""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def bucket_start_for(ts: datetime, granularity: str) -> datetime:
    """把时间戳对齐到所属时间桶的起点（UTC，向下取整）"""
    seconds = GRANULARITY_SECONDS[granularity]
    epoch = int(to_utc(ts).timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=timezone.utc)


def iter_windows(
    last_bucket: datetime | None,
    now: datetime,
    granularity: str,
    max_windows: int = MAX_WINDOWS_PER_RUN,
) -> list[tuple[datetime, datetime]]:
    """生成待处理的**完整**窗口列表 [(start, end), ...]

    只处理已结束的窗口（不含当前正在累积的桶）。
    无水位线时从上一个完整窗口开始。
    """
    seconds = GRANULARITY_SECONDS[granularity]
    step = timedelta(seconds=seconds)
    latest_complete = bucket_start_for(now, granularity) - step

    if last_bucket is not None:
        start = to_utc(last_bucket) + step
    else:
        start = latest_complete

    windows: list[tuple[datetime, datetime]] = []
    cur = start
    while cur <= latest_complete and len(windows) < max_windows:
        windows.append((cur, cur + step))
        cur += step
    return windows


def windows_between(
    start: datetime, end: datetime, granularity: str
) -> list[tuple[datetime, datetime]]:
    """生成与 [start, end) 相交的全部窗口（回填用）

    注意边界：不能用 `cur < bucket_start_for(end)`——当 start/end 落在同一个
    粗粒度桶内（如 30 分钟区间 × 1h 粒度）时会得到 0 个窗口，
    导致回填静默不产生任何结果。正确判定是「桶起点落在区间内」。
    """
    step = timedelta(seconds=GRANULARITY_SECONDS[granularity])
    cur = bucket_start_for(start, granularity)
    windows: list[tuple[datetime, datetime]] = []
    while cur < end:
        windows.append((cur, cur + step))
        cur += step
    return windows


# ============================================================
# 取值工具
# ============================================================

def json_get(obj: Any, path: str) -> Any:
    """按点分路径取嵌套值（不存在的路径返回 None）"""
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def get_field(row: dict[str, Any], field: str | None) -> Any:
    """从扫描行取字段值

    先按顶层列取（status/task_id/duration_ms 等）；未命中则进入 data（中间数据
    JSON）按点分路径取。两种数据源统一处理。

    大小写互认（add-bi-sql-pushdown 存量修复）：Oracle 驱动返回的键经
    cursor.description 小写化，而字段元数据来自 user_tab_columns（大写列名）——
    直接精确匹配会让 Oracle DB form 源的聚合全取 None。命中失败时按小写归一
    再配一次（对齐 AGENTS 陷阱 9 的既有约定）。
    """
    if not field:
        return None
    if field in row:
        return row.get(field)
    lower = field.lower()
    for key in row.keys():
        if isinstance(key, str) and key.lower() == lower:
            return row.get(key)
    data = row.get("data")
    if isinstance(data, dict):
        value = json_get(data, field)          # 点分路径下钻（info.region 等）
        if value is not None:
            return value
        for key in data.keys():
            if isinstance(key, str) and key.lower() == lower:
                return data.get(key)
    return None


def _as_number(value: Any) -> float | None:
    """尽力转数值；不可转返回 None（不参与数值聚合）"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


# 筛选操作符白名单 FILTER_OPS 定义在 bi_defs（校验层共用）


def _rule_match(row: dict[str, Any], rule: dict[str, Any]) -> bool:
    """单条结构化筛选规则匹配（field + op + value）

    值语义：in/not_in 为数组；empty/not_empty 无值；其余为标量
    （数值比较时尽力互转，字符串比较大小写敏感）。
    """
    op = str(rule.get("op") or "eq")
    if op not in FILTER_OPS:
        return True  # 未知操作符不静默过滤（校验层已拦截，此处防御）
    actual = get_field(row, rule.get("field"))
    if op == "empty":
        return actual is None or str(actual).strip() == ""
    if op == "not_empty":
        return not (actual is None or str(actual).strip() == "")
    expect = rule.get("value")
    ftype = rule.get("field_type")

    def as_num(v: Any) -> float | None:
        return _as_number(v)

    def to_str(v: Any) -> str:
        return "" if v is None else str(v)

    if op in ("in", "not_in"):
        expects = expect if isinstance(expect, list) else [expect]
        hit = any(actual == e or to_str(actual) == to_str(e) for e in expects)
        return hit if op == "in" else not hit

    if op in ("between", "not_between"):
        # 区间：value = [最小, 最大]；数值字段按数值比较，时间/文本按字符串序
        if not isinstance(expect, (list, tuple)) or len(expect) != 2:
            return True  # 配置不全时不误杀
        lo, hi = expect
        if ftype == "number":
            a = as_num(actual)
            lo_n, hi_n = as_num(lo), as_num(hi)
            if a is None or lo_n is None or hi_n is None:
                return op == "not_between"
            hit = lo_n <= a <= hi_n
        else:
            s = to_str(actual)
            lo_s, hi_s = to_str(lo), to_str(hi)
            hit = lo_s <= s <= hi_s
        return hit if op == "between" else not hit

    if op == "contains":
        return to_str(expect).lower() in to_str(actual).lower()
    if op == "not_contains":
        return to_str(expect).lower() not in to_str(actual).lower()

    # 标量比较：先等值/字符串，再数值兜底（"5" vs 5）
    if op == "eq":
        return actual == expect or to_str(actual) == to_str(expect)
    if op == "ne":
        return not (actual == expect or to_str(actual) == to_str(expect))

    a_num, e_num = as_num(actual), as_num(expect)
    if a_num is not None and e_num is not None:
        a_val, e_val = a_num, e_num
    else:
        a_val, e_val = to_str(actual), to_str(expect)
    if op == "gt":
        return a_val > e_val
    if op == "ge":
        return a_val >= e_val
    if op == "lt":
        return a_val < e_val
    if op == "le":
        return a_val <= e_val
    return True


def _match_filter(row: dict[str, Any], filt: dict | list | None) -> bool:
    """筛选匹配：两种形态

    - dict（旧形态，向后兼容）：{字段: 期望值} 等值
    - list（结构化规则）：[{field, op, value}, ...]，规则间 AND
    """
    if not filt:
        return True
    if isinstance(filt, list):
        return all(
            _rule_match(row, r) for r in filt
            if isinstance(r, dict) and r.get("field")
        )
    if isinstance(filt, dict):
        for key, expect in filt.items():
            actual = get_field(row, key)
            if actual is None:
                return False
            if actual == expect or str(actual) == str(expect):
                continue
            return False
        return True
    return True


def _percentile(values: list[float], q: float) -> float | None:
    """线性插值分位数（与 app/api/metrics.py 口径一致，跨库统一实现）"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


# ============================================================
# 维度提取
# ============================================================

def dim_value(
    row: dict[str, Any],
    dim: dict[str, Any],
    resolvers: dict[str, dict[str, dict[str, Any]]],
) -> Any:
    """提取单个维度的值

    - 有 resolver：走预加载的内存映射（经 task_id 关联）
    - 有 normalize=error_message：归一化错误文本
    - 否则：按 field 从行取（含 JSON 路径）
    """
    resolver = dim.get("resolver")
    if resolver:
        mapping = resolvers.get(resolver) or {}
        info = mapping.get(str(row.get("task_id"))) or {}
        val = info.get(dim.get("field"))
        return val if val is not None else "未知"

    field = dim.get("field")
    val = get_field(row, field)
    if dim.get("normalize") == "error_message":
        return normalize_error(val)
    # 自定义分组（值 → 组名；未命中归 other，未声明 other 保留原值）
    groups = dim.get("groups")
    if groups and val is not None:
        raw = str(val)
        for g in groups:
            if raw in [str(v) for v in (g.get("values") or [])]:
                return g.get("name")
        other = dim.get("other")
        return other if other else (val if isinstance(val, (int, float, bool)) else raw)
    return _dim_scalar(val)


def _dim_scalar(value: Any) -> Any:
    """维度标量归一（内存路径 dim_value 尾部规则；下推路径共用同一口径）

    NULL → "未知"；数值/布尔保留原值（不字符串化，保持分组键类型稳定）；
    其余 str()（时间/Decimal 等与内存路径同款）。
    """
    if value is None:
        return "未知"
    if isinstance(value, (int, float, bool)):
        return value
    return str(value)


# ============================================================
# 行级派生列（计算字段；add-bi-calc-fields）
# ============================================================

def apply_computed(
    rows: list[dict[str, Any]],
    computed: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """对每一行求值派生列（原地写入 row[key]），返回 rows

    - 表达式引用已在保存层规范化为字段键（中文名解析发生在 API 层）
    - 派生列可引用其他派生列：按依赖拓扑排序，被引用者先算
      （循环引用在定义校验期已拦截，此处防御性兜底为原序）
    - 单行缺值/类型不合法 → 该行该列 None（行级容错）
    """
    if not computed:
        return rows
    name_to_key: dict[str, str] = {}
    for c in computed:
        if isinstance(c, dict) and c.get("key"):
            name_to_key[str(c["key"])] = str(c["key"])
            if c.get("name"):
                name_to_key[str(c["name"])] = str(c["key"])
    deps: dict[str, set[str]] = {
        str(c["key"]): {name_to_key[r] for r in extract_refs(str(c.get("expr") or ""))
                        if r in name_to_key}
        for c in computed if isinstance(c, dict) and c.get("key")
    }
    pending = [c for c in computed if isinstance(c, dict) and c.get("key")]
    ordered: list[dict[str, Any]] = []
    done: set[str] = set()
    while pending:
        progressed = False
        for c in list(pending):
            if deps.get(str(c["key"]), set()) <= done:
                ordered.append(c)
                pending.remove(c)
                done.add(str(c["key"]))
                progressed = True
        if not progressed:          # 防御：理论不可达（校验期拦环）
            ordered.extend(pending)
            break

    compiled = [(str(c["key"]), compile_expr(str(c["expr"]), lambda n: n)) for c in ordered]
    # 引用取值经 get_field（form 源行是 {"data": {...}} 包装结构：顶层未命中进 data）；
    # 派生列结果写回行顶层，供后续派生列/维度/度量经 get_field 读取
    ref_keys: set[str] = set()
    for _, comp in compiled:
        ref_keys.update(comp.refs.values())
    for row in rows:
        probe: dict[str, Any] = {k: get_field(row, k) for k in ref_keys}
        for key, comp in compiled:
            value = eval_compiled(comp, probe)
            probe[key] = value
            row[key] = value
    return rows


# ============================================================
# 度量计算
# ============================================================

def compute_measure(rows: list[dict[str, Any]], measure: dict[str, Any]) -> Any:
    """计算单个度量的值

    count 无需 field；数值算子（sum/avg/min/max/pXX）跳过不可转数值的行；
    count_distinct 统计去重后的非空值个数；
    ratio 先分别聚合分子/分母再相除（除零/空 → None，不编造 0）。
    """
    agg = measure.get("agg")
    filt = measure.get("filter")
    selected = [r for r in rows if _match_filter(r, filt)] if filt else rows

    if agg == "count":
        return len(selected)

    if agg == "ratio":
        num_cfg = measure.get("numerator") or {}
        den_cfg = measure.get("denominator") or {}
        numerator = compute_measure(selected, {
            "agg": num_cfg.get("agg", "sum"), "field": num_cfg.get("field")})
        denominator = compute_measure(selected, {
            "agg": den_cfg.get("agg", "sum"), "field": den_cfg.get("field")})
        if numerator is None or denominator is None or denominator == 0:
            return None
        value = numerator / denominator
        scale = measure.get("scale")
        return value * float(scale) if isinstance(scale, (int, float)) else value

    field = measure.get("field")
    if agg == "count_distinct":
        # 不可哈希值（HAP 多选/附件等 list/dict）稳定字符串化后参与去重，
        # 不再抛 TypeError 让整窗聚合失败（审查 P2）
        seen: set = set()
        for r in selected:
            v = get_field(r, field)
            if v is None:
                continue
            try:
                seen.add(v)
            except TypeError:
                seen.add(json.dumps(v, sort_keys=True, ensure_ascii=False, default=str))
        return len(seen)

    numbers: list[float] = []
    for r in selected:
        n = _as_number(get_field(r, field))
        if n is not None:
            numbers.append(n)
    if not numbers:
        return None

    if agg == "sum":
        return sum(numbers)
    if agg == "avg":
        return sum(numbers) / len(numbers)
    if agg == "min":
        return min(numbers)
    if agg == "max":
        return max(numbers)
    if agg == "p50":
        return _percentile(numbers, 0.50)
    if agg == "p95":
        return _percentile(numbers, 0.95)
    if agg == "p99":
        return _percentile(numbers, 0.99)
    return None


# ============================================================
# 分组与 Top N
# ============================================================

# 可跨组合并的算子（其余在「其他」项置 None，不谎报）
_ADDITIVE_AGGS = frozenset({"count", "sum"})


def _apply_top_n(
    entries: list[dict[str, Any]],
    dims: list[dict[str, Any]],
    measures: list[dict[str, Any]],
    top_n: int,
    preserve_order: bool = False,
) -> list[dict[str, Any]]:
    """按主度量（第一个度量）降序取 Top N，其余归并为「其他」

    - 保证单桶结果行数有界（D11）；
    - 「其他」项只累加可加算子，不可加算子置 None（诚实，不编造）；
    - preserve_order=True（调用方带用户 sort 时）：Top N 只决定"看哪些"不改顺序，
      按 entries 原顺序复原（_apply_sort 已排好），「其他」归并项固定殿后；
      无用户 sort 时保持主度量降序（看板排行卡的默认可读顺序）。
    """
    if not top_n or top_n <= 0 or len(entries) <= top_n:
        return entries

    primary = measures[0]["key"] if measures else None

    def sort_key(e: dict[str, Any]):
        v = e["values"].get(primary) if primary else None
        num = v if isinstance(v, (int, float)) else 0
        return (0 if isinstance(v, (int, float)) else 1, -num)

    ordered = sorted(entries, key=sort_key)
    kept = ordered[:top_n]
    rest = ordered[top_n:]
    if preserve_order:
        kept_set = {id(e) for e in kept}
        kept = [e for e in entries if id(e) in kept_set]
    if not rest:
        return kept

    others_values: dict[str, Any] = {}
    for m in measures:
        key, agg = m["key"], m["agg"]
        if agg in _ADDITIVE_AGGS:
            others_values[key] = sum(
                (e["values"].get(key) or 0) for e in rest)
        else:
            others_values[key] = None  # 分位/均值等不可跨组合并

    kept.append({
        # 存储层用哨兵（见 OTHERS_KEY）：避免与真实维度值「其他」撞 dim_hash；
        # 展示前由 display_dims 还原
        "dimensions": {d["name"]: OTHERS_KEY for d in dims},
        "values": others_values,
        "is_others": True,
    })
    return kept


def _apply_sort(entries: list[dict[str, Any]],
                defn: dict[str, Any]) -> list[dict[str, Any]]:
    """按 `defn["sort"]` 排序（即席分析用；看板定义无此字段 → 原样返回）

    sort = `{"by": <度量 key | 维度名>, "order": "asc"|"desc"}`：
    - **在 Top N 之前**生效——先决定"看哪些/什么顺序"，再由 `_apply_top_n` 截断并
      追加「其他」归并项（两项语义独立，互不覆盖）；
    - 稳定：只对可比较的数值项排序，缺值/文本项保持原相对顺序并置末尾
      （降序时也不把空值顶到最前）。
    """
    spec = defn.get("sort") if isinstance(defn, dict) else None
    if not isinstance(spec, dict) or not spec.get("by"):
        return entries
    by = str(spec["by"])
    order = "asc" if str(spec.get("order") or "desc").lower() == "asc" else "desc"
    if any(str((m or {}).get("key")) == by for m in (defn.get("measures") or [])):
        def getter(e: dict[str, Any]) -> Any:
            return (e.get("values") or {}).get(by)
    elif any(str((d or {}).get("name")) == by for d in (defn.get("dimensions") or [])):
        def getter(e: dict[str, Any]) -> Any:
            return (e.get("dimensions") or {}).get(by)
    else:
        return entries      # 未知排序字段：不静默改变顺序（调用方已校验）

    def as_num(e: dict[str, Any]) -> float | None:
        v = getter(e)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        return None

    def as_text(e: dict[str, Any]) -> str | None:
        v = getter(e)
        return v if isinstance(v, str) and v.strip() != "" else None

    nums = [e for e in entries if as_num(e) is not None]
    texts = [e for e in entries if as_num(e) is None and as_text(e) is not None]
    blanks = [e for e in entries if as_num(e) is None and as_text(e) is None]
    nums.sort(key=lambda e: as_num(e) or 0.0, reverse=(order == "desc"))
    texts.sort(key=lambda e: as_text(e) or "", reverse=(order == "desc"))
    return nums + texts + blanks


def aggregate_rows(
    rows: list[dict[str, Any]],
    defn: dict[str, Any],
    resolvers: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """把窗口内的原始行聚合为 [{dimensions, values}, ...]

    纯函数（无 IO），便于单测。
    行级派生列（computed）在过滤/分组之前求值——筛选与维度/度量都可引用派生列。
    """
    dims = defn.get("dimensions") or []
    measures = defn.get("measures") or []
    top_n = defn.get("top_n") or 0
    resolvers = resolvers or {}

    rows = apply_computed(rows, defn.get("computed"))
    top_filters = defn.get("filters") or {}
    filtered = [r for r in rows if _match_filter(r, top_filters)]

    if not dims:
        # 无维度：单条汇总
        entries = [{
            "dimensions": {},
            "values": {m["key"]: compute_measure(filtered, m) for m in measures},
        }]
        return entries

    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in filtered:
        key = tuple(dim_value(row, d, resolvers) for d in dims)
        groups.setdefault(key, []).append(row)

    entries = []
    for key, group_rows in groups.items():
        entries.append({
            "dimensions": {d["name"]: key[i] for i, d in enumerate(dims)},
            "values": {m["key"]: compute_measure(group_rows, m) for m in measures},
        })
    return _apply_top_n(_apply_sort(entries, defn), dims, measures, top_n,
                        preserve_order=bool(defn.get("sort")))


def entries_from_grouped(
    defn: dict[str, Any], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """把下推分组行装配为 entries（与内存 aggregate_rows 同形）

    - 维度值走 `_dim_scalar`（NULL→"未知"、数值保留、其余 str）；
    - 度量值由驱动层归一（count→int，其余→float，NULL→None）——与
      compute_measure 的返回类型一致；
    - ratio：SQL 侧两个分量列在此相除并乘 scale（除零/缺值 → None，不编造 0）；
    - Top N/「其他」归并复用 `_apply_top_n`（纯函数，与内存路径同一实现）。
    """
    from app.services.bi_sources import pushdown_aggs     # 延迟导入：避免模块环

    dims = defn.get("dimensions") or []
    measures = defn.get("measures") or []
    _, ratio_keys = pushdown_aggs(defn)

    entries: list[dict[str, Any]] = []
    for row in rows:
        values: dict[str, Any] = {}
        for i, m in enumerate(measures):
            if not isinstance(m, dict):
                continue
            key = str(m.get("key") or f"m{i}")
            if m.get("agg") == "ratio":
                comp = ratio_keys.get(key) or {}
                numerator = row.get(comp.get("numerator", ""))
                denominator = row.get(comp.get("denominator", ""))
                if numerator is None or denominator is None or denominator == 0:
                    values[key] = None
                else:
                    value = numerator / denominator
                    scale = m.get("scale")
                    values[key] = (value * float(scale)
                                   if isinstance(scale, (int, float)) else value)
            else:
                values[key] = row.get(key)
        entries.append({
            "dimensions": {d["name"]: _dim_scalar(row.get(d.get("field")))
                           for d in dims if isinstance(d, dict) and d.get("name")},
            "values": values,
        })
    return _apply_top_n(_apply_sort(entries, defn), dims, measures,
                        defn.get("top_n") or 0,
                        preserve_order=bool(defn.get("sort")))


# ============================================================
# 窗口计算与写入
# ============================================================

async def compute_window(
    session: AsyncSession,
    defn: dict[str, Any],
    start: datetime,
    end: datetime,
    resolvers: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """扫描一个窗口并计算聚合结果

    取值顺序（add-bi-sql-pushdown）：
    1. DB form 源且判定可下推 → SQL GROUP BY 聚合（不拉全量行，不受 MAX_FORM_ROWS）；
    2. 其余（HAP / 不可下推 / 下推异常）→ 扫窗口原始行 + 内存聚合。

    并发硬化：内存聚合（纯 CPU 计算）与大结果装配经 to_thread 移出事件循环；
    扫源+聚合整段受在飞重查询信号量约束（投放读桶不经此路径，不受限）。
    """
    if resolvers is None:
        resolvers = await load_resolvers(session, defn)
    async with _HEAVY_QUERY_SEMAPHORE:
        grouped = await try_pushdown_rows(session, defn)
        if grouped is not None:
            if len(grouped) >= HEAVY_THREAD_MIN_ROWS:
                return await asyncio.to_thread(entries_from_grouped, defn, grouped)
            return entries_from_grouped(defn, grouped)
        rows = await scan_window(session, defn, start, end)
        if len(rows) >= HEAVY_THREAD_MIN_ROWS:
            return await asyncio.to_thread(aggregate_rows, rows, defn, resolvers)
        return aggregate_rows(rows, defn, resolvers)


async def write_window(
    session: AsyncSession,
    metric_key: str,
    bucket_start: datetime,
    entries: list[dict[str, Any]],
) -> None:
    """把某窗口的结果写入预聚合表（窗口整体覆盖，D2）

    先删除该 (metric_key, bucket_start) 的全部旧行再插入——相比 UPSERT，
    这样还能清掉「上轮存在、本轮已消失」的维度组合，避免残留脏桶。
    """
    from app.services.bi_defs import dim_hash

    await session.execute(
        delete(BiMetricBucket).where(
            BiMetricBucket.metric_key == metric_key,
            BiMetricBucket.bucket_start == bucket_start,
        )
    )
    for e in entries:
        session.add(BiMetricBucket(
            metric_key=metric_key,
            bucket_start=bucket_start,
            dim_hash=dim_hash(e["dimensions"]),
            dimensions=e["dimensions"],
            values=e["values"],
        ))
    await session.commit()


async def _advance_watermark(
    session: AsyncSession, metric_key: str, completed_bucket: datetime
) -> None:
    """推进水位线（只允许向前）"""
    # SQL 级条件更新（审查 P3）：原实现比较的是本会话身份映射里的（可能过期
    # 的）值、UPDATE 无条件写——调度轮与手动 /aggregate/run 重叠时水位可回退。
    # 比较值归一成 naive（与库内存储同形）：ORM 更新的会话内评估器做 Python
    # 侧比较，naive/aware 混比会 TypeError
    naive = (completed_bucket.astimezone(timezone.utc).replace(tzinfo=None)
             if completed_bucket.tzinfo else completed_bucket)
    await session.execute(
        update(BiAggregationWatermark)
        .where(BiAggregationWatermark.metric_key == metric_key,
               BiAggregationWatermark.last_bucket < naive)
        .values(last_bucket=naive))
    wm = await session.get(BiAggregationWatermark, metric_key)
    if wm is None:
        session.add(BiAggregationWatermark(
            metric_key=metric_key, last_bucket=naive))
    await session.commit()


# ============================================================
# 对外入口：增量运行 / 区间回填
# ============================================================

async def _prune_form_buckets(
    session: AsyncSession, metric_key: str,
    keep: int = FORM_SNAPSHOT_KEEP_BUCKETS,
) -> int:
    """清理 form 快照指标的旧桶（按 bucket_start 降序保留最近 keep 个）

    追平与否每轮都执行（一次 distinct 查询，代价小）——水位线已追平时
    不产生新桶，但没有这一步存量旧桶永远不会被清。
    """
    starts = (await session.execute(
        select(BiMetricBucket.bucket_start)
        .where(BiMetricBucket.metric_key == metric_key)
        .distinct()
        .order_by(BiMetricBucket.bucket_start.desc())
    )).scalars().all()
    stale = starts[keep:]
    if stale:
        await session.execute(
            delete(BiMetricBucket).where(
                BiMetricBucket.metric_key == metric_key,
                BiMetricBucket.bucket_start.in_(stale),
            ))
        await session.commit()
    return len(stale)


async def run_metric(
    session: AsyncSession,
    defn: dict[str, Any],
    now: datetime | None = None,
    max_windows: int = MAX_WINDOWS_PER_RUN,
) -> dict[str, Any]:
    """增量运行一个指标：从水位线起逐窗口补齐

    返回 {"metric_key", "windows", "lagged"}；lagged=True 表示本轮未追平
    （滞后超过 max_windows），调用方应记录告警。
    """
    now = now or datetime.now(timezone.utc)
    metric_key = defn["metric_key"]
    granularity = defn.get("granularity", "1min")

    # form 源为快照语义：每次只算最新一个完整窗口——历史窗口回溯只会
    # 重复同一份当前快照，逐窗补齐没有意义，水位线直接前跳
    if defn.get("source_type") == "form":
        step = timedelta(seconds=GRANULARITY_SECONDS[granularity])
        latest_complete = bucket_start_for(now, granularity) - step
        # 快照桶保留窗口：无论是否追平，先清理超限旧桶
        pruned = await _prune_form_buckets(session, metric_key)
        wm = await session.get(BiAggregationWatermark, metric_key)
        if wm is not None and to_utc(wm.last_bucket) >= latest_complete:
            return {"metric_key": metric_key, "windows": 0, "lagged": False,
                    "pruned": pruned}
        resolved = await load_resolvers(session, defn)
        entries = await compute_window(session, defn, latest_complete,
                                       latest_complete + step, resolved)
        await write_window(session, metric_key, latest_complete, entries)
        await _advance_watermark(session, metric_key, latest_complete)
        return {"metric_key": metric_key, "windows": 1, "lagged": False,
                "pruned": pruned}

    wm = await session.get(BiAggregationWatermark, metric_key)
    last_bucket = to_utc(wm.last_bucket) if wm is not None else None

    windows = iter_windows(last_bucket, now, granularity, max_windows)
    resolved = await load_resolvers(session, defn)

    computed = 0
    for start, end in windows:
        entries = await compute_window(session, defn, start, end, resolved)
        await write_window(session, metric_key, start, entries)
        await _advance_watermark(session, metric_key, start)
        computed += 1

    # 未追平判定：还有比 max_windows 更早的窗口待处理
    remaining = iter_windows(last_bucket, now, granularity, max_windows + 1)
    lagged = len(remaining) > max_windows

    return {"metric_key": metric_key, "windows": computed, "lagged": lagged}


async def backfill(
    session: AsyncSession,
    defn: dict[str, Any],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """区间回填：重算 [start, end) 内的所有窗口（覆盖既有桶，D3）"""
    if defn.get("source_type") == "form":
        raise SourceError("form 数据源为快照语义，不支持区间回填")
    metric_key = defn["metric_key"]
    granularity = defn.get("granularity", "1min")
    from app.services.bi_defs import GRANULARITY_SECONDS
    if (to_utc(end) - to_utc(start)).total_seconds() > MAX_WINDOWS_PER_RUN * GRANULARITY_SECONDS[granularity]:
        raise SourceError("回填区间超过单次窗口上限，请分段执行")
    windows = windows_between(start, end, granularity)
    resolved = await load_resolvers(session, defn)

    for win_start, win_end in windows:
        entries = await compute_window(session, defn, win_start, win_end, resolved)
        await write_window(session, metric_key, win_start, entries)

    return {"metric_key": metric_key, "windows": len(windows)}


async def estimate_cardinality(
    session: AsyncSession, defn: dict[str, Any], now: datetime | None = None
) -> dict[str, Any]:
    """预估最近一个窗口的维度组合数（保存定义前的基数告警，D11）"""
    now = now or datetime.now(timezone.utc)
    granularity = defn.get("granularity", "1min")
    step = timedelta(seconds=GRANULARITY_SECONDS[granularity])
    end = bucket_start_for(now, granularity)
    start = end - step

    dims = defn.get("dimensions") or []
    if not dims:
        return {"combinations": 1, "threshold_exceeded": False}

    grouped = await try_pushdown_rows(session, defn)
    if grouped is not None:
        # 下推路径已按维度分组返回：行数即组合数（大表不再全量拉行）
        combos = len(grouped)
    else:
        resolved = await load_resolvers(session, defn)
        rows = await scan_window(session, defn, start, end)
        combos = len({
            tuple(dim_value(r, d, resolved) for d in dims)
            for r in rows
        })
    from app.services.bi_defs import CARDINALITY_WARN_THRESHOLD
    return {
        "combinations": combos,
        "threshold_exceeded": combos > CARDINALITY_WARN_THRESHOLD,
        "threshold": CARDINALITY_WARN_THRESHOLD,
    }
