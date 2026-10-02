# -*- coding: utf-8 -*-
"""多看板 UI 自审（Playwright；随仓库保留，供回归复跑）

验证：
    1. 默认看板（/bi）：既有生产看板卡片完整可见，看板选择器显示「默认看板」
    2. 新建看板 → 自动进布局编辑 → 空画布 + 卡片库 → 点击添加 3 张卡 → 完成
    3. 投放页 /bi/screen?board=<key>：标题为看板名，仅渲染摆放的卡片
    4. 管理页删除看板 → 回到默认看板，卡片恢复
    5. 控制台无报错门禁

用法: python scripts/multi_board_ui_check.py
"""

import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"
TOKEN = "dev-admin-token"
SHOT_DIR = Path(__file__).resolve().parent.parent / ".tmp_ui"
BOARD_NAME = "多看板自审"
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def main() -> int:
    SHOT_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1600, "height": 1100},
                                  locale="zh-CN")
        ctx.add_init_script(
            f"localStorage.setItem('apibridge_admin_token', '{TOKEN}');")
        page = ctx.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(f"console.error: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        # ---------- 1. 默认看板 ----------
        print("=" * 62)
        print("1. 默认看板")
        page.goto(f"{BASE}/bi", wait_until="networkidle")
        page.wait_for_timeout(3500)
        board_sel = page.locator(".ant-select", has_text="默认看板")
        check("看板选择器显示「默认看板」", board_sel.count() >= 1)
        check("默认看板显示生产 KPI 卡",
              page.locator("text=今日产量达成率").count() >= 1)
        check("默认看板显示明细表",
              page.locator("text=异常事件明细").count() >= 1)

        # ---------- 2. 新建看板 + 卡片库添加 ----------
        print("=" * 62)
        print("2. 新建看板与卡片库")
        page.locator("button", has_text="新建看板").click()
        page.wait_for_timeout(400)
        page.locator(".ant-modal input").fill(BOARD_NAME)
        page.locator(".ant-modal .ant-btn-primary").click()
        page.wait_for_timeout(1500)
        board_sel = page.locator(".ant-select", has_text=BOARD_NAME)
        check("看板已创建并切换", board_sel.count() >= 1)
        # 新建后自动进编辑模式：卡片库应可见，点 3 张卡（断言查画布卡标题，避开卡片库同名 tag）
        lib = page.locator("text=卡片库")
        check("布局编辑模式自动开启（卡片库可见）", lib.count() >= 1)
        check("空看板画布无卡片", page.locator(".ant-card-head-title").count() == 0)
        for name in ("今日产量达成率", "设备状态分布", "异常事件明细"):
            page.locator(".ant-tag-checkable", has_text=name).first.click()
            page.wait_for_timeout(700)
            check(f"添加卡片「{name}」到画布",
                  page.locator(".ant-card-head-title", has_text=name).count() >= 1)
        # antd 两字按钮渲染为「完 成」，用正则匹配
        page.locator("button", has_text=re.compile(r"完\s*成")).first.click()
        page.wait_for_timeout(1200)
        page.screenshot(path=str(SHOT_DIR / "mb_new_board.png"), full_page=True)

        # ---------- 3. 投放页 ----------
        print("=" * 62)
        print("3. 投放页（独立地址）")
        # 从看板选择器拿 key：直接遍历 DOM 不可行，走 API 建立的名字→key 映射
        import requests
        H = {"X-Admin-Token": TOKEN}
        screens = requests.get(f"http://localhost:8000/api/v1/bi/screens",
                               headers=H).json()
        key = next(s["key"] for s in screens if s["name"] == BOARD_NAME)
        page.goto(f"{BASE}/bi/screen?board={key}", wait_until="networkidle")
        page.wait_for_timeout(3500)
        body = page.locator("body").inner_text()
        check("投放页标题为看板名", BOARD_NAME in body)
        check("投放页仅渲染摆放的 KPI 卡",
              page.locator("text=今日产量达成率").count() >= 1)
        check("投放页未摆放的卡不出现",
              page.locator("text=产量达成趋势").count() == 0)
        page.screenshot(path=str(SHOT_DIR / "mb_screen.png"), full_page=True)

        # ---------- 4. 删除看板 ----------
        print("=" * 62)
        print("4. 删除看板")
        page.goto(f"{BASE}/bi", wait_until="networkidle")
        page.wait_for_timeout(3000)
        # 打开看板下拉（默认显示「默认看板」），选中自审看板
        page.locator(".ant-select", has_text="默认看板").first.click()
        page.wait_for_timeout(400)
        page.locator(".ant-select-item-option", has_text=BOARD_NAME).first.click()
        page.wait_for_timeout(1000)
        check("已切换到自审看板",
              page.locator(".ant-card-head-title", has_text="异常事件明细").count() >= 1)
        page.locator("button", has_text="删除").click()
        page.wait_for_timeout(500)
        page.locator(".ant-popconfirm .ant-btn-primary").click()
        page.wait_for_timeout(1500)
        check("删除后回到默认看板",
              page.locator(".ant-select", has_text="默认看板").count() >= 1)
        check("默认看板卡片恢复",
              page.locator("text=今日产量达成率").count() >= 1)

        # ---------- 5. 控制台 ----------
        print("=" * 62)
        print("5. 控制台检查")
        real = [e for e in errors
                if "favicon" not in e.lower() and "net::ERR_ABORTED" not in e]
        check("无控制台报错", not real, ("; ".join(real[:3]))[:300])

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
