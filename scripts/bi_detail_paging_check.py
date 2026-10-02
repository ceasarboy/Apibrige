# -*- coding: utf-8 -*-
"""明细大表分页 UI 自审（add-bi-detail-query；随仓库保留，供回归复跑）

前置（测试环境资产）：
    - 连接「生产运行管理-测试库」中存在 perf_big 表（20 万行，造数脚本见
      docs/bi-evolution-plan.md 或 project-status.md 性能实测节）；
    - 看板「计算能力演示」（key=ba95345c0cd6）已摆放 perf_detail 卡。

验证：分页渲染（单页 DOM 受控）、翻页、选排序字段回到第一页、关键字搜索命中
（内存过滤提示）、控制台无报错。

用法: python scripts/bi_detail_paging_check.py
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"
BOARD = "ba95345c0cd6"
TOKEN = "dev-admin-token"
SHOT_DIR = Path(__file__).resolve().parent.parent / ".tmp_ui"
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def main() -> int:
    SHOT_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080},
                                  locale="zh-CN")
        ctx.add_init_script(
            f"localStorage.setItem('apibridge_admin_token', '{TOKEN}');")
        page = ctx.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(f"console.error: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(f"{BASE}/bi/screen?board={BOARD}", wait_until="networkidle")
        page.wait_for_timeout(7000)

        panel = page.locator(".scr-panel", has_text="大表明细").first
        check("大表明细卡渲染", panel.count() >= 1)
        check("页码显示（第 1 页）", "第 1 页" in panel.inner_text())
        dom_rows = panel.locator(".scr-table__row").count()
        check("单页 DOM 行数受控（≤200）", 0 < dom_rows <= 200, f"{dom_rows} 行")

        panel.locator("button", has_text="下一页").click()
        page.wait_for_timeout(1500)
        check("翻到第 2 页", "第 2 页" in panel.inner_text())

        sel = panel.locator(".ant-select").first
        sel.click()
        page.wait_for_timeout(400)
        page.locator(".ant-select-item-option", has_text="row_id").first.click()
        page.wait_for_timeout(1500)
        check("选排序字段后回第 1 页", "第 1 页" in panel.inner_text())

        panel.locator(".ant-input").first.fill("批次-19999")
        panel.locator(".ant-input").first.press("Enter")
        page.wait_for_timeout(1600)
        body = panel.inner_text()
        check("关键字搜索命中", "19999" in body)
        check("内存过滤提示", "内存过滤" in body)

        real = [e for e in errors
                if "favicon" not in e.lower() and "net::ERR_ABORTED" not in e]
        check("无控制台报错", not real, ("; ".join(real[:2]))[:200])
        page.screenshot(path=str(SHOT_DIR / "p1_detail_paging.png"))
        browser.close()

    print("=" * 62)
    print(f"截图目录: {SHOT_DIR}")
    if FAILED:
        print(f"结果：{len(FAILED)} 项失败")
        for f in FAILED:
            print("  -", f)
        return 1
    print("结果：全部通过 [OK]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
