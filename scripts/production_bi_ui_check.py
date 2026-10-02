# -*- coding: utf-8 -*-
"""生产运行管理看板 UI 自审（Playwright）

自审项：
    1. 管理页 /bi：14 张卡片渲染（4 KPI + 9 图表 + 1 明细表），KPI 数值命中基准
    2. 产线筛选元素存在，选择 B 线后联动生效
    3. 投放页 /bi/screen：1920 基准画布渲染、KPI 数值、图表 canvas 存在
    4. 控制台无报错门禁

用法: python scripts/production_bi_ui_check.py
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"
TOKEN = "dev-admin-token"
SHOT_DIR = Path(__file__).resolve().parent.parent / ".tmp_ui"
FAILED: list[str] = []

# 基准值（与 tests 数据对照；卡上显示保留两位小数）
KPI_BASELINE = {
    "今日产量达成率": 94.11,
    "今日设备稼动率": 84.79,
    "今日一次合格率": 96.37,
    "今日出勤率": 97.22,
}


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def collect_console(page) -> list[str]:
    errors: list[str] = []
    page.on("console", lambda m: errors.append(f"console.error: {m.text}")
            if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    return errors


def main() -> int:
    SHOT_DIR.mkdir(exist_ok=True)
    # 自审用确定性显示设置：深色主题（跑完还原原值，不破坏用户设置）
    import requests
    api = "http://localhost:8000/api/v1/bi"
    hdr = {"X-Admin-Token": TOKEN}
    orig = requests.get(f"{api}/screen-settings", headers=hdr).json()
    orig_theme = orig.get("theme", "dark")
    if orig_theme != "dark":
        requests.put(f"{api}/screen-settings", headers=hdr, json={**orig, "theme": "dark"})
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1920, "height": 1200},
                                  locale="zh-CN")
        ctx.add_init_script(
            f"localStorage.setItem('apibridge_admin_token', '{TOKEN}');")
        page = ctx.new_page()
        errors = collect_console(page)

        # ---------- 1. 管理页 ----------
        print("=" * 62)
        print("1. 管理页 /bi 画布")
        page.goto(f"{BASE}/bi", wait_until="networkidle")
        page.wait_for_timeout(4000)

        for label, expect in KPI_BASELINE.items():
            card = page.locator(".ant-card", has_text=label).first
            text = card.inner_text() if card.count() else ""
            check(f"KPI「{label}」显示 {expect}",
                  str(expect) in text, text.replace("\n", " ")[:60])

        canvases = page.locator("canvas").count()
        check("图表 canvas 渲染（≥9 个）", canvases >= 9, f"{canvases} 个")

        for name in ("产量达成趋势", "设备状态分布", "各产线产量对比",
                     "不良原因分布", "异常事件明细", "设备稼动率"):
            check(f"卡片「{name}」存在", page.locator(f"text={name}").count() >= 1)

        detail_rows = page.locator(".ant-table-tbody tr.ant-table-row")
        check("异常事件明细表有数据行", detail_rows.count() >= 5,
              f"{detail_rows.count()} 行")

        page.screenshot(path=str(SHOT_DIR / "prod_board_admin.png"), full_page=True)

        # ---------- 2. 产线筛选联动 ----------
        print("=" * 62)
        print("2. 产线筛选联动")
        filter_bar = page.locator("text=产线").first
        check("筛选元素「产线」存在", filter_bar.count() >= 1)
        # 找筛选条里的下拉（DimFilterBar 的 Select）
        bar_select = page.locator(".ant-select").filter(
            has=page.locator(".ant-select-selection-item")).first
        if bar_select.count():
            bar_select.click()
            page.wait_for_timeout(500)
            opt = page.locator(".ant-select-item-option", has_text="B线").first
            if opt.count():
                opt.click()
                page.wait_for_timeout(1500)
                check("选择 B 线后页面无报错", True)
            page.screenshot(path=str(SHOT_DIR / "prod_board_filter_b.png"),
                            full_page=True)
        else:
            check("找到筛选下拉", False)

        # ---------- 3. 投放页 ----------
        print("=" * 62)
        print("3. 投放页 /bi/screen")
        page.goto(f"{BASE}/bi/screen", wait_until="networkidle")
        page.wait_for_timeout(4000)

        for label, expect in KPI_BASELINE.items():
            card = page.locator("text=" + label).first
            check(f"投放页 KPI「{label}」", card.count() >= 1)

        canvases = page.locator("canvas").count()
        check("投放页图表 canvas（≥9 个）", canvases >= 9, f"{canvases} 个")

        # 明细卡完整可见（格位坐标映射内容区，摆到底部的卡不被裁）
        # 断言兼容深浅主题：深色用自研 scr-table，白底用 antd Table
        detail = page.locator(".scr-panel", has_text="异常事件明细").first
        m = detail.evaluate("""el => {
          const r = el.getBoundingClientRect();
          const head = el.querySelector('.scr-table__head, .ant-table-thead');
          const rows = el.querySelectorAll('.scr-table__row, .ant-table-tbody tr').length;
          return {bottom: Math.round(r.bottom), vh: window.innerHeight,
                  head: !!head, rows};
        }""")
        check("明细卡底在视口内（未被裁）", m["bottom"] <= m["vh"] + 1,
              f"bottom={m['bottom']} vh={m['vh']}")
        check("明细表头与数据行渲染", m["head"] and m["rows"] >= 5,
              f"head={m['head']} rows={m['rows']}")

        # 自动轮播：内容溢出时 scrollTop 随时间推进；悬停暂停
        # 探针找"实际滚动容器"（明细卡工具条改版后滚动区下移一层，按 scrollHeight 判）
        probe = """() => {
          const d = [...document.querySelectorAll('.scr-panel')]
                    .find(p => p.textContent.includes('异常事件明细'));
          const sc = [...d.querySelectorAll('div')]
                     .find(x => x.scrollHeight > x.clientHeight + 10);
          if (!sc) return {top: 0, overflow: 0, found: false};
          return {top: sc.scrollTop, overflow: sc.scrollHeight - sc.clientHeight,
                  found: true};
        }"""
        s1 = page.evaluate(probe)
        check("明细滚动容器存在", s1.get("found", False),
              f"overflow={s1['overflow']}px")
        positions = [s1["top"]]
        for _ in range(5):
            page.wait_for_timeout(800)
            positions.append(page.evaluate(probe)["top"])
        spread = max(positions) - min(positions)
        # 极差判定：正向滚动或"到底回顶"都会产生位移
        check("明细表自动轮播滚动", spread >= 20,
              f"采样 {[round(v) for v in positions]} 极差 {spread:.0f}px（溢出 {s1['overflow']}px）")
        detail.hover()
        page.wait_for_timeout(300)
        s3 = page.evaluate(probe)
        page.wait_for_timeout(2500)
        s4 = page.evaluate(probe)
        check("悬停暂停轮播", abs(s4["top"] - s3["top"]) < 2,
              f"悬停 2.5s 移动 {s4['top'] - s3['top']:.0f}px")
        page.mouse.move(10, 10)
        page.screenshot(path=str(SHOT_DIR / "prod_board_screen.png"), full_page=True)

        # KPI 数值在卡内完整显示（紧凑化后矮卡不裁数字）
        kpi = page.locator(".scr-panel", has_text="今日产量达成率").first
        km = kpi.evaluate("""el => {
          const num = el.querySelector('.scr-card__num');
          const card = num ? num.closest('.scr-card') : null;
          if (!num || !card) return null;
          const nr = num.getBoundingClientRect(), cr = card.getBoundingClientRect();
          return {text: num.textContent, fits: nr.bottom <= cr.bottom + 1};
        }""")
        check("KPI 数值在卡内完整显示", bool(km and km["fits"]),
              f"{km['text'] if km else '未找到'} fits={km['fits'] if km else '?'}")

        # ---------- 4. 控制台门禁 ----------
        print("=" * 62)
        print("4. 控制台检查")
        real = [e for e in errors
                if "favicon" not in e.lower() and "net::ERR_ABORTED" not in e]
        check("无控制台报错", not real, ("; ".join(real[:3]))[:300])

        browser.close()

    # 还原用户原有主题设置
    if orig_theme != "dark":
        cur = requests.get(f"{api}/screen-settings", headers=hdr).json()
        requests.put(f"{api}/screen-settings", headers=hdr,
                     json={**cur, "theme": orig_theme})

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
