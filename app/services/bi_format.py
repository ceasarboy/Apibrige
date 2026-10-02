"""BI 图表格式转换层

统一中间格式 → 前端可直接渲染的结构（design D8：后端转换，前端只渲染）。

中间格式（引擎输出，与具体图表库解耦）：
    {
      "metric_key": str, "chart_type": str, "type": str,
      "dimensions": [{"name","label"}],
      "x": [str],                                  # 时间轴
      "series": [{"key","label","data":[...]}],    # 时序
      "items": [{"name","value"}],                 # 分布
      "cells": [[x, y, value]],                    # 矩阵
      "columns": [...], "rows": [...],             # 表格
      "value": Any, "unit": str,                   # 单值
      "meta": {"bucket_count": int, "truncated": bool}
    }
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

# 图表类型 → 中间结构类型
_CHART_KIND = {
    "line": "timeseries",
    "bar": "category",           # 类别对比（DataEase 式）：x = 维度值，series = 度量
    "heatmap": "matrix",
    "pie": "distribution",
    "table": "records",
    "number": "scalar",
    # 2026-09-25 图表库扩充
    "scatter": "pairs",          # 散点：跨桶的度量对
    "radar": "radar",            # 雷达：度量=指标轴，最新桶各维度组合=系列
    "sankey": "links",           # 桑基：dims[0]→dims[1] 流量
    "graph": "links",            # 关系图：同 links（前端力导向布局）
    "flow": "links",             # 途径图：同 links（前端有向箭头）
    "tree": "tree",              # 树图：维度按序成层级
    "funnel": "distribution",    # 漏斗：分布项降序
    "gauge": "scalar",           # 仪表盘：最新主度量值
    "time_line": "timeseries",   # 时间坐标系（x 轴 type=time）
    "bar3d": "matrix",           # 3D 矩阵坐标系（x/y/值 三元组）
}

# 可跨桶/跨组合并的算子（其余不可加，需特殊语义）
_ADDITIVE_AGGS = frozenset({"count", "sum"})

# 不同图表使用的格式适配器名（bi-detail = 表单明细行透传，不走桶聚合）
FORMATS = frozenset({
    "raw",
    "echarts-line", "echarts-bar", "echarts-pie", "echarts-heatmap",
    "datav-number", "table", "bi-detail",
    # 2026-09-25 图表库扩充（sankey/graph/flow 共用 links 中间结构）
    "echarts-scatter", "echarts-radar", "echarts-sankey", "echarts-graph",
    "echarts-flow", "echarts-tree", "echarts-funnel", "echarts-gauge",
    "echarts-bar3d",
})


def _label_of(measure: dict[str, Any]) -> str:
    return str(measure.get("label") or measure.get("key") or "")


def _fmt_time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _dim_label(dim: dict[str, Any]) -> str:
    return str(dim.get("name") or dim.get("field") or "")


def _dim_key(dimensions: dict[str, Any]) -> str:
    """维度组合的展示键（无序值拼接，供 series/矩阵分组）"""
    if not dimensions:
        return ""
    return "·".join(str(v) for v in dimensions.values())


def build_intermediate(defn: dict[str, Any], buckets: list[dict[str, Any]]) -> dict:
    """把预聚合桶列表转成统一中间格式

    buckets: [{"bucket_start": datetime, "dimensions": {...}, "values": {...}}, ...]
             调用方需按 bucket_start 升序传入。
    """
    chart_type = defn.get("chart_type", "line")
    kind = _CHART_KIND.get(chart_type, "timeseries")
    measures = defn.get("measures") or []
    dims = defn.get("dimensions") or []

    intermediate: dict[str, Any] = {
        "metric_key": defn.get("metric_key"),
        "name": defn.get("name"),
        "chart_type": chart_type,
        "type": kind,
        "dimensions": [{"name": _dim_label(d)} for d in dims],
        "measures": [{"key": m.get("key"), "label": _label_of(m)} for m in measures],
        "meta": {
            "bucket_count": len(buckets),
            "truncated": any(b.get("is_others") for b in buckets),
        },
    }

    if kind == "scalar":
        latest = buckets[-1] if buckets else None
        primary = measures[0]["key"] if measures else None
        intermediate["value"] = (latest["values"].get(primary)
                                 if latest and primary else None)
        # 单位约定放 style.unit（style 为自由 JSON，无需模型迁移）；
        # defn["unit"] 仅作历史兼容读取
        intermediate["unit"] = ((defn.get("style") or {}).get("unit")
                                or defn.get("unit", ""))
        intermediate["values"] = latest["values"] if latest else {}
        return intermediate

    if kind == "records":
        intermediate["columns"] = (
            [{"title": _dim_label(d), "dataIndex": _dim_label(d)} for d in dims]
            + [{"title": "时间", "dataIndex": "bucket"}]
            + [{"title": _label_of(m), "dataIndex": m["key"]} for m in measures]
        )
        intermediate["rows"] = []
        for b in buckets:
            row: dict[str, Any] = {"bucket": _fmt_time(b["bucket_start"])}
            row.update(b.get("dimensions") or {})
            row.update(b.get("values") or {})
            intermediate["rows"].append(row)
        return intermediate

    if kind == "distribution":
        # 分布图：滚动源的可加算子（count/sum）在**整个区间**上求和
        #（"最近 24h 各类别占比"）；快照源（form）每个桶都是同一份全表
        # 状态，跨桶求和会翻倍——只取最新桶。不可加算子（avg/p95 等）
        # 无法跨桶合并，一律取最新桶（诚实，不编造）。
        primary_measure = measures[0] if measures else None
        primary = primary_measure["key"] if primary_measure else None
        additive = bool(primary_measure
                        and primary_measure.get("agg") in _ADDITIVE_AGGS)
        snapshot = defn.get("source_type") == "form"
        latest_ts = max((b["bucket_start"] for b in buckets), default=None)
        merged: dict[str, Any] = {}
        for b in buckets:
            if (not additive or snapshot) and b["bucket_start"] != latest_ts:
                continue
            name = _dim_key(b.get("dimensions") or {}) or "合计"
            value = (b.get("values") or {}).get(primary)
            if isinstance(value, (int, float)):
                merged[name] = merged.get(name, 0) + value
            elif value is not None:
                merged[name] = value
        items = [{"name": k, "value": v} for k, v in merged.items()]
        intermediate["items"] = items
        intermediate["value"] = sum(
            i["value"] for i in items if isinstance(i["value"], (int, float)))
        return intermediate

    if kind == "category":
        # 类别对比：x = 维度组合值，series = 每个度量一条
        # 快照源（form）只取最新桶（最新快照即当前状态）；滚动源可加算子
        # 跨桶求和（区间汇总）、不可加取最新桶。输出结构与 timeseries 一致
        # （x + series[{key,label,data}]），to_echarts 复用同一分支。
        snapshot = defn.get("source_type") == "form"
        latest_ts = max((b["bucket_start"] for b in buckets), default=None)
        order: list[str] = []
        cells: dict[str, dict[str, Any]] = {}
        for b in buckets:
            if snapshot and b["bucket_start"] != latest_ts:
                continue
            name = _dim_key(b.get("dimensions") or {}) or "合计"
            if name not in cells:
                cells[name] = {}
                order.append(name)
            for m in measures:
                key = m["key"]
                v = (b.get("values") or {}).get(key)
                if v is None:
                    continue
                if not snapshot and m.get("agg") in _ADDITIVE_AGGS:
                    cells[name][key] = cells[name].get(key, 0) + v
                elif b["bucket_start"] == latest_ts:
                    cells[name][key] = v
        intermediate["x"] = order
        intermediate["series"] = [
            {"key": m["key"], "label": _label_of(m),
             "data": [cells[name].get(m["key"]) for name in order]}
            for m in measures
        ]
        return _apply_display(intermediate, defn)

    if kind == "matrix":
        # 矩阵（热力）：x = 时间桶，y = 维度值，value = 主度量
        primary = measures[0]["key"] if measures else None
        xs = sorted({b["bucket_start"] for b in buckets})
        ys: list[str] = []
        cells: list[list[Any]] = []
        for b in buckets:
            y = _dim_key(b.get("dimensions") or {}) or "合计"
            if y not in ys:
                ys.append(y)
            value = (b.get("values") or {}).get(primary)
            cells.append([_fmt_time(b["bucket_start"]), y,
                          value if value is not None else 0])
        intermediate["x"] = [_fmt_time(t) for t in xs]
        intermediate["y"] = ys
        intermediate["cells"] = cells
        return intermediate

    if kind == "pairs":
        # 散点：系列 = 维度组合，点 = 跨桶的 (度量1, 度量2) 对
        # 快照源只取最新桶（每桶是同一份状态的重复点，无需叠画）
        snapshot = defn.get("source_type") == "form"
        latest_ts = max((b["bucket_start"] for b in buckets), default=None)
        mx, my = (measures + [None, None])[:2]
        point_map: dict[str, list[list[float]]] = {}
        for b in buckets:
            if snapshot and b["bucket_start"] != latest_ts:
                continue
            name = _dim_key(b.get("dimensions") or {}) or "全部"
            x = (b.get("values") or {}).get(mx["key"]) if mx else None
            y = (b.get("values") or {}).get(my["key"]) if my else None
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                point_map.setdefault(name, []).append([x, y])
        intermediate["series"] = [
            {"name": k, "data": v} for k, v in point_map.items()]
        return intermediate

    if kind == "radar":
        # 雷达：指标轴 = 度量列表；系列 = 最新桶的各维度组合（当前状态对比）
        latest_ts = max((b["bucket_start"] for b in buckets), default=None)
        series = [
            {
                "name": _dim_key(b.get("dimensions") or {}) or "全部",
                "values": [(b.get("values") or {}).get(m["key"])
                           for m in measures],
            }
            for b in buckets if b["bucket_start"] == latest_ts
        ]
        intermediate["indicators"] = [{"name": _label_of(m)} for m in measures]
        intermediate["series"] = series
        return intermediate

    if kind == "links":
        # 关系/流向：dims[0] → dims[1]，值 = 主度量
        # （滚动源可加算子=整个区间求和；快照源/不可加=取最新桶，与分布图口径一致）
        dim_names = [d.get("name") for d in (defn.get("dimensions") or [])]
        source_name, target_name = (dim_names + [None, None])[:2]
        primary = measures[0]["key"] if measures else None
        additive = bool(measures and measures[0].get("agg") in _ADDITIVE_AGGS)
        snapshot = defn.get("source_type") == "form"
        latest_ts = max((b["bucket_start"] for b in buckets), default=None)
        acc: dict[tuple[str, str], float] = {}
        for b in buckets:
            if (not additive or snapshot) and b["bucket_start"] != latest_ts:
                continue
            dims_map = b.get("dimensions") or {}
            s, t = dims_map.get(source_name), dims_map.get(target_name)
            v = (b.get("values") or {}).get(primary)
            if s is None or t is None or not isinstance(v, (int, float)):
                continue
            key = (str(s), str(t))
            acc[key] = (acc.get(key, 0) + v) if additive else v
        nodes: list[dict[str, Any]] = []
        seen: set[str] = set()
        links: list[dict[str, Any]] = []
        for (s, t), v in acc.items():
            if s == t:
                continue  # Top N「其他」归并会产生自环，桑基要求 DAG，丢弃
            for n in (s, t):
                if n not in seen:
                    seen.add(n)
                    nodes.append({"name": n})
            links.append({"source": s, "target": t, "value": v})
        intermediate["nodes"] = nodes
        intermediate["links"] = links
        return intermediate

    if kind == "tree":
        # 树：维度按序成层级，叶子值 = 主度量（滚动源可加=区间求和；
        # 快照源/不可加=最新桶，与分布图口径一致）
        primary = measures[0]["key"] if measures else None
        additive = bool(measures and measures[0].get("agg") in _ADDITIVE_AGGS)
        snapshot = defn.get("source_type") == "form"
        latest_ts = max((b["bucket_start"] for b in buckets), default=None)
        dim_names = [d.get("name") for d in (defn.get("dimensions") or [])]
        leaf_acc: dict[tuple[str, ...], float] = {}
        for b in buckets:
            if (not additive or snapshot) and b["bucket_start"] != latest_ts:
                continue
            dims_map = b.get("dimensions") or {}
            path = tuple(str(dims_map.get(n)) for n in dim_names)
            v = (b.get("values") or {}).get(primary)
            if any(p == "None" for p in path) or not isinstance(v, (int, float)):
                continue
            leaf_acc[path] = (leaf_acc.get(path, 0) + v) if additive else v

        nested: dict[str, Any] = {}
        for path, v in leaf_acc.items():
            cur = nested
            for p in path[:-1]:
                cur = cur.setdefault(p, {})
            cur[path[-1]] = v

        def _build(level_map: dict[str, Any], depth: int) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for k, sub in level_map.items():
                if depth == len(dim_names) - 1:
                    out.append({"name": k, "value": sub})
                else:
                    out.append({"name": k, "children": _build(sub, depth + 1)})
            return out

        intermediate["tree"] = {
            "name": defn.get("name") or "全部",
            "children": _build(nested, 0) if nested else [],
        }
        return intermediate

    # timeseries（默认）
    xs = sorted({b["bucket_start"] for b in buckets})
    x_index = {t: i for i, t in enumerate(xs)}
    series_map: dict[str, dict[str, Any]] = {}

    for b in buckets:
        dim_key = _dim_key(b.get("dimensions") or {})
        for m in measures:
            key = f"{dim_key}·{m['key']}" if dim_key else m["key"]
            entry = series_map.setdefault(key, {
                "key": key,
                "label": (f"{dim_key} {_label_of(m)}" if dim_key else _label_of(m)),
                "measure": m["key"],
                "dimension": dim_key,
                "data": [None] * len(xs),
            })
            entry["data"][x_index[b["bucket_start"]]] = (b.get("values") or {}).get(m["key"])

    intermediate["x"] = [_fmt_time(t) for t in xs]
    intermediate["series"] = list(series_map.values())
    return _apply_display(intermediate, defn)


def _apply_display(intermediate: dict, defn: dict[str, Any]) -> dict:
    """展示层修饰（add-bi-calc-fields）：占比 / 累计

    - percent：每个系列各自换算为占总和的百分比（保留 1 位）；总和非正时置 None
    - cumulative：按 x 顺序累计（cumsum；非数值置 None）
    仅作用于 series 结构（line/bar/time_line/category）；饼图占比由前端标签呈现。
    """
    display = defn.get("display") or {}
    series = intermediate.get("series") or []
    if display.get("percent"):
        for s in series:
            data = s.get("data") or []
            total = sum(v for v in data if isinstance(v, (int, float)))
            if total:
                s["data"] = [
                    round(v / total * 100, 1) if isinstance(v, (int, float)) else None
                    for v in data
                ]
        intermediate["unit"] = "%"
    if display.get("cumulative"):
        for s in series:
            acc = 0.0
            out: list[Any] = []
            for v in s.get("data") or []:
                if isinstance(v, (int, float)):
                    acc += v
                    out.append(acc)
                else:
                    out.append(None)
            s["data"] = out
    return intermediate


# ============================================================
# 适配器：中间格式 → 各前端结构
# ============================================================

def to_echarts(intermediate: dict, chart_type: str) -> dict:
    """中间格式 → ECharts option 数据片段（line/bar/pie/heatmap）"""
    if chart_type in ("line", "bar"):
        return {
            "xAxis": {"type": "category", "data": intermediate.get("x", [])},
            "series": [
                {"name": s["label"], "type": chart_type, "data": s["data"]}
                for s in intermediate.get("series", [])
            ],
        }
    if chart_type == "pie":
        return {"series": [{"type": "pie", "data": intermediate.get("items", [])}]}
    if chart_type == "heatmap":
        return {
            "xAxis": {"type": "category", "data": intermediate.get("x", [])},
            "yAxis": {"type": "category", "data": intermediate.get("y", [])},
            "series": [{"type": "heatmap", "data": intermediate.get("cells", [])}],
        }
    raise ValueError(f"不支持的 ECharts 图表类型: {chart_type}")


def to_table(intermediate: dict) -> dict:
    """中间格式 → 表格结构"""
    if intermediate.get("type") == "records":
        return {"columns": intermediate.get("columns", []),
                "rows": intermediate.get("rows", [])}
    # 非 records 类型：退化为 时间 × 度量 表
    columns = [{"title": "时间", "dataIndex": "bucket"}] + [
        {"title": m["label"], "dataIndex": m["key"]}
        for m in intermediate.get("measures", [])]
    times = intermediate.get("x", [])
    series = intermediate.get("series", [])
    rows = []
    for i, t in enumerate(times):
        row: dict[str, Any] = {"bucket": t}
        for s in series:
            row[s["key"]] = s["data"][i] if i < len(s["data"]) else None
        rows.append(row)
    return {"columns": columns, "rows": rows}


def to_datav_number(intermediate: dict) -> dict:
    """中间格式 → 大屏数字翻牌结构"""
    return {
        "value": intermediate.get("value"),
        "unit": intermediate.get("unit", ""),
        "values": intermediate.get("values", {}),
    }


def convert(defn: dict[str, Any], buckets: list[dict[str, Any]],
            fmt: str = "raw") -> Any:
    """统一入口：按 format 输出

    fmt: raw | echarts-line | echarts-bar | echarts-pie | echarts-heatmap
         | datav-number | table
    """
    if fmt not in FORMATS:
        raise ValueError(f"不支持的格式: {fmt}；可用: {sorted(FORMATS)}")

    intermediate = build_intermediate(defn, buckets)
    if fmt == "raw":
        return intermediate
    if fmt == "table":
        return to_table(intermediate)
    if fmt == "datav-number":
        return to_datav_number(intermediate)
    if fmt == "echarts-bar3d":
        # 3D 矩阵：与热力同源三元组，前端拼 grid3D
        return {"x": intermediate.get("x", []),
                "y": intermediate.get("y", []),
                "cells": intermediate.get("cells", [])}
    if fmt == "echarts-funnel":
        items = sorted(
            intermediate.get("items", []),
            key=lambda i: i["value"] if isinstance(i.get("value"), (int, float)) else 0,
            reverse=True)
        return {"items": items}
    if fmt == "echarts-gauge":
        return to_datav_number(intermediate)
    if fmt in ("echarts-scatter", "echarts-radar", "echarts-sankey",
               "echarts-graph", "echarts-flow", "echarts-tree"):
        # 中间结构即前端所需（series/indicators/nodes+links/tree），前端拼 option
        return intermediate
    # echarts-*
    chart_type = fmt.split("-", 1)[1]
    return to_echarts(intermediate, chart_type)
