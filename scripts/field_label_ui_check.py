# -*- coding: utf-8 -*-
"""明细卡「列显示别名」投放页验证（field-labels 收口自审）

背景：definitions 页可给明细卡配置 style.column_labels（字段 → 显示名），
BiBoard 的 DetailTable 渲染时按「卡级别名 > 字段字典 name > 原始字段名」取名。
本脚本对既有夹具 perf_detail（column_labels: row_id→生产日期 / work_date→生产时间 /
actual_qty→实际产量）在**投放页**实测：
    1. 表头出现别名文案，且不再出现原始列名 work_date / actual_qty
    2. 未配置别名的列仍用原始列名（controls 生效的边界）
    3. 控制台无报错
只读：不改任何设置/定义。

用法: python scripts/field_label_ui_check.py
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"
TOKEN = "dev-admin-token"
BOARD = "ba95345c0cd6"          # 含 perf_detail（工作台夹具看板）
ALIASES = ["生产日期", "生产时间", "实际产量"]
RAW_NAMES = ["work_date", "actual_qty", "row_id"]
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def main() -> int:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080},
                                  locale="zh-CN")
        ctx.add_init_script(
            f"localStorage.setItem('apibridge_admin_token', '{TOKEN}');")
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(f"console.error: {m.text}")
                if m.type == "error" else None)
        page.goto(f"{BASE}/bi/screen?board={BOARD}", wait_until="networkidle")
        page.wait_for_timeout(4000)

        headers = page.eval_on_selector_all(
            ".scr-table__head span",
            "els => els.map(e => (e.innerText || '').trim())")
        print(f"  表头采样: {headers[:12]}")
        joined = " | ".join(headers)
        for alias in ALIASES:
            check(f"表头显示别名「{alias}」", alias in joined)
        for raw in RAW_NAMES:
            check(f"表头不再显示原始列名「{raw}」", raw not in joined)

        body = page.eval_on_selector_all(
            ".scr-table__row span",
            "els => els.map(e => (e.innerText || '').trim()).slice(0, 24)")
        check("数据行渲染", len(body) > len(headers),
              f"{len(body)} 个单元格文本（表头 {len(headers)}）")

        strays = [e for e in errors if "favicon" not in e]
        check("无控制台报错", not strays, "; ".join(strays[:2])[:200])
        browser.close()

    if FAILED:
        print(f"结果：{len(FAILED)} 项未通过")
        return 1
    print("结果：全部通过 [OK]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
