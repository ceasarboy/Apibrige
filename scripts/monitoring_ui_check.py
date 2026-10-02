"""监控看板前端 UI 验证（tasks 5.1 – 5.4；随仓库保留，供回归复跑）

验证：
    5.1 时间范围 + 任务/连接筛选器存在且联动
    5.2 任务健康度表格（失败行标红）
    5.3 错误 Top N 点击跳转日志页并携带过滤条件
    5.4 任务行点击下钻抽屉（最近执行明细）

用法: python scripts/monitoring_ui_check.py
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:3000"
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
        ctx = browser.new_context(viewport={"width": 1600, "height": 1100},
                                  locale="zh-CN")
        ctx.add_init_script(
            f"localStorage.setItem('apibridge_admin_token', '{TOKEN}');")
        page = ctx.new_page()

        errors: list[str] = []
        page.on("console", lambda m: errors.append(f"console.error: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        # ---------- 5.1 筛选器 ----------
        print("=" * 62)
        print("5.1 筛选器")
        page.goto(f"{BASE}/dashboard", wait_until="networkidle")
        page.wait_for_timeout(3000)

        selects = page.locator(".ant-select").count()
        check("存在筛选器（时间/任务/连接）", selects >= 3, f"{selects} 个 select")

        # ---------- 5.2 任务健康度 ----------
        print("=" * 62)
        print("5.2 任务健康度表格")
        page.wait_for_selector("text=任务健康度", timeout=10000)
        health_card = page.locator(".ant-card", has_text="任务健康度")
        rows = health_card.locator(".ant-table-tbody tr.ant-table-row")
        row_count = rows.count()
        check("任务表有数据行", row_count >= 2, f"{row_count} 行")

        failed_rows = page.locator(".ant-table-tbody tr.row-has-failure")
        check("失败任务行已标红（row-has-failure）", failed_rows.count() >= 1,
              f"{failed_rows.count()} 行")

        body_text = health_card.inner_text()
        check("含成功/失败/搬运行数列",
              all(k in body_text for k in ("成功", "失败", "搬运行数")))

        # ---------- 5.1 联动：任务筛选 ----------
        print("=" * 62)
        print("5.1 筛选器联动")
        page.locator(".ant-select").nth(1).click()
        page.wait_for_timeout(400)
        options = page.locator(".ant-select-item-option")
        opt_count = options.count()
        check("任务筛选有选项", opt_count >= 2, f"{opt_count} 个选项")
        options.first.click()
        page.wait_for_timeout(800)
        filtered = health_card.locator(".ant-table-tbody tr.ant-table-row").count()
        check("选择任务后表格被过滤", filtered < row_count,
              f"{row_count} → {filtered} 行")
        # 清空筛选
        page.locator(".ant-select").nth(1).hover()
        page.locator(".ant-select-clear").first.click(force=True)
        page.wait_for_timeout(600)

        # ---------- 5.4 下钻抽屉 ----------
        print("=" * 62)
        print("5.4 任务下钻抽屉")
        page.goto(f"{BASE}/dashboard", wait_until="networkidle")
        page.wait_for_timeout(3000)
        health_card = page.locator(".ant-card", has_text="任务健康度")
        health_card.locator(".ant-table-tbody tr.ant-table-row").first.click()
        page.wait_for_timeout(1500)
        drawer = page.locator(".ant-drawer-content")
        check("抽屉已打开", drawer.count() >= 1)
        check("抽屉含执行明细", drawer.locator(".ant-table-tbody tr").count() >= 1,
              f"{drawer.locator('.ant-table-tbody tr').count()} 行明细")
        check("抽屉标题为任务执行明细", "任务执行明细" in drawer.inner_text())
        page.screenshot(path=str(SHOT_DIR / "4_dashboard_drawer.png"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)

        # ---------- 5.3 错误 Top N 跳转 ----------
        print("=" * 62)
        print("5.3 错误 Top N 与日志跳转")
        err_card = page.locator(".ant-card", has_text="错误 Top N")
        err_rows = err_card.locator(".ant-table-tbody tr.ant-table-row")
        check("错误 Top N 有数据", err_rows.count() >= 1, f"{err_rows.count()} 组")
        page.screenshot(path=str(SHOT_DIR / "1_dashboard.png"), full_page=True)

        err_rows.first.click()
        page.wait_for_timeout(2500)
        url = page.url
        check("已跳转到日志页", "/logs" in url, url[-70:])
        check("URL 携带 status=failed", "status=failed" in url)
        check("URL 携带时间范围",
              "start_time=" in url and "end_time=" in url)

        logs_text = page.locator("body").inner_text()
        check("日志页显示失败过滤生效", "失败" in logs_text)
        page.screenshot(path=str(SHOT_DIR / "2_logs_from_jump.png"), full_page=True)

        # ---------- 控制台 ----------
        print("=" * 62)
        print("控制台检查")
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
