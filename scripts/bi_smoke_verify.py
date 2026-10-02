"""BI 端到端冒烟验证（临时脚本，用完即删）

真实 HTTP 链路验证：
    1. 内置定义与候选维度
    2. 区间回填聚合
    3. 各图表格式查询（line/bar/pie/heatmap/table/number）
    4. 业务指标（JSON 维度 + sum 度量）全流程：创建 → 聚合 → 查询 → 删除
    5. 校验拒绝路径（4 维度 / 时间点 sum / 非法算子）

用法: python scripts/bi_smoke_verify.py
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "http://127.0.0.1:8000/api/v1"
TOKEN = "dev-admin-token"
FAILED = []


def call(method: str, path: str, params: dict | None = None, body=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Admin-Token", TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def window(minutes: int = 30):
    now = datetime.now(timezone.utc)
    return (now - timedelta(minutes=minutes)).isoformat(), now.isoformat()


def main() -> int:
    print("=" * 62)
    print("1. 内置指标定义与候选维度")
    status, defs = call("GET", "/bi/metrics")
    check("列出定义 200", status == 200, f"共 {len(defs)} 条")
    check("内置定义数为 10", len(defs) == 10)

    status, dims = call("GET", "/bi/dimensions")
    check("候选维度 8 项", status == 200 and len(dims) == 8)
    high = [d["name"] for d in dims if not d["usable_for_group"]]
    check("高基数维度标记正确", set(high) == {"物料", "计划号"}, str(high))

    print("=" * 62)
    print("2. 区间回填聚合")
    frm, to = window(30)
    status, res = call("POST", "/bi/aggregate/run",
                       {"from": frm, "to": to})
    check("手动聚合 200", status == 200)
    done = [r for r in res.get("results", []) if "error" not in r]
    errs = [r for r in res.get("results", []) if "error" in r]
    check("全部指标无错", not errs, f"{len(done)} 个指标完成，错误 {len(errs)}")
    if errs:
        print("        错误明细:", json.dumps(errs[:2], ensure_ascii=False)[:300])

    print("=" * 62)
    print("3. 各图表格式查询")
    cases = [
        ("sync_throughput", "echarts-line", lambda d: bool(d.get("xAxis")) and bool(d.get("series"))),
        ("task_health", "table", lambda d: bool(d.get("columns")) and bool(d.get("rows"))),
        ("latency_percentile", "echarts-line", lambda d: bool(d.get("series"))),
        ("connection_health", "table", lambda d: bool(d.get("rows"))),
        ("kpi_cards", "datav-number",
         lambda d: bool((d.get("values") or {}).get("total"))),
        ("error_top", "echarts-bar", lambda d: bool(d.get("series"))),
        ("task_success_heatmap", "echarts-heatmap",
         lambda d: bool(d.get("xAxis")) and bool(d.get("series"))),
    ]
    for key, fmt, ok_fn in cases:
        status, body = call("GET", f"/bi/metrics/{key}",
                            {"from": frm, "to": to, "format": fmt})
        data = body.get("data") if isinstance(body, dict) else None
        buckets = body.get("bucket_count", 0) if isinstance(body, dict) else 0
        check(f"{key} / {fmt}", status == 200 and ok_fn(data or {}),
              f"桶数={buckets}")

    print("=" * 62)
    print("4. 业务指标全流程（JSON 维度 + sum 度量）")
    biz = {
        "metric_key": "smoke_biz", "name": "冒烟业务汇总",
        "source_type": "data_buffer", "granularity": "5min", "chart_type": "pie",
        "dimensions": [{"name": "category", "field": "category", "code": "category"}],
        "measures": [{"key": "cnt", "agg": "count"},
                     {"key": "amount_sum", "agg": "sum", "field": "amount",
                      "field_type": "number"}],
        "top_n": 20, "enabled": True,
    }
    status, created = call("POST", "/bi/metrics", body=biz)
    check("创建业务指标 201", status == 201, str(created)[:120] if status != 201 else "")

    status, _ = call("POST", "/bi/aggregate/run",
                     {"metric": "smoke_biz", "from": frm, "to": to})
    check("业务指标聚合 200", status == 200)

    status, body = call("GET", "/bi/metrics/smoke_biz",
                        {"from": frm, "to": to, "format": "raw"})
    data = (body or {}).get("data") or {}
    items = data.get("items") or []
    total = sum(i["value"] for i in items if isinstance(i.get("value"), (int, float)))
    check("业务聚合有 4 个类别", status == 200 and len(items) == 4,
          f"items={[(i['name'], i['value']) for i in items][:4]}")
    check("类别计数合计 = 3000", total == 3000, f"实际 {total}")

    status, body = call("GET", "/bi/metrics/smoke_biz",
                        {"from": frm, "to": to, "format": "echarts-pie"})
    check("饼图格式", status == 200 and bool((body.get("data") or {}).get("series")))

    status, _ = call("DELETE", "/bi/metrics/smoke_biz")
    check("删除业务指标 200", status == 200)
    status, _ = call("GET", "/bi/metrics/smoke_biz")
    check("删除后 404", status == 404)

    print("=" * 62)
    print("5. 校验拒绝路径")
    bad_cases = [
        ("4 个维度", {**biz, "metric_key": "bad1",
                      "dimensions": [{"name": f"d{i}", "field": f"f{i}"} for i in range(4)]},
         "too_many_dimensions"),
        ("时间点 sum", {**biz, "metric_key": "bad2", "dimensions": [],
                        "measures": [{"key": "t", "agg": "sum", "field": "order_time",
                                      "field_type": "point_in_time"}]},
         "point_in_time_agg_forbidden"),
        ("非法算子 median", {**biz, "metric_key": "bad3", "dimensions": [],
                              "measures": [{"key": "x", "agg": "median", "field": "a"}]},
         "unsupported_agg"),
        ("非只读 SQL", {**biz, "metric_key": "bad4", "dimensions": [],
                        "source_type": "custom_sql", "source_ref": "DELETE FROM data_buffer",
                        "measures": [{"key": "c", "agg": "count"}]},
         "sql_not_read_only"),
    ]
    for label, payload, expect_code in bad_cases:
        status, body = call("POST", "/bi/metrics", body=payload)
        codes = [i.get("code") for i in (body or {}).get("detail", {}).get("issues", [])]
        check(f"拒绝 {label}", status == 400 and expect_code in codes,
              f"status={status} codes={codes}")

    print("=" * 62)
    if FAILED:
        print(f"结果：{len(FAILED)} 项失败")
        for f in FAILED:
            print("  -", f)
        return 1
    print("结果：全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
