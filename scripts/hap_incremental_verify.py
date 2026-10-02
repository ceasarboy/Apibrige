# -*- coding: utf-8 -*-
"""HAP 增量拉取真实端点验证（add-bi-hap-incremental）

验收口径（设计 §4）：
1. 首轮 bootstrap 为全量；水位线前进后，增量轮次只回传变化行（占比 <5%）；
2. 合并后的快照与"直接全量拉取"逐行一致（核心不变式）；
3. 删除的行在增量轮次不被感知（让渡语义，靠定期全量对账清除），删除操作本身有效。

**首轮增量的边界刷新代价（实测记录）**：本表 144 行全部落在同一秒（批量导入），
故"水位线同秒的边界行"= 整表 → 首轮增量会把基线整批刷新一遍（有界：受
`HAP_INCREMENTAL_BOUNDARY_MAX_ROWS` 上限约束、超限即跳过）。水位线一旦前进
（新行带来更晚的秒），边界集即收缩为"最新一批"——故验收以**稳态轮次**为准。

前置：后端以 `APIBRIDGE_HAP_INCREMENTAL_MIN_ROWS=0` 启动（阈值调 0 使 144 行的小表
也走增量；生产默认阈值 20000 是为小表保持全量语义，见 design §2）。
脚本只增删自己创建的行（前缀 `inc-verify-`），跑完自清理。

用法：
    APIBRIDGE_HAP_INCREMENTAL_MIN_ROWS=0 python -m uvicorn app.main:app --port 8000 &
    python scripts/hap_incremental_verify.py
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "smoke_case1.db"
BASE = "http://127.0.0.1:8000"
HDR = {"X-Admin-Token": "dev-admin-token"}
CONN_ID = "14252c4c-7f2e-4ff7-8180-17fbc22fad14"     # 明道云-物料数据
WS = "6ab624334e6294af6a07c572"                      # 能力表（144 行）
NAME_FIELD = "6ab624334e6294af6a07c573"              # 人名（文本）
QTY_FIELD = "6ab6246b2f24286e0987ff91"               # 能力值1（数值）
TAG = "inc-verify"
FORM_CACHE_TTL = 60          # app/services/bi_sources.py FORM_CACHE_TTL_SECONDS
FAILED: list[str] = []

BASE_URL = ""
AUTH: dict = {}
_last_pull_at = 0.0


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def load_credentials() -> tuple[str, dict, str]:
    import os
    if not os.environ.get("APIBRIDGE_ENCRYPT_KEY"):
        sys.exit("需要环境变量 APIBRIDGE_ENCRYPT_KEY（解密连接凭据用，见本地 .env）")
    from app.security.crypto import decrypt_db_config

    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    row = c.execute("select name, base_url, auth_config from api_connections "
                    "where id = ?", (CONN_ID.replace("-", ""),)).fetchone()
    cfg = json.loads(row["auth_config"]) if isinstance(row["auth_config"], str) \
        else (row["auth_config"] or {})
    cfg = decrypt_db_config(cfg) or cfg
    params = {}
    for k, v in cfg.items():
        kl = str(k).lower()
        if kl.endswith("appkey"):
            params["appKey"] = v
        elif kl.endswith("sign"):
            params["sign"] = v
    return row["base_url"], params, row["name"]


def hap(path: str, body: dict) -> dict:
    r = requests.post(f"{BASE_URL}{path}", json={**AUTH, **body}, timeout=60)
    r.raise_for_status()
    return r.json()


def hap_rows() -> list[dict]:
    """直接全量拉（真值侧，绕过平台）"""
    rows: list[dict] = []
    page = 1
    while True:
        out = hap("/v2/open/worksheet/getFilterRows",
                  {"worksheetId": WS, "listType": "1", "pageSize": 1000,
                   "pageIndex": page, "getSystemControl": True})
        batch = (out.get("data") or {}).get("rows") or []
        rows.extend(batch)
        if len(batch) < 1000:
            return rows
        page += 1


def pull_snapshot(label: str, *, wait: bool = True) -> dict:
    """经平台拉一轮（等过 form 行缓存 TTL，否则拿到的是缓存）"""
    global _last_pull_at
    if wait:
        wait_s = FORM_CACHE_TTL + 1 - (time.time() - _last_pull_at)
        if wait_s > 0:
            print(f"    （{label}：等 {wait_s:.0f}s 过 form 行缓存 TTL…）")
            time.sleep(wait_s)
    r = requests.get(f"{BASE}/api/v1/bi/form-rows", headers=HDR, timeout=60,
                     params={"connection_id": CONN_ID, "form_key": WS,
                             "page": 1, "page_size": 5})
    r.raise_for_status()
    _last_pull_at = time.time()
    return (r.json() or {}).get("snapshot") or {}


def add_row(seq: int) -> tuple[str, str]:
    name = f"{TAG}-{seq}"
    out = hap("/v2/open/worksheet/addRow", {
        "worksheetId": WS,
        "controls": [{"controlId": NAME_FIELD, "value": name},
                     {"controlId": QTY_FIELD, "value": seq}],
    })
    data = out.get("data")
    row_id = data.get("rowid") if isinstance(data, dict) else data
    return str(row_id or ""), name


def main() -> int:
    global BASE_URL, AUTH, _last_pull_at
    BASE_URL, AUTH, conn_name = load_credentials()
    print("=" * 64)
    print(f"HAP 增量真实端点验证：{conn_name} / 工作表 {WS}")
    print("=" * 64)

    created: list[tuple[str, str]] = []
    try:
        # ---------- 0. 预检 ----------
        snap = pull_snapshot("预检", wait=False)
        print(f"\n[0] 预检：enabled={snap.get('enabled')} mode={snap.get('mode')} "
              f"reason={snap.get('reason')} total={snap.get('total_rows')}")
        if not snap.get("enabled"):
            print("  [错误] 增量被关闭（APIBRIDGE_HAP_INCREMENTAL=0）——无法验证")
            return 2
        if str(snap.get("reason") or "") in ("below_threshold", "degrade:ok"):
            print("  [错误] 阈值未调 0（小表未启用增量）：请以 "
                  "APIBRIDGE_HAP_INCREMENTAL_MIN_ROWS=0 重启后端")
            return 2
        # 快照是**进程内状态**：上一轮验证留下的已删行会滞留到全量对账（设计使然）。
        # 要拿到干净的对照数字，必须重启后端后立刻跑本脚本。
        src_total = len(hap_rows())
        if snap.get("mode") != "full" or snap.get("reason") != "bootstrap"                 or snap.get("total_rows") != src_total:
            print(f"  [错误] 后端快照不干净：mode={snap.get('mode')} "
                  f"reason={snap.get('reason')} 快照 {snap.get('total_rows')} 行 vs "
                  f"源端 {src_total} 行（含上一轮遗留的已删行）")
            print("         → 请重启后端（清空内存快照）后立即重跑本脚本")
            return 2

        # ---------- 1. 首轮（全量 bootstrap） ----------
        print("\n[1] 首轮拉取（全量 bootstrap）")
        all_rows = hap_rows()
        total0 = len(all_rows)
        print(f"    源端直查 {total0} 行；平台快照 {snap.get('total_rows')} 行")
        check("首轮快照行数与源端一致", snap.get("total_rows") == total0,
              f"平台 {snap.get('total_rows')} / 源端 {total0}")

        # ---------- 2. 造变化 ----------
        print("\n[2] 造变化（新增 2 行 + 修改 1 行；只动本脚本创建的行）")
        rid1, name1 = add_row(1)
        rid2, name2 = add_row(2)
        created += [(rid1, name1), (rid2, name2)]
        hap("/v2/open/worksheet/editRow", {
            "worksheetId": WS, "rowId": rid1,
            "controls": [{"controlId": QTY_FIELD, "value": 77}]})
        print(f"    已新增 {name1} / {name2}，并修改 {name1} 的 {QTY_FIELD} → 77")
        check("新增 2 行成功", all(rid for rid, _ in created), str(created))

        # ---------- 3. 首轮增量（含基线秒边界刷新，如实记录） ----------
        print("\n[3] 首轮增量（水位线仍在基线秒 → 边界刷新=整表，属预期代价）")
        snap = pull_snapshot("首轮增量")
        print(f"    mode={snap.get('mode')} delta={snap.get('delta_rows')} "
              f"boundary={snap.get('boundary_rows')}"
              f"{'（已跳过）' if snap.get('boundary_skipped') else ''} "
              f"total={snap.get('total_rows')} wm={snap.get('watermark')}")
        check("本轮为增量模式", snap.get("mode") == "incremental",
              str(snap.get("reason")))
        check("真增量只含变化行（≤3）", (snap.get("delta_rows") or 0) <= 3,
              f"delta={snap.get('delta_rows')}")
        check("快照已含新增行", (snap.get("total_rows") or 0) == total0 + 2,
              f"{snap.get('total_rows')} = {total0} + 2")

        # ---------- 3b. 稳态增量（水位线已前进 → 边界集收缩） ----------
        print("\n[3b] 稳态增量（水位线已前进：验收口径按此轮）")
        rid3, name3 = add_row(3)
        created.append((rid3, name3))
        snap = pull_snapshot("稳态增量")
        delta = snap.get("delta_rows") or 0
        boundary = snap.get("boundary_rows") or 0
        total = snap.get("total_rows") or 1
        print(f"    mode={snap.get('mode')} 真增量={delta} 边界刷新={boundary} "
              f"total={total} wm={snap.get('watermark')}")
        check("本轮为增量模式", snap.get("mode") == "incremental",
              str(snap.get("reason")))
        # 新行若落在**与水位线同一秒**，严格大于不会返回它——由边界刷新（ge 水位线）
        # 兜住：两种通道合计必须覆盖变化行，且远小于整表
        check("本轮回传行数远小于整表（增量有效）", (delta + boundary) < total0,
              f"真增量 {delta} + 边界 {boundary} vs 整表 {total0}")
        check("快照已含同秒新行（边界刷新兜住）",
              (snap.get("total_rows") or 0) == total0 + 3,
              f"total={snap.get('total_rows')} = {total0} + 3")
        check("拉取行数占比 <5%（含边界刷新）", (delta + boundary) / total < 0.05,
              f"({delta}+{boundary})/{total} = {(delta + boundary) / total:.2%}")

        # ---------- 4. 核心不变式 ----------
        print("\n[4] 合并快照 vs 直接全量（逐行对照）")
        truth = {str(r.get("rowid")): r for r in hap_rows()}
        pull_snapshot("对照轮")
        snap_rows: dict[str, dict] = {}
        page = 1
        while True:
            r = requests.get(f"{BASE}/api/v1/bi/form-rows", headers=HDR, timeout=60,
                             params={"connection_id": CONN_ID, "form_key": WS,
                                     "page": page, "page_size": 200})
            body = r.json()
            for row in body.get("rows") or []:
                snap_rows[str(row.get("rowid"))] = row
            if not body.get("has_more"):
                break
            page += 1
        _last_pull_at = time.time()
        check("行标识集合一致", set(snap_rows) == set(truth),
              f"平台 {len(snap_rows)} / 源端 {len(truth)}")
        diff = [k for k in set(snap_rows) & set(truth)
                if str(snap_rows[k].get(NAME_FIELD)) != str(truth[k].get(NAME_FIELD))
                or str(snap_rows[k].get(QTY_FIELD)) != str(truth[k].get(QTY_FIELD))]
        check("抽查字段逐行一致（人名 / 能力值1）", not diff, f"差异 {diff[:3]}")

        # ---------- 5. 删除的让渡语义 ----------
        print("\n[5] 删除行的让渡语义（增量不感知，靠对账清除）")
        hap("/v2/open/worksheet/deleteRow", {"worksheetId": WS, "rowId": rid1})
        print(f"    已删除 {name1}")
        snap = pull_snapshot("删除后轮次")
        check("增量轮次仍持有已删除行（预期让渡）",
              (snap.get("total_rows") or 0) == total0 + 3,
              f"快照 {snap.get('total_rows')} = {total0} + 3 新增（含已删的 1 行）")

        # ---------- 6. 清理 ----------
        print("\n[6] 清理自建行")
        for rid, name in created:
            if rid:
                hap("/v2/open/worksheet/deleteRow", {"worksheetId": WS, "rowId": rid})
        left = [r for r in hap_rows()
                if str(r.get(NAME_FIELD) or "").startswith(TAG)]
        check("源端无残留自建行", not left, f"{len(left)} 行")
    finally:
        pass

    print("=" * 64)
    if FAILED:
        print(f"结果：{len(FAILED)} 项未通过")
        for f in FAILED:
            print("  -", f)
        return 1
    print("结果：全部通过 [OK]")
    print("提示：验证后建议不带上限环境变量重启后端，回到生产默认（阈值 20000）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
