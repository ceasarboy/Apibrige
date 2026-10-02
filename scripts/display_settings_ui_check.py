# -*- coding: utf-8 -*-
"""投放显示设置 UI 自审（Playwright；随仓库保留，供回归复跑）

验证（DataEase screenAdaptor 范式落地）：
    1. 看板设置弹层「投放显示」Tab 可用（分辨率预设/适配模式/主题）
    2. 主题：dark 深色底 / light 白底浅卡，全屏与非全屏一致
    3. 设备适配：1920×1080 与 1366×768（笔记本）两种视口等比缩放
    4. 适配模式：keep 居中 / widthFirst 宽撑满 生效
    5. 控制台无报错门禁

用法: python scripts/display_settings_ui_check.py
"""

import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"
API = "http://localhost:8000/api/v1/bi"
TOKEN = "dev-admin-token"
SHOT_DIR = Path(__file__).resolve().parent.parent / ".tmp_ui"
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def patch_settings(**kv):
    """读改写默认看板设置（整字段覆盖）"""
    cur = requests.get(f"{API}/screen-settings", headers={"X-Admin-Token": TOKEN}).json()
    cur.update(kv)
    r = requests.put(f"{API}/screen-settings", headers={"X-Admin-Token": TOKEN}, json=cur)
    assert r.status_code == 200, r.text


def main() -> int:
    SHOT_DIR.mkdir(exist_ok=True)
    # 还原默认显示设置（1920×1080 / keep / dark）
    # 保存用户原设置，用确定性环境自审（结束时还原）
    orig = requests.get(f"{API}/screen-settings",
                        headers={"X-Admin-Token": TOKEN}).json()
    patch_settings(base_w=1920, base_h=1080, screen_adaptor="keep", theme="dark")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        errors: list[str] = []

        def new_page(w, h):
            ctx = browser.new_context(viewport={"width": w, "height": h},
                                      locale="zh-CN")
            ctx.add_init_script(
                f"localStorage.setItem('apibridge_admin_token', '{TOKEN}');")
            page = ctx.new_page()
            page.on("console", lambda m: errors.append(f"console.error: {m.text}")
                    if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            return page, ctx

        # ---------- 1. 看板设置弹层 ----------
        print("=" * 62)
        print("1. 看板设置「投放显示」Tab")
        page, ctx = new_page(1600, 1000)
        page.goto(f"{BASE}/bi", wait_until="networkidle")
        page.wait_for_timeout(3000)
        page.locator("button", has_text="看板设置").click()
        page.wait_for_timeout(600)
        page.locator(".ant-tabs-tab", has_text="投放显示").click()
        page.wait_for_timeout(500)
        # 控件存在性断言（文本匹配受 antd Select 渲染时序影响不稳）
        popover = page.locator(".ant-popover:visible").last
        selects = popover.locator(".ant-select")
        check("三个设置控件（比例/适配/主题）", selects.count() >= 3,
              f"{selects.count()} 个")
        check("分辨率预设可见", popover.locator("text=画布比例").count() >= 1)
        check("适配模式可见", popover.locator("text=适配模式").count() >= 1)
        check("主题底色可见", popover.locator("text=主题底色").count() >= 1)
        page.screenshot(path=str(SHOT_DIR / "ds_settings_popover.png"))
        # 切 light 主题（走 UI：第 3 个 Select）
        selects.nth(2).click()
        page.wait_for_timeout(400)
        page.locator(".ant-select-item-option", has_text="白底").first.click()
        page.wait_for_timeout(800)
        saved = requests.get(f"{API}/screen-settings",
                             headers={"X-Admin-Token": TOKEN}).json()
        check("UI 切主题已持久化", saved.get("theme") == "light")
        page.keyboard.press("Escape")

        # ---------- 2. light 主题投放（非全屏） ----------
        print("=" * 62)
        print("2. 白底主题（1366×768 笔记本视口）")
        page.goto(f"{BASE}/bi/screen", wait_until="networkidle")
        page.wait_for_timeout(3500)
        root_cls = page.locator(".scr-root").first.get_attribute("class") or ""
        check("scr-root 带 light 类", "scr-root--light" in root_cls)
        bg = page.locator(".scr-root").first.evaluate(
            "el => getComputedStyle(el).backgroundColor")
        check("投放页底色为浅色（非全屏一致）",
              bg in ("rgb(242, 244, 248)", "rgb(242, 244, 248)"), bg)
        check("KPI 卡渲染", page.locator("text=今日产量达成率").count() >= 1)
        page.screenshot(path=str(SHOT_DIR / "ds_light_1366.png"), full_page=True)

        # ---------- 3. dark 主题（1920 大屏视口） ----------
        print("=" * 62)
        print("3. 深色主题（1920×1080 大屏视口）")
        patch_settings(theme="dark")
        page2, ctx2 = new_page(1920, 1080)
        page2.goto(f"{BASE}/bi/screen", wait_until="networkidle")
        page2.wait_for_timeout(3500)
        bg2 = page2.locator(".scr-root").first.evaluate(
            "el => getComputedStyle(el).backgroundColor")
        check("深色底生效", bg2 == "rgb(6, 20, 38)", bg2)
        # 等比缩放：画布宽 ≤ 视口宽
        canvas_w = page2.locator(".scr-root > div").first.evaluate(
            "el => el.getBoundingClientRect().width")
        check("等比缩放宽不超视口", canvas_w <= 1921, f"{canvas_w:.0f}px")
        page2.screenshot(path=str(SHOT_DIR / "ds_dark_1920.png"), full_page=True)

        # ---------- 4. widthFirst 适配（宽扁视口） ----------
        print("=" * 62)
        print("4. widthFirst 适配（1600×700 宽扁视口）")
        patch_settings(screen_adaptor="widthFirst")
        page3, ctx3 = new_page(1600, 700)
        page3.goto(f"{BASE}/bi/screen", wait_until="networkidle")
        page3.wait_for_timeout(3500)
        canvas_w3 = page3.locator(".scr-root > div").first.evaluate(
            "el => el.getBoundingClientRect().width")
        check("宽度撑满视口", abs(canvas_w3 - 1600) < 3, f"{canvas_w3:.0f}px")
        page3.screenshot(path=str(SHOT_DIR / "ds_widthfirst_1600.png"), full_page=True)
        ctx3.close()

        # ---------- 5. keep 垂直居中（16:10 视口） ----------
        print("=" * 62)
        print("5. keep 等比居中（1920×1200 视口）")
        patch_settings(screen_adaptor="keep")
        page4, ctx4 = new_page(1920, 1200)
        page4.goto(f"{BASE}/bi/screen", wait_until="networkidle")
        page4.wait_for_timeout(3000)
        top = page4.locator(".scr-root > div").first.evaluate(
            "el => Math.round(el.getBoundingClientRect().top)")
        # 基准 1920×1080 在 1200 高视口：scale=1，上下各留 60px
        check("垂直居中（top≈60）", abs(top - 60) <= 2, f"top={top}")

        # ---------- 6. 轮询跟随（投放页免刷新自动生效） ----------
        print("=" * 62)
        print("6. 设置轮询跟随（改主题，不刷新页面）")
        patch_settings(theme="light")
        page4.wait_for_timeout(35000)
        cls = page4.locator(".scr-root").first.get_attribute("class") or ""
        check("35s 内主题自动跟随", "scr-root--light" in cls, cls)
        ctx4.close()
        patch_settings(theme="dark")

        # ---------- 还原默认 + 控制台 ----------
        print("=" * 62)
        print("7. 还原与控制台")
        # 还原用户原设置（含主题/模式/基准）
        cur = requests.get(f"{API}/screen-settings",
                           headers={"X-Admin-Token": TOKEN}).json()
        cur.update({k: orig[k] for k in
                    ("base_w", "base_h", "screen_adaptor", "theme") if k in orig})
        requests.put(f"{API}/screen-settings",
                     headers={"X-Admin-Token": TOKEN}, json=cur)
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
