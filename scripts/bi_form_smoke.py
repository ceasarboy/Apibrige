# -*- coding: utf-8 -*-
"""BI form 数据源真实端点冒烟（HAP 真实表单 + SQLite 测试表）

连接 / 表单 / 字段全部经管理 API 动态发现，脚本内不含任何真实凭据。
前置：后端 8000 已启动（smoke_case1.db，含明道云与四库测试连接）。

用法: python scripts/bi_form_smoke.py
"""
import json
import sys
import uuid

import requests

API = "http://127.0.0.1:8000/api/v1"
H = {"X-Admin-Token": "dev-admin-token"}
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def pick_target() -> tuple[dict, list[str]] | None:
    """找 HAP 连接及其全部注册表单（真实明道云端点）

    注册表里可能存在已删除的工作表（明道云侧用完即删），调用方需逐个尝试。
    """
    conns = requests.get(f"{API}/connections", headers=H, timeout=10).json()
    forms = requests.get(f"{API}/forms", headers=H, timeout=10).json()
    items = forms.get("items", forms) if isinstance(forms, dict) else forms
    for c in conns:
        if c.get("protocol") == "hap":
            keys = [x["form_key"] for x in items
                    if x.get("connection_id") == c["id"]]
            if keys:
                return c, keys
    return None


def pick_sqlite() -> tuple[dict, str] | None:
    conns = requests.get(f"{API}/connections", headers=H, timeout=10).json()
    forms = requests.get(f"{API}/forms", headers=H, timeout=10).json()
    items = forms.get("items", forms) if isinstance(forms, dict) else forms
    for c in conns:
        if c.get("db_type") == "sqlite":
            f = next((x for x in items if x.get("connection_id") == c["id"]), None)
            if f:
                return c, f["form_key"]
    return None


def discover_fields(conn_id: str, form_key: str) -> list[dict]:
    r = requests.get(f"{API}/bi/form-fields", headers=H,
                     params={"connection_id": conn_id, "form_key": form_key},
                     timeout=30)
    r.raise_for_status()
    return r.json().get("fields") or []


def make_def(key: str, name: str, conn_id: str, form_key: str,
             fields: list[dict], chart_type: str) -> dict:
    dims, measures = [], []
    if chart_type != "detail":
        dim = next((f for f in fields if f["bi_type"] == "dimension"), None)
        if dim:
            dims = [{"name": dim["name"], "field": dim["path"]}]
        num = next((f for f in fields if f["bi_type"] == "number"), None)
        if num:
            measures = [{"key": f"{num['path']}_sum", "agg": "sum",
                         "field": num["path"], "field_type": "number"}]
        else:
            measures = [{"key": "行数", "agg": "count", "field_type": "number"}]
    return {
        "metric_key": key, "name": name, "source_type": "form",
        "source_ref": json.dumps({"connection_id": conn_id,
                                  "form_key": form_key}),
        "granularity": "1min", "dimensions": dims, "measures": measures,
        "chart_type": chart_type, "top_n": 100, "enabled": True,
    }


def run_case(label: str, conn: dict, form_key: str, suffix: str,
             with_detail: bool = True) -> None:
    print("=" * 62)
    print(f"{label}: {conn['name']} / {form_key}")
    fields = discover_fields(conn["id"], form_key)
    check("form-fields 拉到字段", len(fields) > 0, f"{len(fields)} 个")
    check("字段带 bi_type 归一",
          all("bi_type" in f for f in fields),
          str({f["bi_type"] for f in fields}))

    keys = []
    tag = uuid.uuid4().hex[:6]
    for chart_type, name in (("bar", f"冒烟-聚合-{label}"),
                             ("detail", f"冒烟-明细-{label}")):
        if chart_type == "detail" and not with_detail:
            continue
        key = f"smoke_{suffix}_{chart_type}_{tag}"
        keys.append(key)
        r = requests.post(f"{API}/bi/metrics", headers=H,
                          json=make_def(key, name, conn["id"], form_key,
                                        fields, chart_type),
                          timeout=15)
        check(f"创建 {chart_type} 定义", r.status_code == 201,
              r.text[:120] if r.status_code != 201 else "")

    r = requests.post(f"{API}/bi/aggregate/run", headers=H,
                      params={"metric": keys[0]}, timeout=120)
    check("聚合执行", r.status_code == 200 and
          r.json()["results"][0].get("windows", 0) >= 1, r.text[:150])

    r = requests.get(f"{API}/bi/metrics/{keys[0]}", headers=H,
                     params={"format": "raw"}, timeout=15)
    body = r.json()
    check("聚合结果非空", body.get("bucket_count", 0) >= 1,
          f"buckets={body.get('bucket_count')}")

    if len(keys) > 1:
        r = requests.get(f"{API}/bi/metrics/{keys[1]}", headers=H,
                         params={"format": "bi-detail"}, timeout=30)
        rows = (r.json().get("data") or {}).get("rows") or []
        check("明细行非空（真实业务数据）", len(rows) > 0, f"{len(rows)} 行")

        r = requests.get(f"{API}/bi/form-rows", headers=H,
                         params={"connection_id": conn["id"],
                                 "form_key": form_key, "limit": 10},
                         timeout=30)
        check("form-rows 端点可用", r.status_code == 200 and
              len(r.json().get("rows") or []) > 0)

    for key in keys:
        requests.delete(f"{API}/bi/metrics/{key}", headers=H, timeout=10)
    print(f"  [清理] 已删除冒烟定义 {keys}")


def main() -> int:
    hap = pick_target()
    check("找到 HAP 连接与注册表单", hap is not None)
    if hap:
        conn, keys = hap
        # 注册表可能含已删除的工作表：逐个尝试，取第一个能拉到字段的
        for form_key in keys:
            try:
                if discover_fields(conn["id"], form_key):
                    run_case("明道云", conn, form_key, "hap")
                    break
            except requests.HTTPError:
                print(f"  [跳过] 表单 {form_key} 已失效（fields 拉取失败）")
        else:
            check("至少一个 HAP 表单可用", False, "全部注册表单 fields 拉取失败")

    lite = pick_sqlite()
    check("找到 SQLite 连接与注册表单", lite is not None)
    if lite:
        run_case("SQLite", lite[0], lite[1], "sqlite")

    print("=" * 62)
    if FAILED:
        print(f"结果：{len(FAILED)} 项失败")
        for f in FAILED:
            print("  -", f)
        return 1
    print("结果：全部通过 [OK]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
