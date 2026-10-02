"""BI 聚合定义：常量、维度候选、内置指标集与配置校验

本模块为纯逻辑（无 DB 依赖），便于单测。
设计依据：openspec/changes/add-bi-aggregation-engine/design.md（D4/D6/D11）；
计算字段/聚合比/自定义分组见 openspec/changes/add-bi-calc-fields/。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.services.bi_expr import ExprError, compile_expr, detect_cycle, extract_refs

# ============================================================
# 常量
# ============================================================

# 支持的聚合算子（白名单）
SUPPORTED_AGGS: frozenset[str] = frozenset({
    "count", "sum", "avg", "min", "max",
    "p50", "p95", "p99", "count_distinct", "ratio",
})

# ratio（聚合比）的分子/分母允许的算子（先分别聚合再相除）
RATIO_COMPONENT_AGGS: frozenset[str] = frozenset({
    "count", "sum", "avg", "min", "max",
})

# 需要数值输入的算子（count 不需要 field）
NUMERIC_AGGS: frozenset[str] = frozenset({
    "sum", "avg", "min", "max", "p50", "p95", "p99",
})

# 时间点字段禁止的算子（对时间戳求和/平均无意义）
POINT_IN_TIME_FORBIDDEN_AGGS: frozenset[str] = frozenset({"sum", "avg"})

# 时间粒度 → 秒
GRANULARITY_SECONDS: dict[str, int] = {
    "1min": 60,
    "5min": 300,
    "1h": 3600,
    "1d": 86400,
}

# 数据源类型（form = 连接表单的业务数据，2026-09-25 重定位为主数据源）
SOURCE_TYPES: frozenset[str] = frozenset({
    "form", "data_buffer", "execution_logs", "custom_sql",
})

# 图表类型
# detail = 表单原始行明细表（仅 form 源）；time_line = 时间坐标系折线；
# bar3d = 3D 矩阵坐标系（2D 矩阵 = heatmap）；
# sankey/graph/flow/tree = 关系族（需 ≥2 维度）；scatter 需 2 度量；gauge ≤1 维度
CHART_TYPES: frozenset[str] = frozenset({
    "line", "bar", "pie", "heatmap", "table", "number", "detail",
    "scatter", "radar", "sankey", "graph", "flow", "tree",
    "funnel", "gauge", "time_line", "bar3d",
})

# 字段语义类型（用于时间点/时长校验）
FIELD_TYPES: frozenset[str] = frozenset({
    "number", "duration", "point_in_time", "string",
})

# BI 字段逻辑类型（借鉴 DataEase deType+groupType 的统一中间类型：
# dimension=可分组维度 / number=可聚合数值 / time=时间）
BI_FIELD_TYPES: frozenset[str] = frozenset({"dimension", "number", "time"})

# HAP 控件中文名 → BI 逻辑类型（与字段端点 HAP_CONTROL_TYPE_NAMES 对应）
_HAP_NUMBER_TYPES = frozenset({"数值", "金额", "自动编号"})
_HAP_TIME_TYPES = frozenset({"日期"})

# DB 原始列类型关键字（按序匹配）
_DB_NUMBER_KEYWORDS = ("int", "decimal", "numeric", "number", "float",
                       "double", "real")
_DB_TIME_KEYWORDS = ("date", "time", "year")


def classify_bi_type(type_name: Any) -> str:
    """把字段类型串（HAP 中文名或 DB 原始类型）归一为 BI 逻辑类型

    顺序敏感：先数值后时间（timestamp 含 time 但不是数值），未命中归维度。
    """
    s = str(type_name or "").strip()
    low = s.lower()
    if s in _HAP_NUMBER_TYPES or any(k in low for k in _DB_NUMBER_KEYWORDS):
        return "number"
    if s in _HAP_TIME_TYPES or any(k in low for k in _DB_TIME_KEYWORDS):
        return "time"
    return "dimension"

# 筛选操作符白名单（对齐 DataEase term 族与明道云筛选器常用集）
FILTER_OPS: frozenset[str] = frozenset({
    "eq", "ne", "gt", "ge", "lt", "le",
    "in", "not_in", "contains", "not_contains",
    "between", "not_between",
    "empty", "not_empty",
})

# 筛选操作符中文名（前端下拉用）
FILTER_OP_LABELS: dict[str, str] = {
    "eq": "等于", "ne": "不等于",
    "gt": "大于", "ge": "大于等于", "lt": "小于", "le": "小于等于",
    "in": "属于", "not_in": "不属于",
    "contains": "包含", "not_contains": "不包含",
    "between": "区间（最小~最大）", "not_between": "区间之外",
    "empty": "为空", "not_empty": "不为空",
}

# 按字段逻辑类型区分的操作符集（明道云/DataEase 范式：数值=区间、时间=起止、文本=包含）
FILTER_OPS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "dimension": ("eq", "ne", "in", "not_in",
                  "contains", "not_contains", "empty", "not_empty"),
    "number": ("eq", "ne", "gt", "ge", "lt", "le",
               "between", "not_between", "empty", "not_empty"),
    "time": ("between", "gt", "lt", "eq", "ne", "empty", "not_empty"),
}

# 维度数量上限（时间维不计入）
MAX_DIMENSIONS = 3

# 默认 Top N（单桶结果行数上界）
DEFAULT_TOP_N = 100

# 维度组合数预估告警阈值
CARDINALITY_WARN_THRESHOLD = 5000

# 单窗口分片阈值（行数）
DEFAULT_SHARD_THRESHOLD = 200_000

# 明细保留天数默认值
DEFAULT_RETENTION_DAYS = 7

# ============================================================
# 业务维度候选（用户确认的 8 个，含基数分级）
# ============================================================

DIMENSION_CANDIDATES: list[dict[str, Any]] = [
    {"name": "类别", "code": "category", "cardinality": "low",
     "usable_for_group": True, "hint": "首选分组维度"},
    {"name": "状态", "code": "status", "cardinality": "low",
     "usable_for_group": True, "hint": "首选分组维度"},
    {"name": "地区", "code": "region", "cardinality": "low",
     "usable_for_group": True, "hint": "适合分组"},
    {"name": "客户", "code": "customer", "cardinality": "medium",
     "usable_for_group": True, "hint": "分组需 Top N 截断"},
    {"name": "人", "code": "person", "cardinality": "medium",
     "usable_for_group": True, "hint": "分组需 Top N 截断"},
    {"name": "设备", "code": "equipment", "cardinality": "medium",
     "usable_for_group": True, "hint": "分组需 Top N 截断"},
    {"name": "物料", "code": "material", "cardinality": "high",
     "usable_for_group": False, "hint": "仅作 Top N 排行，不建议作分组维度"},
    {"name": "计划号", "code": "plan_no", "cardinality": "high",
     "usable_for_group": False, "hint": "近乎唯一，仅作筛选/明细"},
]


# ============================================================
# 错误消息归一化（供 error_top 指标使用）
# ============================================================

_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?")
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_NUM_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")

_UNKNOWN_ERROR = "未知错误"


def normalize_error(text: Any) -> str:
    """归一化错误文本作为分组 key

    替换顺序重要：时间戳（含数字）→ UUID（含数字）→ 其余数字。
    仅用于分组；原样本由调用方另行保留。
    """
    if text is None or str(text).strip() == "":
        return _UNKNOWN_ERROR
    s = _TS_RE.sub("<time>", str(text))
    s = _UUID_RE.sub("<id>", s)
    s = _NUM_RE.sub("<n>", s)
    s = _WS_RE.sub(" ", s).strip()
    return s[:200] if s else _UNKNOWN_ERROR


# ============================================================
# 维度组合哈希（UPSERT 冲突键）
# ============================================================

def dim_hash(dimensions: dict | None) -> str:
    """维度组合的稳定哈希（键排序后 JSON，sha1 取 40 位）

    空维度（仅时间桶）也产出稳定哈希，保证幂等 UPSERT 正常。
    """
    payload = json.dumps(dimensions or {}, sort_keys=True, ensure_ascii=False,
                         default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


# ============================================================
# 内置指标集（10 组，全部默认停用）
#
# 2026-09-25 重定位拍板：BI 大屏 = 连接表单的业务数据可视化；
# 平台运营类内置指标（data_buffer / execution_logs 源）不再默认上屏，
# 需要时可在定义页手动启用（引擎保留全部数据源类型）。
# ============================================================

def _m(key: str, agg: str, field: str | None = None,
       filter_: dict | None = None, field_type: str = "number") -> dict:
    d: dict[str, Any] = {"key": key, "agg": agg}
    if field:
        d["field"] = field
    if filter_:
        d["filter"] = filter_
    if field_type != "number":
        d["field_type"] = field_type
    return d


BUILTIN_METRICS: list[dict[str, Any]] = [
    {
        "metric_key": "sync_throughput",
        "name": "同步吞吐趋势",
        "description": "中间数据量与完成/失败计数（数据源：data_buffer）",
        "source_type": "data_buffer",
        "time_field": "created_at",
        "granularity": "1min",
        "dimensions": [],
        "measures": [
            _m("total", "count"),
            _m("completed", "count", filter_={"status": "completed"}),
            _m("failed", "count", filter_={"status": "failed"}),
        ],
        "chart_type": "line",
        "enabled": False,
    },
    {
        "metric_key": "latency_percentile",
        "name": "延迟分位趋势",
        "description": "执行耗时 P50/P95/P99 与均值（数据源：execution_logs）",
        "source_type": "execution_logs",
        "time_field": "created_at",
        "granularity": "5min",
        "dimensions": [],
        "measures": [
            _m("p50", "p50", "duration_ms"),
            _m("p95", "p95", "duration_ms"),
            _m("p99", "p99", "duration_ms"),
            _m("avg", "avg", "duration_ms"),
        ],
        "chart_type": "line",
        "enabled": False,
    },
    {
        "metric_key": "error_top",
        "name": "错误类型 Top N",
        "description": "失败原因归一化后聚合（数据源：execution_logs）",
        "source_type": "execution_logs",
        "time_field": "created_at",
        "granularity": "1h",
        "dimensions": [
            {"name": "error_type", "field": "error_message",
             "normalize": "error_message"},
        ],
        "measures": [_m("count", "count")],
        "filters": {"status": "failed"},
        "chart_type": "bar",
        "enabled": False,
    },
    {
        "metric_key": "task_success_heatmap",
        "name": "任务成功率热力",
        "description": "按任务的成功/失败计数（数据源：execution_logs）",
        "source_type": "execution_logs",
        "time_field": "created_at",
        "granularity": "1h",
        "dimensions": [{"name": "task", "field": "task_id"}],
        "measures": [
            _m("success", "count", filter_={"status": "success"}),
            _m("failed", "count", filter_={"status": "failed"}),
        ],
        "chart_type": "heatmap",
        "enabled": False,
    },
    {
        "metric_key": "task_health",
        "name": "任务健康明细",
        "description": "按任务的成功/失败/耗时明细（数据源：execution_logs）",
        "source_type": "execution_logs",
        "time_field": "created_at",
        "granularity": "1h",
        "dimensions": [{"name": "task", "field": "task_id"}],
        "measures": [
            _m("success", "count", filter_={"status": "success"}),
            _m("failed", "count", filter_={"status": "failed"}),
            _m("avg_duration_ms", "avg", "duration_ms"),
            _m("max_duration_ms", "max", "duration_ms"),
        ],
        "chart_type": "table",
        "enabled": False,
    },
    {
        "metric_key": "connection_health",
        "name": "连接健康总览",
        "description": "按数据流向（源连接→目标连接）的成功/失败与耗时",
        "source_type": "execution_logs",
        "time_field": "created_at",
        "granularity": "1h",
        "dimensions": [
            {"name": "flow", "resolver": "task_connection", "field": "pair",
             "cardinality": "medium"},
        ],
        "measures": [
            _m("success", "count", filter_={"status": "success"}),
            _m("failed", "count", filter_={"status": "failed"}),
            _m("avg_latency_ms", "avg", "duration_ms"),
        ],
        "chart_type": "table",
        "enabled": False,
    },
    {
        "metric_key": "flow_volume",
        "name": "数据流向流量",
        "description": "按数据流向的行数（数据源：execution_logs）",
        "source_type": "execution_logs",
        "time_field": "created_at",
        "granularity": "1h",
        "dimensions": [
            {"name": "flow", "resolver": "task_connection", "field": "pair",
             "cardinality": "medium"},
        ],
        "measures": [_m("runs", "count")],
        "chart_type": "bar",
        "enabled": False,
    },
    {
        "metric_key": "biz_dimension_summary",
        "name": "业务维度汇总",
        "description": "按业务维度（需用户配置 JSON 路径与求和字段）",
        "source_type": "data_buffer",
        "time_field": "created_at",
        "granularity": "1h",
        "dimensions": [],  # 由用户配置
        "measures": [],    # 由用户配置
        "chart_type": "pie",
        "enabled": False,  # 业务指标默认停用（需用户配置后启用）
    },
    {
        "metric_key": "biz_topn",
        "name": "业务 Top N 排名",
        "description": "按业务维度排行的 Top N（需用户配置 JSON 路径）",
        "source_type": "data_buffer",
        "time_field": "created_at",
        "granularity": "1d",
        "dimensions": [],  # 由用户配置
        "measures": [],    # 由用户配置
        "chart_type": "bar",
        "enabled": False,  # 业务指标默认停用
    },
    {
        "metric_key": "kpi_cards",
        "name": "大屏 KPI 卡组",
        "description": "大屏顶栏指标卡（总量/成功/失败/平均耗时）",
        "source_type": "execution_logs",
        "time_field": "created_at",
        "granularity": "1h",
        "dimensions": [],
        "measures": [
            _m("total", "count"),
            _m("success", "count", filter_={"status": "success"}),
            _m("failed", "count", filter_={"status": "failed"}),
            _m("avg_duration_ms", "avg", "duration_ms"),
        ],
        "chart_type": "number",
        "enabled": False,
    },
]

# 业务类指标（需用户配置维度/度量后才可启用）
BUSINESS_METRIC_KEYS: frozenset[str] = frozenset({
    "biz_dimension_summary", "biz_topn",
})


# ============================================================
# 配置校验
# ============================================================

_SQL_WRITE_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"replace|merge|call|execute|vacuum|copy)\b",
    re.IGNORECASE,
)


def is_read_only_sql(sql: str) -> bool:
    """custom_sql 只读校验：拒绝含写操作关键字的语句

    注意：这是保守的静态检查（拒绝即安全），不承担语法解析职责。
    """
    if not sql or not sql.strip():
        return False
    stripped = sql.strip().rstrip(";")
    # 必须以 SELECT / WITH 开头
    if not re.match(r"^\s*(select|with)\b", stripped, re.IGNORECASE):
        return False
    return not _SQL_WRITE_RE.search(stripped)


def validate_metric_def(
    defn: dict[str, Any],
    ref_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    """校验聚合定义；返回问题列表（空列表 = 通过）

    校验项（对应 spec「聚合定义配置化」「业务维度与度量由用户配置」
    「聚合维度基数保护」「派生计算字段」「聚合比度量」「维度值自定义分组」
    「展示层派生」）：
    - 必要字段、枚举值
    - 维度数量上限（时间维不计入）/ 自定义分组结构
    - 算子白名单 / ratio 分子分母结构
    - 时间点字段禁止 sum/avg
    - 计算字段表达式白名单与循环引用
    - 累计修饰仅滚动源
    - custom_sql 只读
    - 高基数维度作分组时的风险提示（warning 级）

    ref_names：可引用的字段名集合（表单字段元数据的 name/path + 派生列）；
    为 None 时表达式引用做宽松校验（存在性由保存层把关）。"""
    issues: list[dict[str, Any]] = []

    def err(code: str, msg: str, **extra: Any) -> None:
        issues.append({"level": "error", "code": code, "message": msg, **extra})

    def warn(code: str, msg: str, **extra: Any) -> None:
        issues.append({"level": "warning", "code": code, "message": msg, **extra})

    metric_key = (defn.get("metric_key") or "").strip()
    if not metric_key:
        err("missing_metric_key", "缺少 metric_key")

    source_type = defn.get("source_type")
    if source_type not in SOURCE_TYPES:
        err("invalid_source_type",
            f"不支持的数据源类型: {source_type}；可用: {sorted(SOURCE_TYPES)}")

    granularity = defn.get("granularity", "1min")
    if granularity not in GRANULARITY_SECONDS:
        err("invalid_granularity",
            f"不支持的时间粒度: {granularity}；可用: {sorted(GRANULARITY_SECONDS)}")

    chart_type = defn.get("chart_type", "line")
    if chart_type not in CHART_TYPES:
        err("invalid_chart_type",
            f"不支持的图表类型: {chart_type}；可用: {sorted(CHART_TYPES)}")

    # ---- 维度 ----
    dimensions = defn.get("dimensions") or []
    if not isinstance(dimensions, list):
        err("invalid_dimensions", "dimensions 必须是数组")
        dimensions = []
    else:
        # 时间维不计入配额
        explicit = [d for d in dimensions
                    if isinstance(d, dict) and not d.get("is_time")]
        if len(explicit) > MAX_DIMENSIONS:
            err("too_many_dimensions",
                f"分组维度数量 {len(explicit)} 超过上限 {MAX_DIMENSIONS}"
                f"（时间维不计入）", limit=MAX_DIMENSIONS,
                actual=len(explicit))
        for d in explicit:
            if not isinstance(d, dict):
                err("invalid_dimension", f"维度项必须是对象: {d!r}")
                continue
            if not (d.get("field") or d.get("resolver")):
                err("invalid_dimension",
                    f"维度 {d.get('name')} 缺少 field 或 resolver")
            if not str(d.get("name") or "").strip():
                # 缺 name 的维度在聚合行里键为 None/被静默丢弃（展平行不可用）
                err("invalid_dimension",
                    f"维度缺少 name（field={d.get('field')}）")
            # 自定义分组（值→组映射；other = 未命中归组）
            groups = d.get("groups")
            if groups is not None:
                if not isinstance(groups, list) or not groups:
                    err("invalid_dim_groups",
                        f"维度 {d.get('name')} 的 groups 必须是非空数组")
                else:
                    for g in groups:
                        if (not isinstance(g, dict) or not str(g.get("name") or "").strip()
                                or not isinstance(g.get("values"), list)
                                or not g["values"]):
                            err("invalid_dim_groups",
                                f"维度 {d.get('name')} 的分组项必须是 "
                                f"{{name, values[]}} 且 values 非空: {g!r}")
                    other = d.get("other")
                    if other is not None and not str(other).strip():
                        err("invalid_dim_groups",
                            f"维度 {d.get('name')} 的 other 不能为空串（省略则保留原值）")
            # 高基数维度作分组 → 风险提示
            code = d.get("code") or d.get("name")
            cand = next((c for c in DIMENSION_CANDIDATES
                         if c.get("code") == code or c.get("name") == code), None)
            if cand and cand.get("cardinality") == "high":
                warn("high_cardinality_dimension",
                     f"维度「{cand['name']}」基数高（{cand['hint']}）；"
                     "建议改用 Top N 排行而非分组",
                     dimension=cand["name"])

    # ---- 度量 ----
    measures = defn.get("measures") or []
    if not isinstance(measures, list):
        err("invalid_measures", "measures 必须是数组")
        measures = []
    if not measures:
        warn("empty_measures", "未配置任何度量（业务指标需配置后才应启用）")
    for m in measures:
        if not isinstance(m, dict):
            err("invalid_measure", f"度量项必须是对象: {m!r}")
            continue
        agg = m.get("agg")
        if agg not in SUPPORTED_AGGS:
            err("unsupported_agg",
                f"不支持的聚合算子: {agg}；可用: {sorted(SUPPORTED_AGGS)}",
                available=sorted(SUPPORTED_AGGS))
            continue
        if not m.get("key"):
            err("invalid_measure", f"度量缺少 key: {m!r}")
        if agg == "ratio":
            # 聚合比：numerator/denominator 各含 {field, agg}，先聚合再相除
            for side in ("numerator", "denominator"):
                comp = m.get(side)
                if not isinstance(comp, dict):
                    err("invalid_ratio",
                        f"ratio 度量必须配置 {side}={{field, agg}}（度量 {m.get('key')}）")
                    continue
                cagg = comp.get("agg")
                if cagg not in RATIO_COMPONENT_AGGS:
                    err("invalid_ratio_agg",
                        f"ratio 的 {side}.agg 不支持: {cagg}；"
                        f"可用: {sorted(RATIO_COMPONENT_AGGS)}")
                if cagg != "count" and not comp.get("field"):
                    err("invalid_ratio", f"ratio 的 {side} 缺少 field")
            scale = m.get("scale")
            if scale is not None and not isinstance(scale, (int, float)):
                err("invalid_ratio_scale", "ratio 的 scale 必须是数值（如 100 表示百分数）")
            continue
        if agg in NUMERIC_AGGS and not m.get("field"):
            err("missing_measure_field",
                f"算子 {agg} 需要指定 field（度量 {m.get('key')}）")
        # 时间点字段禁止 sum/avg
        ftype = m.get("field_type", "number")
        if ftype not in FIELD_TYPES:
            err("invalid_field_type",
                f"不支持的字段类型: {ftype}；可用: {sorted(FIELD_TYPES)}")
        elif ftype == "point_in_time" and agg in POINT_IN_TIME_FORBIDDEN_AGGS:
            err("point_in_time_agg_forbidden",
                f"时间点字段不允许 {agg} 运算（结果无意义），"
                f"仅允许 min/max（度量 {m.get('key')}）",
                allowed=["min", "max"])

    # 下推 ratio 分量逻辑键为 __ratioN__(num|den)：用户度量撞此形态会在
    # 下推结果的别名映射中被覆盖（比值静默串位），保存期直接拒绝
    for m in measures:
        if isinstance(m, dict) and re.match(r"^__ratio\d+__(num|den)$",
                                            str(m.get("key") or "")):
            err("invalid_measure",
                f"度量 key 不能使用保留形态 __ratio<数字>__(num|den)：{m.get('key')}")

    # 度量 key 撞维度名：展平成行时 row.update(values) 会覆盖同名维度值，
    # 图表上表现为该维度取值丢失（P1）
    dim_names = {str(d.get("name") or "").strip() for d in dimensions
                 if isinstance(d, dict) and str(d.get("name") or "").strip()}
    for m in measures:
        if isinstance(m, dict) and str(m.get("key") or "").strip() in dim_names:
            err("measure_key_conflicts_dimension",
                f"度量 key「{m.get('key')}」与维度名同名——展平成行时度量会覆盖维度值，请改名")

    # ---- 计算字段（computed：行级派生列；add-bi-calc-fields） ----
    computed = defn.get("computed") or []
    if not isinstance(computed, list):
        err("invalid_computed", "computed 必须是数组")
        computed = []
    else:
        derived_names: set[str] = set()
        for c in computed:
            if not isinstance(c, dict):
                err("invalid_computed", f"计算字段项必须是对象: {c!r}")
                continue
            ckey = str(c.get("key") or "").strip()
            cname = str(c.get("name") or ckey).strip()
            if not ckey:
                err("invalid_computed", f"计算字段缺少 key: {c!r}")
                continue
            if not str(c.get("expr") or "").strip():
                err("invalid_computed", f"计算字段 {ckey} 缺少 expr")
                continue
            ctype = c.get("field_type", "number")
            if ctype not in FIELD_TYPES:
                err("invalid_computed",
                    f"计算字段 {ckey} 的 field_type 不支持: {ctype}；"
                    f"可用: {sorted(FIELD_TYPES)}")
            derived_names.add(ckey)
            if cname:
                derived_names.add(cname)
        # 表达式静态校验（白名单节点/函数/语法）；ref_names 给定时精确校验引用集合
        for c in computed:
            if not isinstance(c, dict) or not str(c.get("expr") or "").strip():
                continue
            ckey = str(c.get("key") or "").strip()

            def _resolve(name: str, _rn=ref_names, _dn=derived_names) -> str | None:
                if _rn is None:
                    return name  # 宽松模式：引用存在性由保存层（表单字段元数据）把关
                return name if name in _rn or name in _dn else None

            try:
                compile_expr(str(c["expr"]), _resolve)
            except ExprError as e:
                err("invalid_computed_expr",
                    f"计算字段 {ckey} 表达式错误：{e}")
        # 派生列之间循环引用检测（按 key/name 匹配引用名）
        if computed:
            deps: dict[str, set[str]] = {}
            name_to_key: dict[str, str] = {}
            for c in computed:
                if isinstance(c, dict) and c.get("key"):
                    name_to_key[str(c.get("name") or c["key"])] = str(c["key"])
                    name_to_key[str(c["key"])] = str(c["key"])
            for c in computed:
                if not isinstance(c, dict) or not c.get("key"):
                    continue
                refs = {name_to_key[r] for r in extract_refs(str(c.get("expr") or ""))
                        if r in name_to_key}
                deps[str(c["key"])] = refs
            cycle = detect_cycle(deps)
            if cycle:
                err("computed_circular_ref",
                    f"计算字段存在循环引用：{' → '.join(cycle)}")

    # ---- 展示层修饰（占比/累计；累计仅滚动源） ----
    display = defn.get("display") or {}
    if display and not isinstance(display, dict):
        err("invalid_display", "display 必须是对象")
        display = {}
    else:
        if display.get("percent") and not isinstance(display.get("percent"), bool):
            err("invalid_display", "display.percent 必须是布尔值")
        if display.get("cumulative"):
            if not isinstance(display.get("cumulative"), bool):
                err("invalid_display", "display.cumulative 必须是布尔值")
            elif source_type == "form":
                err("cumulative_requires_rolling_source",
                    "快照源（form）不支持累计——每轮都是同一份当前状态，累计无意义；"
                    "仅滚动源（buffer/logs/custom_sql）可用")

    # ---- custom_sql 只读 ----
    if source_type == "custom_sql":
        sql = defn.get("source_ref") or ""
        if not is_read_only_sql(sql):
            err("sql_not_read_only",
                "custom_sql 必须是只读语句（以 SELECT/WITH 开头且不含写操作关键字）")

    # ---- form 源：source_ref 必须是 {"connection_id","form_key"} JSON ----
    if source_type == "form" and not is_valid_form_ref(defn.get("source_ref")):
        err("invalid_form_source",
            'form 数据源的 source_ref 必须是 JSON：'
            '{"connection_id":"…","form_key":"…"}')

    # ---- detail 图表仅 form 源 ----
    if chart_type == "detail" and source_type != "form":
        err("detail_requires_form_source", "明细表图表仅支持 form 数据源")

    # ---- 图表类型的数据形状要求（2026-09-25 图表库扩充） ----
    n_dims = len([d for d in (dimensions or [])
                  if isinstance(d, dict) and not d.get("is_time")])
    n_measures = len([m for m in (measures or []) if isinstance(m, dict)])
    if chart_type == "scatter" and n_measures != 2:
        err("scatter_needs_two_measures", "散点图需要恰好 2 个度量（作为 x/y 轴）")
    if chart_type in ("sankey", "graph", "flow", "tree") and n_dims < 2:
        err("relation_needs_two_dims",
            f"图表类型 {chart_type} 需要至少 2 个分组维度（构成层级/流向关系）")
    if chart_type == "gauge" and n_dims > 1:
        err("gauge_dims_limit", "仪表盘至多 1 个分组维度（取其最新值）")
    if chart_type == "radar" and 0 < n_measures < 3:
        warn("radar_measures_hint",
             "雷达图建议配置 ≥3 个度量作为指标轴，过少难以成图")

    # ---- 筛选规则（list=结构化 field+op+value+field_type；dict=旧等值形态向后兼容） ----
    filt = defn.get("filters")
    if isinstance(filt, list):
        for r in filt:
            if not isinstance(r, dict) or not str(r.get("field") or "").strip():
                err("invalid_filter_rule",
                    f"筛选规则必须是含 field 的对象: {r!r}")
                continue
            op = r.get("op") or "eq"
            if op not in FILTER_OPS:
                err("invalid_filter_op",
                    f"不支持的筛选操作符: {op}；可用: {sorted(FILTER_OPS)}")
                continue
            ftype = r.get("field_type")
            if ftype in FILTER_OPS_BY_TYPE and op not in FILTER_OPS_BY_TYPE[ftype]:
                err("filter_op_type_mismatch",
                    f"操作符 {op} 不适用于 {ftype} 类型字段；"
                    f"可用: {list(FILTER_OPS_BY_TYPE[ftype])}")
            if op in ("between", "not_between") and (
                    not isinstance(r.get("value"), (list, tuple))
                    or len(r["value"]) != 2):
                err("filter_between_needs_range",
                    "区间筛选的值必须是 [最小, 最大] 两个元素")

    return issues


def is_valid_form_ref(source_ref: Any) -> bool:
    """form 源 source_ref 结构校验：JSON 对象且含非空 connection_id / form_key"""
    if not isinstance(source_ref, str) or not source_ref.strip():
        return False
    try:
        cfg = json.loads(source_ref)
    except json.JSONDecodeError:
        return False
    return (isinstance(cfg, dict)
            and bool(str(cfg.get("connection_id") or "").strip())
            and bool(str(cfg.get("form_key") or "").strip()))


def normalize_computed(
    computed: list[dict[str, Any]],
    name_to_path: dict[str, str],
) -> list[dict[str, Any]]:
    """把计算字段表达式中的 [引用名] 规范化为 [字段键]（add-bi-calc-fields）

    解析优先级：派生列（key/name）→ 表单字段（中文显示名 → path）→ 已是 path。
    未知引用抛 ExprError（调用方转 422）。返回新的 computed 列表（不改原对象）。
    派生列引用规范化为其 key（求值后派生列以 key 挂在行上）。
    """
    derived: dict[str, str] = {}
    for c in computed:
        if isinstance(c, dict) and c.get("key"):
            derived[str(c.get("key"))] = str(c["key"])
            if c.get("name"):
                derived[str(c["name"])] = str(c["key"])
    out: list[dict[str, Any]] = []
    for c in computed:
        if not isinstance(c, dict):
            out.append(c)
            continue
        expr = str(c.get("expr") or "")
        refs = extract_refs(expr)   # 字面量内的 [xxx] 已被排除（与 compile_expr 同口径）
        targets = {}
        for ref in refs:
            target = derived.get(ref) or name_to_path.get(ref) or (
                ref if ref in name_to_path.values() else None)
            if not target:
                raise ExprError(f"表达式引用了不存在的字段：[{ref}]")
            targets[ref] = target
        # 只在代码段替换；字符串字面量原样保留（复审修正：全局 re.sub 会把
        # concat("阈值[A线]", [A线]) 的展示文本永久改写入库）
        from app.services.bi_expr import _split_literals
        rewritten = ""
        for seg, is_lit in _split_literals(expr):
            if not is_lit:
                for ref, target in targets.items():
                    seg = re.sub(r"\[\s*" + re.escape(ref) + r"\s*]", f"[{target}]", seg)
            rewritten += seg
        item = dict(c)
        item["expr"] = rewritten
        out.append(item)
    return out


def has_errors(issues: list[dict[str, Any]]) -> bool:
    """问题列表中是否含 error 级问题"""
    return any(i.get("level") == "error" for i in issues)


def estimate_combination_count(cardinalities: list[int]) -> int:
    """按各维度基数估算组合数（用于保存前告警）"""
    total = 1
    for c in cardinalities:
        total *= max(int(c or 1), 1)
    return total
