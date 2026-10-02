# -*- coding: utf-8 -*-
"""访问授权 UI 自审（add-bi-access-scope，Playwright）

自审项：
    1. 带 `?token=` 打开投放页 → 令牌写入 localStorage 且**地址栏参数被抹掉**
    2. 只看到授权看板；看板内只渲染"可见卡片"（行过滤维度缺失的卡片不出现）
    3. 页面自身发出的取值请求（浏览器路径）返回值已被过滤（只含授权维度值、不泄露其它值）
    4. 控制台无报错
    5. 跑完清理临时授权档案

前置：后端 8000（含 P6）+ 前端 3000 已启动；用默认看板（default）的既有卡片做夹具——
其中「各产线产量对比」(line_output) 有产线维度（可见）、
「产量达成趋势」(output_trend) 只有日期维度（行过滤 line 缺失 → 不可见）。

用法: python scripts/access_scope_ui_check.py
"""
import json
import re
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"
API = "http://127.0.0.1:8000/api/v1"
HDR = {"X-Admin-Token": "dev-admin-token"}
SHOT_DIR = Path(__file__).resolve().parent.parent / ".tmp_ui"
BOARD = "default"
LINE = "A线"
VISIBLE_CARD = "各产线产量对比"      # line_output：维度=产线 → 行过滤齐备 → 可见
HIDDEN_CARD = "产量达成趋势"          # output_trend：维度=日期 → 该授权不可见
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def main() -> int:
    SHOT_DIR.mkdir(exist_ok=True)
    # ---------- 0. 建临时授权（行过滤 line=A线） ----------
    r = requests.post(f"{API}/bi/access/profiles", headers=HDR, timeout=30, json={
        "name": "UI自审-一车间", "boards": [BOARD], "row_filters": {"line": LINE},
        "hidden_fields": [], "note": "access_scope_ui_check.py 临时档案"})
    if r.status_code != 201:
        print(f"[错误] 建授权失败 HTTP {r.status_code}: {r.text[:200]}")
        return 2
    token, pkey = r.json()["token"], r.json()["key"]
    print(f"临时授权：{pkey}（line={LINE}，看板={BOARD}）")
    errors: list[str] = []
    seen: dict[str, dict] = {}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1600, "height": 1000},
                                      locale="zh-CN")
            page = ctx.new_page()
            page.on("console", lambda m: errors.append(f"console.error: {m.text}")
                    if m.type == "error" else None)

            def on_response(resp):
                if "/api/v1/bi/metrics/" in resp.url and "format=" in resp.url:
                    key = resp.url.split("/metrics/")[1].split("?")[0]
                    try:
                        seen[key] = resp.text()
                    except Exception:                      # noqa: BLE001
                        pass
            page.on("response", on_response)

            # ---------- 1. token 注入与地址栏清理 ----------
            print("\n1. 令牌注入")
            page.goto(f"{BASE}/bi/screen?board={BOARD}&token={token}",
                      wait_until="networkidle")
            page.wait_for_timeout(4000)
            check("地址栏已抹掉 token", "token=" not in page.url, page.url)
            stored = page.evaluate(
                "() => localStorage.getItem('apibridge_bi_token')")
            check("令牌已写入 localStorage", stored == token,
                  (stored or "")[:16] + "…")

            # ---------- 2. 卡片级范围 ----------
            print("\n2. 卡片级范围（服务端裁剪）")
            body = page.inner_text("body")
            check(f"授权卡片可见「{VISIBLE_CARD}」", VISIBLE_CARD in body)
            check(f"行过滤维度缺失的卡片不可见「{HIDDEN_CARD}」", HIDDEN_CARD not in body)
            canvas = page.locator("canvas").count()
            check("画布仍渲染（可见卡片出图）", canvas >= 1, f"{canvas} 个 canvas")
            page.screenshot(path=str(SHOT_DIR / "access_scope_screen.png"))

            # ---------- 3. 浏览器取值路径已过滤 ----------
            # 断言按**响应原文**做（图表输出格式各异：echarts-bar 里没有 x 字段），
            # 且只允许"含产线维度的卡片"被返回（其余卡片已被服务端裁掉）
            print("\n3. 浏览器取值请求的返回值")
            allowed_cards = {"line_output", "line_pass"}   # 默认看板上含产线维度的两张
            check("只返回授权范围内的卡片", set(seen) == allowed_cards,
                  f"返回 {sorted(seen)}")
            for key, text in sorted(seen.items()):
                has_a = LINE in text
                leaks = [v for v in ("B线", "C线") if v in text]
                print(f"    {key}: 含授权值={has_a} 泄露其它产线={leaks}")
                check(f"{key} 含授权维度值且未泄露其它产线", has_a and not leaks,
                      f"leaks={leaks}")

            strays = [e for e in errors if "favicon" not in e]
            check("无控制台报错", not strays, "; ".join(strays[:2])[:200])
            browser.close()
    finally:
        requests.delete(f"{API}/bi/access/profiles/{pkey}", headers=HDR, timeout=30)
        print(f"\n9. 清理：临时授权已删除")

    if FAILED:
        print(f"结果：{len(FAILED)} 项未通过")
        for f in FAILED:
            print("  -", f)
        return 1
    print("结果：全部通过 [OK]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
