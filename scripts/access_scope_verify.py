# -*- coding: utf-8 -*-
"""BI 访问授权真实数据验证（add-bi-access-scope）

用**生产测试库**（DB 源，`data/production_test.db`）验证三条取值路径在受限令牌下
都按行过滤且数值与 SQL 直查一致：

1. 预聚合桶（`GET /bi/metrics/{key}`）：`line_output`（维度=产线）→ 只剩 A线；
2. 明细（`GET /bi/form-rows`）：每行 `line == A线`，且隐藏列不在响应里；
3. 即席（`POST /bi/adhoc/query`）：客户端传 `in [A线, B线]` 试图放宽 → 结果仍只有 A线。

只建一个临时授权档案（跑完删除），不动任何既有数据/定义。

用法: python scripts/access_scope_verify.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "production_test.db"
API = "http://127.0.0.1:8000/api/v1"
HDR = {"X-Admin-Token": "dev-admin-token"}
CONN_ID = "ccfc5207-17aa-4395-b1c3-7e3544c8a28e"     # 生产运行管理-测试库
TABLE = "production_output"
METRIC = "line_output"                                # 维度=产线，度量=计划/实际
LINE = "A线"
HIDDEN = "defect_qty"                                 # 明细里要隐藏的列
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def sql_scalar(sql: str, params: tuple = ()):
    conn = sqlite3.connect(str(DB))
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def main() -> int:
    print("=" * 64)
    print("BI 访问授权真实数据验证（生产测试库 + 受限令牌）")
    print("=" * 64)

    # ---------- 0. 建临时授权（行过滤 line=A线 + 隐藏 defect_qty） ----------
    r = requests.post(f"{API}/bi/access/profiles", headers=HDR, timeout=30, json={
        "name": "自审-一车间", "boards": [], "row_filters": {"line": LINE},
        "hidden_fields": [HIDDEN], "note": "access_scope_verify.py 临时档案"})
    if r.status_code != 201:
        print(f"[错误] 建授权失败 HTTP {r.status_code}: {r.text[:200]}")
        return 2
    profile = r.json()
    token, pkey = profile["token"], profile["key"]
    bi_hdr = {"X-BI-Token": token, "X-Admin-Token": ""}
    print(f"\n[0] 临时授权已建：{pkey} token={profile['token_prefix']}… "
          f"行过滤 line={LINE} 隐藏 {HIDDEN}")

    try:
        # ---------- 1. 预聚合桶 ----------
        print("\n[1] 预聚合桶（line_output：维度=产线）")
        # 聚合属写操作 → 管理令牌触发
        requests.post(f"{API}/bi/aggregate/run", headers=HDR, timeout=300,
                      params={"metric": METRIC})
        r = requests.get(f"{API}/bi/metrics/{METRIC}", headers=bi_hdr, timeout=60,
                         params={"format": "raw"})
        if r.status_code != 200:
            check("受限令牌可读该卡片", False, f"HTTP {r.status_code}: {r.text[:150]}")
            return 1
        data = r.json()["data"]
        # 该卡有两个度量（计划/实际）——按度量 key 取实际产量那条 series，
        # 否则会拿计划产量去跟 SUM(actual_qty) 比（首轮实测踩到：10500 vs 10577）
        series = next((s for s in (data.get("series") or [])
                       if s.get("key") == "实际产量"), (data.get("series") or [{}])[0])
        got = dict(zip(data.get("x") or [], series.get("data") or []))
        truth_a = sql_scalar(
            f"SELECT SUM(actual_qty) FROM {TABLE} WHERE line = ?", (LINE,))
        truth_all = sql_scalar(f"SELECT SUM(actual_qty) FROM {TABLE}")
        print(f"    受限视角: {json.dumps(got, ensure_ascii=False)}")
        print(f"    SQL: line={LINE} → {truth_a}；全表 → {truth_all}")
        check("桶只剩授权行（A线）", set(got) == {LINE}, str(set(got)))
        check("数值与 SQL 直查一致", abs((got.get(LINE) or 0) - truth_a) < 1e-9,
              f"{got.get(LINE)} vs {truth_a}")
        check("未泄露其它产线", all(k == LINE for k in got))

        # ---------- 2. 明细 ----------
        print("\n[2] 明细分页（每行 line=A线；隐藏列不出现）")
        r = requests.get(f"{API}/bi/form-rows", headers=bi_hdr, timeout=60, params={
            "connection_id": CONN_ID, "form_key": TABLE, "page": 1, "page_size": 200})
        body = r.json()
        rows = body.get("rows") or []
        count_a = sql_scalar(f"SELECT COUNT(*) FROM {TABLE} WHERE line = ?", (LINE,))
        count_all = sql_scalar(f"SELECT COUNT(*) FROM {TABLE}")
        print(f"    fallback={body.get('fallback')} total={body.get('total')} "
              f"（SQL: A线 {count_a} / 全表 {count_all}）")
        check("total = 授权行数", body.get("total") == count_a,
              f"{body.get('total')} vs {count_a}")
        check("行数 < 全表（确实过滤了）", (body.get("total") or 0) < count_all)
        check("每行都是 A线", all(x.get("line") == LINE for x in rows),
              f"{len(rows)} 行")
        check("隐藏列不在响应里", all(HIDDEN not in x for x in rows))
        check("标明过滤降级", body.get("fallback") == "row_permission")

        # ---------- 3. 即席：客户端无法放宽 ----------
        print("\n[3] 即席查询（客户端传 in[A线,B线] 试图放宽）")
        measures = [{"key": "实际产量", "agg": "sum", "field": "actual_qty",
                     "field_type": "number"}]
        r = requests.post(f"{API}/bi/adhoc/query", headers=bi_hdr, timeout=60, json={
            "connection_id": CONN_ID, "form_key": TABLE, "chart_type": "bar",
            "format": "raw",
            "dimensions": [{"name": "产线", "field": "line"}],
            "measures": measures,
            "filters": [{"field": "line", "op": "in", "value": ["A线", "B线"]}]})
        if r.status_code != 200:
            check("即席查询可执行", False, f"HTTP {r.status_code}: {r.text[:150]}")
        else:
            d = r.json()["data"]
            got2 = dict(zip(d.get("x") or [],
                            ((d.get("series") or [{}])[0].get("data") or [])))
            print(f"    结果: {json.dumps(got2, ensure_ascii=False)}（引擎 "
                  f"{r.json().get('engine')}）")
            check("授权值覆盖客户端过滤", set(got2) == {LINE}, str(set(got2)))
            check("即席数值与 SQL 一致",
                  abs((got2.get(LINE) or 0) - truth_a) < 1e-9)

        # ---------- 4. 隐藏字段被引用 → 403 ----------
        print("\n[4] 隐藏度量被引用")
        r = requests.post(f"{API}/bi/adhoc/query", headers=bi_hdr, timeout=60, json={
            "connection_id": CONN_ID, "form_key": TABLE, "chart_type": "bar",
            "dimensions": [{"name": "产线", "field": "line"}],
            "measures": [{"name": "不良", "key": "不良合计", "agg": "sum",
                          "field": HIDDEN, "field_type": "number"}]})
        code = (r.json().get("detail") or {}).get("code") if r.status_code == 403 else None
        check("引用隐藏字段 403", r.status_code == 403 and code == "hidden_field",
              f"HTTP {r.status_code} code={code}")

        # ---------- 5. 写端点越权 ----------
        print("\n[5] 越权写")
        r = requests.post(f"{API}/bi/aggregate/run", headers=bi_hdr, timeout=30,
                          params={"metric": METRIC})
        check("受限令牌不能触发聚合（403）", r.status_code == 403, f"HTTP {r.status_code}")
        r = requests.post(f"{API}/bi/access/profiles", headers=bi_hdr, timeout=30,
                          json={"name": "x"})
        check("受限令牌不能建授权（403）", r.status_code == 403, f"HTTP {r.status_code}")
    finally:
        requests.delete(f"{API}/bi/access/profiles/{pkey}", headers=HDR, timeout=30)
        left = [p for p in requests.get(f"{API}/bi/access/profiles", headers=HDR,
                                       timeout=30).json()
                if p["key"] == pkey]
        print(f"\n[9] 清理：临时档案已删除（残留 {len(left)}）")

    print("=" * 64)
    if FAILED:
        print(f"结果：{len(FAILED)} 项未通过")
        for f in FAILED:
            print("  -", f)
        return 1
    print("结果：全部通过 [OK]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
