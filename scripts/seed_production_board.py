"""生成「生产管理看板」：生产测试库 → BI 指标定义 → 聚合 → 数值对照 → 看板布局

用法（在包根目录）：
    runtime\\python.exe scripts/seed_production_board.py                # 生成（幂等）
    runtime\\python.exe scripts/seed_production_board.py --check        # 只做数值对照

环境变量：APIBRIDGE_SERVER（默认 http://127.0.0.1:8124）、APIBRIDGE_ADMIN_TOKEN
         APIBRIDGE_PROD_DB（源库路径；未设置时拒绝执行，不提供机器默认值）
幂等：连接/指标/看板按键复用；重复执行只更新定义与布局。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import urllib.error
import urllib.request

SERVER = os.environ.get("APIBRIDGE_SERVER", "http://127.0.0.1:8124").rstrip("/")
TOKEN = os.environ.get("APIBRIDGE_ADMIN_TOKEN", "wt-admin-token")
PROD_DB = os.environ.get("APIBRIDGE_PROD_DB")
if not PROD_DB:
    import sys
    sys.exit("需要环境变量 APIBRIDGE_PROD_DB（生产源库 SQLite 路径）")
CONN_NAME = "生产测试库"
BOARD_KEY = "production"
BOARD_NAME = "生产管理看板"

API = f"{SERVER}/api/v1"


def call(method: str, path: str, payload=None, *, auth=True):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["X-Admin-Token"] = TOKEN
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"{method} {path} -> {exc.code}: {detail[:400]}") from None


def ref(conn_id: str, table: str) -> str:
    return json.dumps({"connection_id": conn_id, "form_key": table})


# ============================================================
# 指标定义（17 张卡：KPI 条 / 趋势层 / 状态层 / 分析层 / 明细层）
# ============================================================

def build_defs(cid: str) -> list[dict]:
    def metric(key, name, table, *, chart="bar", dims=(), measures=(), computed=(),
               display=None, style=None, top_n=100):
        d = {"metric_key": key, "name": name, "source_type": "form",
             "source_ref": ref(cid, table), "granularity": "1min", "chart_type": chart,
             "dimensions": list(dims), "measures": list(measures), "top_n": top_n,
             "enabled": True}
        if computed:
            d["computed"] = list(computed)
        if display:
            d["display"] = display
        if style:
            d["style"] = style
        return d

    def dim(name, field, **extra):
        return {"name": name, "field": field, **extra}

    def meas(key, agg, field=None, ftype="number", **extra):
        m = {"key": key, "agg": agg}
        if field is not None:
            m["field"] = field
        if agg != "count":
            m["field_type"] = ftype
        m.update(extra)
        return m

    days = {"field": "date", "op": "between", "value": ["2026-09-21", "2026-09-27"],
            "field_type": "string"}

    return [
        # ---- KPI 条（统一限定期内 2026-09-21 ~ 09-27）----
        {**metric("kpi_plan", "计划产量", "production_output", chart="number",
                  measures=[meas("计划产量", "sum", "plan_qty")]), "filters": [days]},
        {**metric("kpi_actual", "实际产量", "production_output", chart="number",
                  measures=[meas("实际产量", "sum", "actual_qty")]), "filters": [days]},
        metric("kpi_attain", "整体达成率", "production_output", chart="number",
               measures=[meas("整体达成率", "ratio", numerator={"field": "actual_qty", "agg": "sum"},
                              denominator={"field": "plan_qty", "agg": "sum"}, scale=100)],
               style={"unit": "%"}),
        metric("kpi_pass_rate", "质量合格率", "quality_records", chart="number",
               measures=[meas("质量合格率", "ratio", numerator={"field": "passed", "agg": "sum"},
                              denominator={"field": "inspected", "agg": "sum"}, scale=100)],
               style={"unit": "%"}),
        metric("kpi_utilization", "设备综合利用率", "equipment_daily", chart="number",
               measures=[meas("利用率", "avg", "utilization")], style={"unit": "%"}),
        metric("kpi_attendance", "出勤率", "personnel_attendance", chart="number",
               measures=[meas("出勤率", "avg", "attendance_rate")], style={"unit": "%"}),

        # ---- 趋势层 ----
        # 语义提醒：引擎里 line/time_line 恒为"桶时间轴"（时间推移靠滚动源）；
        # 快照源（form）只算最新窗口，按业务日期看走势要用 bar（x = 日期维度值）
        metric("trend_output", "各日产量对比（计划 vs 实际）", "production_output",
               chart="bar", dims=[dim("日期", "date")],
               measures=[meas("计划", "sum", "plan_qty"), meas("实际", "sum", "actual_qty")]),
        metric("line_output_cmp", "各产线产量对比", "production_output",
               dims=[dim("产线", "line")],
               measures=[meas("计划", "sum", "plan_qty"), meas("实际", "sum", "actual_qty")]),

        # ---- 状态层 ----
        metric("line_attain", "各产线达成率", "production_output",
               dims=[dim("产线", "line")],
               measures=[meas("平均达成率", "avg", "attain_rate")], style={"unit": "%"}),
        metric("equipment_downtime", "设备停机小时", "equipment_daily",
               dims=[dim("设备", "equipment")],
               measures=[meas("停机小时", "sum", "downtime_hours")]),
        metric("abnormal_type", "异常类型分布", "abnormal_events", chart="pie",
               dims=[dim("类型", "etype")], measures=[meas("条数", "count", "description")]),
        metric("team_attendance", "班组出勤率", "personnel_attendance",
               dims=[dim("班组", "team")],
               measures=[meas("平均出勤率", "avg", "attendance_rate")], style={"unit": "%"}),

        # ---- 分析层 ----
        metric("output_share", "各产线产量占比", "production_output",
               dims=[dim("产线", "line")], measures=[meas("实际产量", "sum", "actual_qty")],
               display={"percent": True}, style={"unit": "%"}),
        metric("defect_by_line", "各产线不良数（检验口径）", "quality_records",
               dims=[dim("产线", "line")], measures=[meas("不良数", "sum", "defective")]),
        metric("workshop_output", "各车间产量（自定义分组）", "production_output",
               dims=[dim("车间", "line",
                         groups=[{"name": "一车间", "values": ["A线", "B线"]},
                                 {"name": "二车间", "values": ["C线"]}], other="其他")],
               measures=[meas("实际产量", "sum", "actual_qty")]),
        metric("bigtable_pushdown", "百万行表聚合（下推）", "pushdown_bench",
               dims=[dim("产线", "line")], measures=[meas("实际产量", "sum", "actual_qty")]),

        # ---- 明细层 ----
        metric("prod_detail", "生产明细", "production_output", chart="detail"),
    ]


LAYOUT = {
    # KPI 条
    "kpi_plan": {"x": 0, "y": 0, "w": 12, "h": 5},
    "kpi_actual": {"x": 12, "y": 0, "w": 12, "h": 5},
    "kpi_attain": {"x": 24, "y": 0, "w": 12, "h": 5},
    "kpi_pass_rate": {"x": 36, "y": 0, "w": 12, "h": 5},
    "kpi_utilization": {"x": 48, "y": 0, "w": 12, "h": 5},
    "kpi_attendance": {"x": 60, "y": 0, "w": 12, "h": 5},
    # 趋势层
    "trend_output": {"x": 0, "y": 5, "w": 36, "h": 8},
    "line_output_cmp": {"x": 36, "y": 5, "w": 36, "h": 8},
    # 状态层
    "line_attain": {"x": 0, "y": 13, "w": 18, "h": 8},
    "equipment_downtime": {"x": 18, "y": 13, "w": 18, "h": 8},
    "abnormal_type": {"x": 36, "y": 13, "w": 18, "h": 8},
    "team_attendance": {"x": 54, "y": 13, "w": 18, "h": 8},
    # 分析层
    "output_share": {"x": 0, "y": 21, "w": 18, "h": 8},
    "defect_by_line": {"x": 18, "y": 21, "w": 18, "h": 8},
    "workshop_output": {"x": 36, "y": 21, "w": 18, "h": 8},
    "bigtable_pushdown": {"x": 54, "y": 21, "w": 18, "h": 8},
    # 明细层
    "prod_detail": {"x": 0, "y": 29, "w": 72, "h": 7},
}


# ============================================================
# 数值对照基准（直查源库，不由 BI 生成）
# ============================================================

def sql_expected() -> dict:
    c = sqlite3.connect(PROD_DB)
    q = lambda sql: c.execute(sql).fetchone()
    span = "where date between '2026-09-21' and '2026-09-27'"
    plan, actual = q(f"select sum(plan_qty), sum(actual_qty) from production_output {span}")
    passed, inspected = q("select sum(passed), sum(inspected) from quality_records")
    util = q("select avg(utilization) from equipment_daily")[0]
    att = q("select avg(attendance_rate) from personnel_attendance")[0]
    by_line = {r[0]: (float(r[1]), float(r[2])) for r in c.execute(
        f"select line, sum(plan_qty), sum(actual_qty) from production_output {span} group by line")}
    by_date = dict((d, (p, a)) for d, p, a in c.execute(
        f"select date, sum(plan_qty), sum(actual_qty) from production_output {span} group by date"))
    attain_line = dict(c.execute(
        f"select line, avg(attain_rate) from production_output {span} group by line"))
    downtime = dict(c.execute("select equipment, sum(downtime_hours) from equipment_daily group by equipment"))
    etype = dict(c.execute("select etype, count(*) from abnormal_events group by etype"))
    team_att = dict(c.execute("select team, avg(attendance_rate) from personnel_attendance group by team"))
    defect = {r[0]: float(r[1]) for r in c.execute(
        "select line, sum(defective) from quality_records group by line")}
    big = dict(c.execute("select line, sum(actual_qty) from pushdown_bench group by line"))
    detail_rows = q(f"select count(*) from production_output {span}")[0]
    c.close()
    share = {k: round(v[1] / actual * 100, 1) for k, v in by_line.items()}
    workshop = {"一车间": by_line["A线"][1] + by_line["B线"][1], "二车间": by_line["C线"][1]}
    return {
        "kpi_plan": float(plan), "kpi_actual": float(actual),
        "kpi_attain": round(actual / plan * 100, 2),
        "kpi_pass_rate": round(passed / inspected * 100, 2),
        "kpi_utilization": round(util, 2), "kpi_attendance": round(att, 2),
        "line_output_cmp": {k: (float(p), float(a)) for k, (p, a) in by_line.items()},
        "trend_output": {k: (float(p), float(a)) for k, (p, a) in by_date.items()},
        "line_attain": {k: round(v, 2) for k, v in attain_line.items()},
        "equipment_downtime": {k: float(v) for k, v in downtime.items()},
        "abnormal_type": {k: int(v) for k, v in etype.items()},
        "team_attendance": {k: round(v, 2) for k, v in team_att.items()},
        "output_share": share,
        "defect_by_line": {k: float(v) for k, v in defect.items()},
        "workshop_output": {k: float(v) for k, v in workshop.items()},
        "bigtable_pushdown": {k: float(v) for k, v in big.items()},
        "prod_detail_rows": int(detail_rows),
    }


def fetch_chart(key: str) -> dict:
    """取图数据（format=raw：number→value / 类目→xAxis+series / 饼→series[].data / 明细→rows）"""
    return call("GET", f"/bi/metrics/{key}?format=raw")["data"]


def _scalar(data: dict) -> float:
    return float(data["value"] if "value" in data else data["values"].popitem()[1])


def _series_map(data: dict) -> dict:
    """{x: {series名: 值}}"""
    xs = data.get("x") or (data.get("xAxis") or {}).get("data") or []
    out: dict[str, dict] = {}
    for s in data.get("series", []):
        for x, y in zip(xs, s.get("data", [])):
            out.setdefault(str(x), {})[s.get("name") or s.get("label")] = y
    return out


def _pie_map(data: dict) -> dict:
    if isinstance(data.get("items"), list):
        return {i["name"]: i["value"] for i in data["items"]}
    for s in data.get("series", []):
        if s.get("type") == "pie":
            return {i["name"]: i["value"] for i in s["data"]}
    return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只跑数值对照")
    args = ap.parse_args()

    expected = sql_expected()
    board_key = BOARD_KEY

    if not args.check:
        # ① 连接（幂等：同名复用，否则创建）
        conns = call("GET", "/connections")
        conn = next((c for c in conns if c["name"] == CONN_NAME), None)
        if conn is None:
            conn = call("POST", "/connections", {
                "name": CONN_NAME, "connection_type": "source", "protocol": "sqlite",
                "db_type": "sqlite", "db_config": {"database": PROD_DB}, "status": "active"})
            print(f"连接已创建: {conn['id']}")
        else:
            print(f"复用连接: {conn['id']}")
        cid = conn["id"]
        # 连接可用性先验证（失败早退出）
        test = call("POST", f"/connections/{cid}/test")
        print(f"连接测试: success={test.get('success')} {test.get('message','')}")

        # ② 表单注册（可选但便于界面选择）
        try:
            sync = call("POST", f"/forms/{cid}/sync")
            print(f"表单注册: 新增 {sync['added']} / 共 {sync['total']}")
        except SystemExit as exc:
            print(f"表单注册跳过: {exc}")

        # ③ 指标定义（按 key 幂等：存在则 PUT 覆盖，否则 POST）
        existing = {m["metric_key"] for m in call("GET", "/bi/metrics")}
        defs = build_defs(cid)
        for d in defs:
            if d["metric_key"] in existing:
                call("PUT", f"/bi/metrics/{d['metric_key']}", d)
            else:
                call("POST", "/bi/metrics", d)
        print(f"指标定义: {len(defs)} 张卡已就绪")

        # ④ 聚合（全部启用指标）
        result = call("POST", "/bi/aggregate/run")
        errs = [r for r in result["results"] if r.get("error")]
        print(f"聚合: {result['metrics']} 个指标" + (f"（失败 {len(errs)}）" if errs else "，全部成功"))
        for e in errs:
            print("   失败:", e)

        # ⑤ 看板 + 布局（固定 key=production；按 key 幂等复用）
        screens = call("GET", "/bi/screens")
        board = next((s for s in screens if s.get("key") == BOARD_KEY), None)
        if board is None:
            board = call("POST", "/bi/screens", {"key": BOARD_KEY, "name": BOARD_NAME})
            print(f"看板已创建: {board['key']}")
        board_key = board["key"]
        call("PUT", f"/bi/screen-settings?board={board_key}", {
            "name": BOARD_NAME, "layout": {}, "filter_dims": ["产线"],
            "cells": LAYOUT, "base_w": 1920, "base_h": 1080,
            "screen_adaptor": "keep", "theme": "dark"})
        print(f"看板布局: {len(LAYOUT)} 张卡已摆放（board={board_key}）")

    # ⑥ 数值对照（BI 结果 vs 源库直查）
    print("\n=== 数值对照（BI vs 源库直查）===")
    checks: list[tuple[str, object, object]] = []

    def kpi(key: str) -> float:
        return _scalar(fetch_chart(key))

    checks.append(("计划产量", kpi("kpi_plan"), expected["kpi_plan"]))
    checks.append(("实际产量", kpi("kpi_actual"), expected["kpi_actual"]))
    checks.append(("整体达成率%", round(kpi("kpi_attain"), 2), expected["kpi_attain"]))
    checks.append(("质量合格率%", round(kpi("kpi_pass_rate"), 2), expected["kpi_pass_rate"]))
    checks.append(("设备利用率%", round(kpi("kpi_utilization"), 2), expected["kpi_utilization"]))
    checks.append(("出勤率%", round(kpi("kpi_attendance"), 2), expected["kpi_attendance"]))

    for label, key, series_name, want in [
        ("各产线实际产量", "line_output_cmp", "实际",
         {k: v[1] for k, v in expected["line_output_cmp"].items()}),
        ("各产线平均达成率", "line_attain", "平均达成率", expected["line_attain"]),
        ("班组出勤率", "team_attendance", "平均出勤率", expected["team_attendance"]),
        ("各产线不良数", "defect_by_line", "不良数", expected["defect_by_line"]),
        ("百万行表按产线（下推）", "bigtable_pushdown", "实际产量", expected["bigtable_pushdown"]),
        ("各产线占比%", "output_share", "实际产量", expected["output_share"]),
    ]:
        got = {k: v.get(series_name) for k, v in _series_map(fetch_chart(key)).items()}
        checks.append((label, got, want))

    # 业务日趋势：维度是派生标签（09-21…），对照时按后 5 位对齐
    trend_got = {k[-5:] if len(str(k)) == 10 else str(k): v.get("实际")
                 for k, v in _series_map(fetch_chart("trend_output")).items()}
    checks.append(("产量趋势逐日实际", trend_got,
                   {k[-5:]: v[1] for k, v in expected["trend_output"].items()}))

    checks.append(("车间分组产量", {k: v.get("实际产量") for k, v in
                                _series_map(fetch_chart("workshop_output")).items()},
                   expected["workshop_output"]))
    checks.append(("异常类型条数", _pie_map(fetch_chart("abnormal_type")),
                   expected["abnormal_type"]))
    detail_rows = fetch_chart("prod_detail").get("rows") or []
    checks.append(("生产明细行数", len(detail_rows), expected["prod_detail_rows"]))

    ok = True
    for label, got, want in checks:
        if isinstance(got, float) and isinstance(want, float):
            good = abs(got - want) < 0.02
        elif isinstance(got, dict) and isinstance(want, dict):
            good = got.keys() == want.keys() and all(
                (abs(float(got[k]) - float(want[k])) < 0.02)
                if isinstance(got[k], (int, float)) else got[k] == want[k] for k in want)
        else:
            good = got == want
        ok &= bool(good)
        print(f"  {'OK  ' if good else 'FAIL'} {label}: BI={got}  SQL={want}")

    print(f"\n看板地址: {SERVER}/bi/screen?board={board_key}")
    print(f"管理入口: {SERVER}/bi （看板下拉选「{BOARD_NAME}」）")
    print("数值对照:", "全部通过" if ok else "存在不一致，见上表")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
