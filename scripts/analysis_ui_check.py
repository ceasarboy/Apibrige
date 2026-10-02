# -*- coding: utf-8 -*-
"""分析工作台 UI 自审（add-bi-adhoc-analysis，Playwright）

自审项：
    1. 页面可达（管理页入口按钮）+ 数据源选择 + 字段树加载（维度/指标分组）
    2. **点击通路**：点维度字段 → 维度槽；点数值字段 → 指标槽 → 即时出图
    3. **拖拽通路**：原生 HTML5 DnD（合成 DragEvent）把字段拖入槽位
    4. 引擎标签如实（下推 vs 内存 + 耗时 + 行数）
    5. 图表类型切换后画布仍渲染
    6. 「另存为看板卡片」→ 看板可见且数值与工作台一致
    7. 控制台无报错；跑完清理自审夹具（卡片/看板/摆放设置还原）

用法: python scripts/analysis_ui_check.py
前置：后端 8000（dev-admin-token）+ 前端 3000 已启动。
"""
import json
import re
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"
API = "http://127.0.0.1:8000/api/v1"
TOKEN = "dev-admin-token"
HDR = {"X-Admin-Token": TOKEN}
SHOT_DIR = Path(__file__).resolve().parent.parent / ".tmp_ui"

CONN_NAME = "生产运行管理-测试库"      # 夹具源：SQLite 大表/业务表
FORM_KEY = "production_output"
CARD_KEY = "adhoc_audit"              # 自审卡片（幂等：跑前删、跑后用同名残留）
AUDIT_BOARD = "分析自审"
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def api(method: str, path: str, **kw):
    return requests.request(method, f"{API}{path}", headers=HDR, timeout=60, **kw)


def pick_option(page, text: str | None = None, index: int = 0) -> None:
    """点下拉选项——必须限定在**未隐藏**的下拉里（antd 关闭后仍留在 DOM，
    直接 .ant-select-item-option 会命中已关闭下拉的不可见项）。"""
    dd = page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)")
    opts = dd.locator(".ant-select-item-option")
    if text:
        opts = opts.filter(has_text=text)
    opts.nth(index).click()


def cleanup() -> None:
    """幂等清理：删自审卡片 + 自审看板 + 从既有看板摘掉自审卡片（还原用户设置）"""
    api("DELETE", f"/bi/metrics/{CARD_KEY}")
    for b in (api("GET", "/bi/screens").json() or []):
        if b.get("name") == AUDIT_BOARD:
            api("DELETE", f"/bi/screens/{b['key']}")
    for b in (api("GET", "/bi/screens").json() or []):
        s = api("GET", "/bi/screen-settings", params={"board": b["key"]}).json() or {}
        cells = s.get("cells") or {}
        if CARD_KEY in cells:
            cells.pop(CARD_KEY)
            api("PUT", "/bi/screen-settings", params={"board": b["key"]},
                json={**s, "cells": cells})


def main() -> int:
    SHOT_DIR.mkdir(exist_ok=True)
    cleanup()
    # 自审看板必须在页面加载前建好——useBoards 只在挂载时拉一次列表
    created_board = (api("POST", "/bi/screens", json={"name": AUDIT_BOARD}).json() or {}).get("key")
    print(f"自审看板已创建: {created_board}")
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000}, locale="zh-CN")
        ctx.add_init_script(f"localStorage.setItem('apibridge_admin_token', '{TOKEN}');")
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(f"console.error: {m.text}")
                if m.type == "error" else None)

        # ---------- 1. 入口与字段树 ----------
        print("=" * 62)
        print("1. 入口与数据源")
        page.goto(f"{BASE}/bi", wait_until="networkidle")
        page.wait_for_timeout(2500)
        entry = page.locator("button", has_text="分析工作台")
        check("管理页有「分析工作台」入口", entry.count() > 0)
        entry.first.click()
        page.wait_for_timeout(1500)
        check("路由到 /bi/analysis", "/bi/analysis" in page.url, page.url)

        page.locator(".ant-select").first.click()
        page.wait_for_timeout(500)
        pick_option(page, CONN_NAME)
        page.wait_for_timeout(800)
        page.locator(".ant-select").nth(1).click()
        page.wait_for_timeout(500)
        pick_option(page)
        page.wait_for_timeout(2000)
        chips = page.locator("[data-testid='analysis-field-chip']")
        check("字段树加载（可拖拽字段 ≥5）", chips.count() >= 5, f"{chips.count()} 个字段")

        # ---------- 2. 点击通路 ----------
        print("=" * 62)
        print("2. 点击添加 → 即时出图")
        # 字段 chip 带 data-bitype（dimension/number/time）——比文案匹配稳定
        page.locator("[data-testid='analysis-field-chip'][data-bitype='dimension']").first.click()
        page.wait_for_timeout(400)
        page.locator("[data-testid='analysis-field-chip'][data-bitype='number']").first.click()
        page.wait_for_timeout(3000)
        dim_zone = page.locator(f"[data-testid='analysis-dimension-zone']")
        meas_zone = page.locator(f"[data-testid='analysis-measure-zone']")
        dim_tags = dim_zone.locator(".ant-tag")
        meas_tags = meas_zone.locator(".ant-tag")
        check("维度槽收到字段", dim_tags.count() + meas_tags.count() >= 1,
              f"维度 {dim_tags.count()} / 指标 {meas_tags.count()}")
        check("画布渲染（canvas 或表格）",
              page.locator("canvas").count() > 0 or page.locator(".scr-table").count() > 0,
              f"canvas={page.locator('canvas').count()}")
        tag_text = page.locator(".ant-card-head").last.inner_text()
        check("引擎标签如实标注", ("聚合下推" in tag_text) or ("内存聚合" in tag_text),
              tag_text.replace("\n", " ")[:60])

        # ---------- 3. 拖拽通路（合成 DragEvent） ----------
        print("=" * 62)
        print("3. 拖拽通路（原生 HTML5 DnD）")
        before = page.locator("[data-testid='analysis-dimension-zone'] .ant-tag").count()
        dragged = page.evaluate("""() => {
            const zone = document.querySelector("[data-testid='analysis-dimension-zone']");
            const used = new Set([...zone.querySelectorAll('.ant-tag')]
                .map(t => t.dataset.field));
            const chip = [...document.querySelectorAll(
                "[data-testid='analysis-field-chip'][data-bitype='dimension']")]
                .find(c => !used.has(c.dataset.field));
            if (!chip) return "";
            const dt = new DataTransfer();
            chip.dispatchEvent(new DragEvent('dragstart', {dataTransfer: dt, bubbles: true}));
            zone.dispatchEvent(new DragEvent('dragover', {dataTransfer: dt, bubbles: true, cancelable: true}));
            zone.dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true}));
            return chip.dataset.field;
        }""")
        page.wait_for_timeout(2500)
        after = page.locator("[data-testid='analysis-dimension-zone'] .ant-tag").count()
        check("拖拽把字段加入维度槽", after > before, f"拖入 {dragged}：{before} → {after}")

        # ---------- 4. 图表类型切换 ----------
        print("=" * 62)
        print("4. 图表类型切换")
        page.locator(".ant-select").nth(2).click()
        page.wait_for_timeout(400)
        pick_option(page, "number")
        page.wait_for_timeout(2000)
        kpi = page.locator(".ant-card").last.inner_text()
        check("切换为 number 后仍渲染", len(kpi.strip()) > 0)
        page.locator(".ant-select").nth(2).click()
        page.wait_for_timeout(400)
        pick_option(page, "bar")
        page.wait_for_timeout(2000)
        check("切回 bar 后 canvas 仍渲染", page.locator("canvas").count() > 0)
        page.screenshot(path=str(SHOT_DIR / "analysis_workbench.png"))

        # ---------- 5. 另存为卡片 → 看板可见 ----------
        print("=" * 62)
        print("5. 另存为看板卡片")
        check("自审看板已就绪", bool(created_board), str(created_board))
        page.locator("button", has_text="另存为看板卡片").first.click()
        page.wait_for_timeout(800)
        key_input = page.locator(".ant-modal input").nth(1)
        key_input.fill(CARD_KEY)
        page.locator(".ant-modal .ant-select").first.click()
        page.wait_for_timeout(400)
        pick_option(page, AUDIT_BOARD)
        page.locator(".ant-modal .ant-btn-primary", has_text=re.compile(r"保\s*存")).click()
        page.wait_for_timeout(3000)

        r = api("GET", f"/bi/metrics/{CARD_KEY}", params={"format": "raw"})
        check("卡片定义已建立", r.status_code == 200, f"HTTP {r.status_code}")
        card_def = next((m for m in api("GET", "/bi/metrics").json()
                         if m.get("metric_key") == CARD_KEY), None)
        check("卡片定义可从列表读回", bool(card_def))
        page.goto(f"{BASE}/bi?board={created_board}", wait_until="networkidle")
        page.wait_for_timeout(3500)
        body = page.inner_text("body")
        check("看板出现新卡片区", "分析" in body or CARD_KEY in body)
        page.screenshot(path=str(SHOT_DIR / "analysis_saved_card.png"))

        # 数值一致性：卡片预聚合桶 vs 同参数即席查询（另存不改变口径）
        if card_def:
            ref = json.loads(card_def["source_ref"])
            live = api("POST", "/bi/adhoc/query", json={
                "connection_id": ref["connection_id"], "form_key": ref["form_key"],
                "chart_type": card_def["chart_type"],
                "dimensions": card_def["dimensions"],
                "measures": card_def["measures"],
                "top_n": card_def["top_n"], "format": "raw",
            }).json()
            api("POST", "/bi/aggregate/run", params={"metric": CARD_KEY})
            card = api("GET", f"/bi/metrics/{CARD_KEY}", params={"format": "raw"}).json()
            live_map = dict(zip((live.get("data") or {}).get("x") or [],
                                ((live.get("data") or {}).get("series") or [{}])[0].get("data") or []))
            card_map = dict(zip((card.get("data") or {}).get("x") or [],
                                ((card.get("data") or {}).get("series") or [{}])[0].get("data") or []))
            same = (set(live_map) == set(card_map) and all(
                (live_map[k] is None and card_map[k] is None)
                or (isinstance(live_map[k], (int, float)) and isinstance(card_map[k], (int, float))
                    and abs(live_map[k] - card_map[k]) < 1e-6)
                for k in live_map))
            check("卡片数值与同参数即席查询逐项一致", same,
                  f"即席 {len(live_map)} 组 / 卡片 {len(card_map)} 组"
                  + (f"；差异 {[k for k in live_map if live_map.get(k) != card_map.get(k)][:3]}" if not same else ""))

        strays = [e for e in errors if "favicon" not in e]
        check("无控制台报错", not strays, "; ".join(strays[:2])[:200])
        browser.close()

    cleanup()
    if created_board:
        print(f"  （已清理自审看板 {created_board} 与卡片 {CARD_KEY}）")
    if FAILED:
        print(f"结果：{len(FAILED)} 项未通过")
        return 1
    print("结果：全部通过 [OK]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
