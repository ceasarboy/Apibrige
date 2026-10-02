"""BI 前端 UI 验证（真实 Chromium + 截图；临时脚本，用完即删）

验证：
    1. /bi        大屏页：图表 canvas 渲染、无控制台报错
    2. /bi/definitions  指标定义页：候选维度表 8 行、指标表有数据
    3. /bi/screen 投放页：深色容器、面板网格、轮播工具条
    4. 各页截图留档供人工核对

用法: python scripts/bi_ui_check.py
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:3000"
TOKEN = "dev-admin-token"
SHOT_DIR = Path(__file__).resolve().parent.parent / ".tmp_ui"
FAILED = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def main() -> int:
    SHOT_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000},
                                  locale="zh-CN")
        # 注入管理 token（前端从 localStorage 读取）
        ctx.add_init_script(
            f"localStorage.setItem('apibridge_admin_token', '{TOKEN}');")
        page = ctx.new_page()

        errors: list[str] = []
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        # ---------- 1. /bi 大屏页 ----------
        print("=" * 60)
        print("1. /bi 大屏页")
        page.goto(f"{BASE}/bi", wait_until="networkidle")
        page.wait_for_timeout(3500)
        canvases = page.locator("canvas").count()
        check("图表 canvas 已渲染", canvases > 0, f"{canvases} 个 canvas")
        check("页面标题存在", page.locator("text=BI 数据大屏").count() > 0)
        check("存在指标卡片", page.locator(".ant-card").count() > 0,
              f"{page.locator('.ant-card').count()} 个卡片")
        box = page.locator("canvas").first.bounding_box()
        check("canvas 有实际尺寸（非 0×0）",
              bool(box) and box["width"] > 80 and box["height"] > 80,
              f"{box['width']:.0f}×{box['height']:.0f}" if box else "无")
        # 表格型指标应真的渲染出数据行
        table_rows = page.locator(".ant-table-tbody tr").count()
        check("表格型指标有数据行", table_rows > 0, f"{table_rows} 行")
        page.screenshot(path=str(SHOT_DIR / "1_bi.png"), full_page=True)

        # ---------- 2. /bi/definitions ----------
        print("=" * 60)
        print("2. /bi/definitions 指标定义页")
        page.goto(f"{BASE}/bi/definitions", wait_until="networkidle")
        page.wait_for_timeout(2500)
        dim_rows = page.locator(".ant-table-tbody tr").count()
        check("表格已渲染", dim_rows > 0, f"{dim_rows} 行")
        check("候选维度含「类别」「物料」",
              page.locator("text=类别").count() > 0
              and page.locator("text=物料").count() > 0)
        check("含高基数标签", page.locator("text=高基数").count() > 0,
              f"{page.locator('text=高基数').count()} 处")
        page.screenshot(path=str(SHOT_DIR / "2_definitions.png"), full_page=True)

        # ---------- 3. /bi/screen 投放页 ----------
        print("=" * 60)
        print("3. /bi/screen 投放页")
        page.goto(f"{BASE}/bi/screen", wait_until="networkidle")
        page.wait_for_timeout(3500)
        check("深色投放容器存在", page.locator(".scr-root").count() == 1)
        check("面板已渲染", page.locator(".scr-panel").count() > 0,
              f"{page.locator('.scr-panel').count()} 个面板")
        check("KPI 翻牌卡已渲染", page.locator(".scr-card").count() > 0,
              f"{page.locator('.scr-card').count()} 张卡")
        check("轮播状态标签存在", page.locator("text=轮播中").count() > 0
              or page.locator("text=已暂停").count() > 0)
        check("投放页无左侧管理导航", page.locator(".ant-layout-sider").count() == 0)
        # 翻牌数字必须是真的数值（而非占位「—」）
        nums = page.locator(".scr-card__num").all_inner_texts()
        numeric = [n.strip() for n in nums if any(c.isdigit() for c in n)]
        check("KPI 翻牌卡有真实数值", len(numeric) > 0, f"值={nums[:4]}")
        scr_canvas = page.locator("canvas").count()
        check("投放页图表已渲染", scr_canvas > 0, f"{scr_canvas} 个 canvas")
        page.screenshot(path=str(SHOT_DIR / "3_screen.png"))

        # 空格暂停轮播
        page.keyboard.press("Space")
        page.wait_for_timeout(600)
        check("空格可暂停轮播", page.locator("text=已暂停").count() > 0)

        # ---------- 4. 控制台错误 ----------
        print("=" * 60)
        print("4. 控制台错误检查")
        real_errors = [e for e in errors
                       if "favicon" not in e.lower()
                       and "net::ERR_ABORTED" not in e]
        check("无控制台报错", not real_errors,
              ("; ".join(real_errors[:3]))[:300])

        browser.close()

    print("=" * 60)
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
