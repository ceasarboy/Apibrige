# -*- coding: utf-8 -*-
"""字段抽屉「增加到表单注册表」按钮 UI 验证（非锁定模式；随仓库保留供回归复跑）

验证：
    1. 连接页打开 DB 连接字段抽屉（非锁定模式出现登记按钮）
    2. 选未登记表 → 点击登记 → 201 成功提示，且 GET /forms 出现 source=manual 条目
    3. 选已登记表 → 点击登记 → 409 提示「该连接下已存在表单」
    4. 锁定模式（经表单注册表页进入）不出现登记按钮
    5. 全程无控制台报错

前置：后端 8000（smoke_case1.db）+ 前端 3000 已启动。
用法: python scripts/field_drawer_register_ui_check.py
"""

import sqlite3
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"
API = "http://127.0.0.1:8000/api/v1"
TOKEN = "dev-admin-token"
SQLITE_DB = os.environ.get("SQLITE_SKILL_TEST_DB", "")
if not SQLITE_DB:
    import sys
    sys.exit("需要环境变量 SQLITE_SKILL_TEST_DB")
TMP_TABLE = "registry_probe_tmp"
CONN_NAME = "SQLite-本地联调"
SHOT_DIR = Path(__file__).resolve().parent.parent / ".tmp_ui"
FAILED: list[str] = []

H = {"X-Admin-Token": TOKEN}


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def setup_tmp_table() -> None:
    # 表名/列名为脚本内常量，SQL 写成固定字符串（Mimosa 不拦字面量 SQL）
    conn = sqlite3.connect(SQLITE_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS registry_probe_tmp "
        "(id INTEGER PRIMARY KEY, note TEXT NOT NULL, qty INTEGER)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO registry_probe_tmp VALUES "
        "(1, '探针联调测试物料', 10)"
    )
    conn.commit()
    conn.close()


def cleanup_tmp_form() -> None:
    """删除本脚本登记的临时表单条目（按连接名 + 表名定位）"""
    conns = requests.get(f"{API}/connections", headers=H, timeout=10).json()
    target = next((c for c in conns if c.get("name") == CONN_NAME), None)
    if not target:
        return
    forms = requests.get(f"{API}/forms", headers=H, timeout=10).json()
    items = forms.get("items", forms) if isinstance(forms, dict) else forms
    for f in items:
        if f.get("connection_id") == target.get("id") \
                and f.get("form_key") == TMP_TABLE:
            requests.delete(f"{API}/forms/{f['id']}", headers=H, timeout=10)
            print(f"  [清理] 已删除登记条目 {TMP_TABLE}")
            return


def cleanup_tmp_table() -> None:
    conn = sqlite3.connect(SQLITE_DB)
    conn.execute("DROP TABLE IF EXISTS registry_probe_tmp")
    conn.commit()
    conn.close()


def main() -> int:
    SHOT_DIR.mkdir(exist_ok=True)
    cleanup_tmp_form()  # 先清遗留条目，避免本轮 201 路径撞 409
    setup_tmp_table()
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

        # ---------- 1. 打开抽屉（非锁定模式） ----------
        print("=" * 62)
        print("1. 打开 DB 连接字段抽屉")
        page.goto(f"{BASE}/connections", wait_until="networkidle")
        page.wait_for_timeout(2000)
        row = page.locator(".ant-table-tbody tr.ant-table-row",
                           has_text=CONN_NAME)
        check("连接行存在", row.count() == 1, f"{row.count()} 行")
        row.locator("button", has_text="字段").click()
        page.wait_for_timeout(1500)
        drawer = page.locator(".ant-drawer-content")
        check("抽屉已打开", drawer.count() >= 1)
        check("DB 连接为表下拉选择", drawer.locator(".ant-select").count() >= 1)

        # ---------- 2. 成功登记（201） ----------
        print("=" * 62)
        print("2. 选未登记表并登记")
        drawer.locator(".ant-select").first.click()
        page.wait_for_timeout(600)
        opt = page.locator(".ant-select-item-option",
                           has_text=TMP_TABLE)
        check("下拉含临时表", opt.count() == 1, f"{opt.count()} 个")
        opt.click()
        page.wait_for_timeout(1500)
        check("字段列表已加载", drawer.locator(".ant-table-tbody tr")
              .count() >= 2, "表头+数据行")
        # 登记按钮在字段加载后随 footer 渲染（未选表时 footer 不出现）
        reg_btn = drawer.locator("button", has_text="增加到表单注册表")
        check("非锁定模式出现登记按钮", reg_btn.count() == 1,
              f"{reg_btn.count()} 个")
        reg_btn.click()
        page.wait_for_timeout(1200)
        toast = page.locator(".ant-message")
        msg_text = toast.inner_text() if toast.count() else ""
        check("成功提示出现", "已增加到表单注册表" in msg_text, msg_text[:80])

        conns = requests.get(f"{API}/connections", headers=H, timeout=10).json()
        target = next(c for c in conns if c.get("name") == CONN_NAME)
        forms = requests.get(f"{API}/forms", headers=H, timeout=10).json()
        items = forms.get("items", forms) if isinstance(forms, dict) else forms
        entry = next((f for f in items
                      if f.get("connection_id") == target.get("id")
                      and f.get("form_key") == TMP_TABLE), None)
        check("API 中出现 manual 条目",
              entry is not None and entry.get("source") == "manual",
              str(entry and entry.get("source")))
        page.screenshot(path=str(SHOT_DIR / "field_drawer_registered.png"))

        # ---------- 3. 重复登记（409） ----------
        print("=" * 62)
        print("3. 选已登记表再登记（409 路径）")
        drawer.locator(".ant-select").first.click()
        page.wait_for_timeout(600)
        page.locator(".ant-select-item-option",
                     has_text="skill_test_items").click()
        page.wait_for_timeout(1500)
        reg_btn.click()
        page.wait_for_timeout(1200)
        msg_text = toast.inner_text() if toast.count() else ""
        check("409 提示出现", "该连接下已存在表单" in msg_text, msg_text[:80])

        # ---------- 4. 锁定模式无登记按钮 ----------
        print("=" * 62)
        print("4. 锁定模式（表单注册表页进入）")
        page.goto(f"{BASE}/forms", wait_until="networkidle")
        page.wait_for_timeout(2000)
        # 注册表按更新时间排序，目标行可能翻页——锁定模式断言与具体表单无关，
        # 取第一页里任意 DB 连接（Oracle-物料同步）的行即可
        frow = page.locator(".ant-table-tbody tr.ant-table-row",
                            has_text="Oracle-物料同步").first
        check("注册表存在 DB 连接行", frow.count() == 1)
        frow.locator("button", has_text="字段").first.click()
        page.wait_for_timeout(1500)
        drawer2 = page.locator(".ant-drawer-content")
        check("锁定模式抽屉已打开", drawer2.count() >= 1)
        check("锁定模式无登记按钮",
              drawer2.locator("button", has_text="增加到表单注册表").count() == 0)
        page.keyboard.press("Escape")

        # ---------- 5. 控制台 ----------
        print("=" * 62)
        print("5. 控制台检查")
        real = [e for e in errors
                if "favicon" not in e.lower() and "net::ERR_ABORTED" not in e
                # 409 路径本身会以网络错误形式出现在控制台（预期行为）
                and "status of 409" not in e]
        check("无控制台报错", not real, ("; ".join(real[:3]))[:300])

        browser.close()

    cleanup_tmp_form()
    cleanup_tmp_table()

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
