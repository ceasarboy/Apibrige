# -*- coding: utf-8 -*-
"""BI form 数据源前端 UI 验证（随仓库保留供回归复跑）

验证：
    1. 定义页新建指标默认数据源 = 连接表单；连接→表单→字段级联可用
    2. 维度/度量字段下拉来自 form-fields（带逻辑类型标注）；保存真实定义成功
    3. 明细表定义经大屏管理页渲染（detail 网格卡片）
    4. 大屏/投放页标题为「业务数据大屏」
    5. 全程无控制台报错

前置：后端 8000（smoke_case1.db）+ 前端 3000 已启动。
用法: python scripts/bi_form_ui_check.py
"""

import json
import sys
import uuid
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"
API = "http://127.0.0.1:8000/api/v1"
TOKEN = "dev-admin-token"
SHOT_DIR = Path(__file__).resolve().parent.parent / ".tmp_ui"
FAILED: list[str] = []
H = {"X-Admin-Token": TOKEN}


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def find_sqlite_form() -> tuple[str, str] | None:
    conns = requests.get(f"{API}/connections", headers=H, timeout=10).json()
    forms = requests.get(f"{API}/forms", headers=H, timeout=10).json()
    items = forms.get("items", forms) if isinstance(forms, dict) else forms
    for c in conns:
        if c.get("db_type") == "sqlite":
            f = next((x for x in items if x.get("connection_id") == c["id"]), None)
            if f:
                return c["id"], c["name"], f["form_key"]
    return None


def main() -> int:
    SHOT_DIR.mkdir(exist_ok=True)
    picked = find_sqlite_form()
    if not picked:
        print("未找到 SQLite 连接与注册表单，无法执行 UI 验证")
        return 1
    conn_id, conn_name, form_key = picked
    detail_key = f"ui_detail_{uuid.uuid4().hex[:6]}"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1600, "height": 1100},
                                  locale="zh-CN")
        ctx.add_init_script(
            f"localStorage.setItem('apibridge_admin_token', '{TOKEN}');")
        page = ctx.new_page()

        errors: list[str] = []
        page.on("console", lambda m: errors.append(
            f"console.error[{(m.location or {}).get('url', '?')[-60:]}]: {m.text}")
            if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        # 已知无害上游告警（精确过滤，不做宽匹配）：
        # rc-field-form Field.triggerMetaEvent → rc-util isEqual 在 Form.List
        # 动态项场景触发的开发态警告，仅 dev 构建出现，无功能影响
        KNOWN_BENIGN = ("There may be circular references",)

        # ---------- 1. 定义页级联 ----------
        print("=" * 62)
        print("1. 定义页：连接→表单→字段级联")
        page.goto(f"{BASE}/bi/definitions", wait_until="networkidle")
        page.wait_for_timeout(2000)
        page.locator("button", has_text="新建指标").first.click()
        page.wait_for_timeout(800)
        modal = page.locator(".ant-modal-content")
        check("弹窗已打开", modal.count() >= 1)
        src_value = modal.locator(".ant-select-selection-item").first.inner_text()
        check("默认数据源 = 连接表单", "连接表单" in src_value, src_value)

        def select_by_placeholder(placeholder: str):
            return modal.locator(".ant-select").filter(
                has=page.locator(".ant-select-selection-placeholder",
                                has_text=placeholder))

        conn_sel = select_by_placeholder("选择 HAP / 数据库连接")
        check("连接下拉存在", conn_sel.count() == 1, f"{conn_sel.count()} 个")
        conn_sel.click()
        page.wait_for_timeout(500)
        # 选项在 body 级 dropdown 门户里，且可能有多个（含隐藏的）——限定可见层
        visible_opts = page.locator(
            ".ant-select-dropdown:not(.ant-select-dropdown-hidden) "
            ".ant-select-item-option")
        visible_opts.filter(has_text=conn_name).first.click()
        page.wait_for_timeout(800)
        # 表单下拉（AutoComplete，placeholder 是 div 不是 input 属性）
        form_sel = select_by_placeholder("注册表选择")
        check("表单下拉存在", form_sel.count() == 1)
        form_sel.click()
        page.wait_for_timeout(300)
        form_sel.locator("input").first.fill(form_key)
        page.wait_for_timeout(800)
        visible_opts.filter(has_text=form_key).first.click()
        page.wait_for_timeout(1500)
        alert_text = modal.locator(".ant-alert").last.inner_text()
        check("字段元数据已加载", "已加载" in alert_text, alert_text[:60])

        # 添加维度：字段下拉应来自 form-fields，选中后中文名自动回填
        modal.locator("button", has_text="添加维度").first.click()
        page.wait_for_timeout(400)
        dim_sel = select_by_placeholder("选择维度字段")
        check("维度字段下拉存在", dim_sel.count() == 1)
        dim_sel.click()
        page.wait_for_timeout(500)
        opt_count = visible_opts.count()
        check("维度字段下拉有选项", opt_count >= 1, f"{opt_count} 个")
        visible_opts.first.click()
        page.wait_for_timeout(400)
        dim_name = modal.locator("input[placeholder='随字段自动填中文名']").first
        check("维度中文名自动回填", dim_name.input_value() != "",
              dim_name.input_value())

        # 保存：默认自带一条合法 count 度量，补名称/标识即可
        modal.locator("input[placeholder='如 产品目录汇总']").fill("冒烟UI-表单汇总")
        mk = f"ui_smoke_{uuid.uuid4().hex[:6]}"
        modal.locator("input[placeholder='如 product_catalog_summary']").fill(mk)
        # antd 两字按钮文案自动插空格（"保 存"），按主按钮定位
        page.locator(".ant-modal-footer button.ant-btn-primary").click()
        page.wait_for_timeout(1200)
        created = page.locator(".ant-table-tbody tr.ant-table-row", has_text=mk)
        check("定义保存成功（列表可见）", created.count() == 1)

        # ---------- 2. 大屏与投放页 ----------
        print("=" * 62)
        print("2. 大屏与投放页标题")
        page.goto(f"{BASE}/bi", wait_until="networkidle")
        page.wait_for_timeout(1500)
        check("管理页标题 = 业务数据大屏", "业务数据大屏" in page.locator("h1").inner_text())
        page.goto(f"{BASE}/bi/screen", wait_until="networkidle")
        page.wait_for_timeout(1500)
        check("投放页标题 = 业务数据大屏", "业务数据大屏" in page.locator("body").inner_text())

        # ---------- 3. 明细表渲染（经 API 建启用 detail 定义） ----------
        print("=" * 62)
        print("3. 明细表卡片渲染")
        r = requests.post(f"{API}/bi/metrics", headers=H, timeout=15, json={
            "metric_key": detail_key, "name": "UI冒烟-明细",
            "source_type": "form",
            "source_ref": json.dumps({"connection_id": conn_id,
                                      "form_key": form_key}),
            "granularity": "1min", "dimensions": [], "measures": [],
            "chart_type": "detail", "top_n": 100, "enabled": True})
        check("创建 detail 定义", r.status_code == 201, r.text[:120])
        page.goto(f"{BASE}/bi", wait_until="networkidle")
        page.wait_for_timeout(3000)
        grid = page.locator(".ant-card", has_text="UI冒烟-明细")
        check("明细卡片出现", grid.count() >= 1)
        check("明细卡片含真实行数据",
              grid.first.locator(".ant-table-tbody tr").count() >= 1,
              f"{grid.first.locator('.ant-table-tbody tr').count()} 行")
        page.screenshot(path=str(SHOT_DIR / "bi_form_detail.png"))

        # ---------- 4. 控制台 ----------
        print("=" * 62)
        print("4. 控制台检查")
        real = [e for e in errors
                if "favicon" not in e.lower() and "net::ERR_ABORTED" not in e
                and not any(k in e for k in KNOWN_BENIGN)]
        check("无控制台报错", not real, ("; ".join(real[:3]))[:300])

        browser.close()

    # 清理 UI 冒烟创建的定义（detail 走 API；UI 建的按名称定位）
    requests.delete(f"{API}/bi/metrics/{detail_key}", headers=H, timeout=10)
    defs = requests.get(f"{API}/bi/metrics", headers=H, timeout=10).json()
    for d in defs:
        if d.get("name", "").startswith("冒烟UI-") or d.get("metric_key", "").startswith("ui_smoke_"):
            requests.delete(f"{API}/bi/metrics/{d['metric_key']}", headers=H, timeout=10)
    print("  [清理] 冒烟定义已删除")

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
