# -*- coding: utf-8 -*-
"""明道云 V2 getFilterRows 能力探针（P4 增量拉取前置调研）

目的：用**真实端点**确认三件事，再决定增量拉取的技术路线（AGENTS 陷阱 18：
接口行为必须实测，不能照文档猜）：

1. `getFilterRows` 行里有哪些**系统字段**（rowid / ctime / utime / 自动编号）——
   增量水位线要挂在一个单调递增、能同时覆盖"新增"与"变更"的字段上；
2. `sortId` + `sortType` 是否被接受（能否按字段降序取"最新 N 行"）；
3. `filters` 数组是否被接受、形状是什么（能否服务端过滤 `utime > 水位线`）——
   这是"只拉增量"的关键；若 filters 不可用，退路是 sortId 降序 + 客户端截断。

只读脚本：只调 getFilterRows（不写任何行）。凭据从本机应用库解密读取。

用法: python scripts/hap_incremental_probe.py [worksheet_id]
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:                 # 允许直接 python scripts/xxx.py
    sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "smoke_case1.db"
CONN_ID = "14252c4c-7f2e-4ff7-8180-17fbc22fad14"   # 「明道云-物料数据」
DEFAULT_WS = "6ab624334e6294af6a07c572"            # 「能力表」(ryjnb_kpi 用的表)


def load_credentials() -> tuple[str, dict]:
    """从应用库读连接配置（auth_config 里是 header 鉴权或加密的 appKey/sign）"""
    import os
    if not os.environ.get("APIBRIDGE_ENCRYPT_KEY"):
        sys.exit("需要环境变量 APIBRIDGE_ENCRYPT_KEY（解密连接凭据用，见本地 .env）")
    from app.security.crypto import decrypt_db_config

    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    row = c.execute("select name, base_url, auth_config from api_connections "
                    "where id = ?", (CONN_ID.replace("-", ""),)).fetchone()
    if row is None:
        row = c.execute("select name, base_url, auth_config from api_connections "
                        "where id like ?", (CONN_ID.replace("-", "")[:20] + "%",)).fetchone()
    cfg = json.loads(row["auth_config"]) if isinstance(row["auth_config"], str) \
        else (row["auth_config"] or {})
    cfg = decrypt_db_config(cfg) or cfg
    print(f"连接: {row['name']} → {row['base_url']}；auth keys={list(cfg)}")
    return row["base_url"], cfg


async def call(base_url: str, auth: dict, path: str, body: dict) -> dict:
    """V2 工作表端点：**凭证进 body**（appKey/sign），连接级 header 只作自定义头附上"""
    headers = {"Content-Type": "application/json"}
    params: dict = {}
    for k, v in auth.items():
        kl = str(k).lower()
        if kl.endswith("appkey"):
            params["appKey"] = v
        elif kl.endswith("sign"):
            params["sign"] = v
        else:
            headers[k] = v
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30) as cli:
        r = await cli.post(base_url + path, json={**params, **body})
        r.raise_for_status()
        return r.json()


async def main() -> int:
    ws = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_WS
    base_url, auth = load_credentials()
    print(f"工作表: {ws}\n")

    # ---- 1. 基线：全量第一页，看行结构与系统字段 ----
    print("=" * 62)
    print("1. 行结构与系统字段（getSystemControl=True）")
    out = await call(base_url, auth, "/v2/open/worksheet/getFilterRows", {
        "worksheetId": ws, "listType": "1", "pageSize": 2, "pageIndex": 1,
        "getSystemControl": True,
    })
    print("success:", out.get("success"), "| error:", out.get("error_msg"))
    rows = (out.get("data") or {}).get("rows") or []
    print("data keys:", list((out.get("data") or {}).keys()))
    if rows:
        print("行键:", sorted(rows[0].keys()))
        print("样例行:", json.dumps(rows[0], ensure_ascii=False)[:400])
    if not rows:
        print("（该表无行，探针后续项无法进行）")
        return 1

    # ---- 2. sortId + sortType ----
    print("=" * 62)
    print("2. sortId + sortType（降序取前 2 行）")
    for sid in ("rowid", "ctime", "utime"):
        try:
            out = await call(base_url, auth, "/v2/open/worksheet/getFilterRows", {
                "worksheetId": ws, "listType": "1", "pageSize": 2, "pageIndex": 1,
                "getSystemControl": True, "sortId": sid, "sortType": "desc",
            })
            ok = out.get("success")
            got = [r.get(sid) for r in ((out.get("data") or {}).get("rows") or [])]
            print(f"  sortId={sid:<6} success={ok} error={out.get('error_msg')} 值={got}")
        except Exception as e:                                   # noqa: BLE001
            print(f"  sortId={sid:<6} 异常: {type(e).__name__}: {str(e)[:120]}")

    # ---- 3. filters 语法（判定性：用 pageSize=1000 看**过滤后的真实条数**） ----
    print("=" * 62)
    print("3. filters 生效判定（pageSize=1000，看命中条数）")

    async def probe(label: str, **extra) -> int:
        try:
            out = await call(base_url, auth, "/v2/open/worksheet/getFilterRows", {
                "worksheetId": ws, "listType": "1", "pageSize": 1000, "pageIndex": 1,
                "getSystemControl": True, **extra,
            })
            rows_n = len(((out.get("data") or {}).get("rows") or []))
            total = (out.get("data") or {}).get("total")
            print(f"  {label:<28} success={out.get('success')} rows={rows_n} total={total} "
                  f"err={out.get('error_msg')}")
            return rows_n
        except Exception as e:                                   # noqa: BLE001
            print(f"  {label:<28} 异常: {type(e).__name__}: {str(e)[:120]}")
            return -1

    base = await probe("无过滤（基线）")
    old_val = "2020-01-01 00:00:00"
    new_val = "2099-01-01 00:00:00"
    f_gt = [{"controlId": "utime", "dataType": 16, "filterType": 13, "value": old_val}]
    f_gt_far = [{"controlId": "utime", "dataType": 16, "filterType": 13, "value": new_val}]
    f_gt_str = [{"controlId": "utime", "dataType": 2, "filterType": 13, "value": old_val}]

    n1 = await probe("filterControls: utime gt 2020", filterControls=f_gt)
    n2 = await probe("filters: utime gt 2020", filters=f_gt)
    n3 = await probe("filterControls: utime gt 2099", filterControls=f_gt_far)
    n4 = await probe("filterControls: utime gt 2020 (dt=2)", filterControls=f_gt_str)
    f_eq = [{"controlId": "utime", "dataType": 16, "filterType": 2, "value": ""}]
    n5 = await probe("filterControls: utime eq 空", filterControls=f_eq)
    f_auto = [{"controlId": "autoid", "dataType": 6, "filterType": 13, "value": "1"}]
    n6 = await probe("filterControls: autoid gt 1", filterControls=f_auto)

    print("  —— 判定 ——")
    print(f"  filterControls 生效: {n1 not in (-1, base) and n1 == base}；"
          f"远未来值应 0: {n3 == 0}；filters 键是否等价: {n2 == n1}；"
          f"dt=2 是否等价: {n4 == n1}；autoid gt 1 应接近全量: {n6}")

    # ---- 4. 排序跨页一致性 + autoid 形态 ----
    print("=" * 62)
    print("4. sortId=utime 降序跨页一致性 / autoid 形态")
    seq: list[tuple] = []
    for page in (1, 2, 3):
        out = await call(base_url, auth, "/v2/open/worksheet/getFilterRows", {
            "worksheetId": ws, "listType": "1", "pageSize": 5, "pageIndex": page,
            "getSystemControl": True, "sortId": "utime", "sortType": "desc",
        })
        batch = (out.get("data") or {}).get("rows") or []
        seq.extend((r.get("utime"), str(r.get("autoid")), r.get("rowid")) for r in batch)
        print(f"  page{page}: " + ", ".join(f"{u}/{a}" for u, a, _ in
                                            [(r.get("utime"), r.get("autoid"), None) for r in batch]))
    print(f"  utime 单调不增: {all(seq[i][0] >= seq[i+1][0] for i in range(len(seq)-1))}")
    print(f"  跨页无重复 rowid: {len({r for _, _, r in seq}) == len(seq)}")
    print(f"  autoid 形如: {seq[0][1]!r}")

    # ---- 5. 决定性判定：filters 的 gt/lt 语义（增量拉取就靠它） ----
    print("=" * 62)
    print("5. filters 的 gt/lt 语义（utime 为日期时间 dt16；autoid 为数值 dt6）")

    async def probe(label: str, filters: list) -> tuple[int, int]:
        out = await call(base_url, auth, "/v2/open/worksheet/getFilterRows", {
            "worksheetId": ws, "listType": "1", "pageSize": 1000, "pageIndex": 1,
            "getSystemControl": True, "filters": filters,
        })
        data = out.get("data") or {}
        n, total = len(data.get("rows") or []), data.get("total")
        print(f"  {label:<34} rows={n:<5} total={total}")
        return n, total if isinstance(total, int) else -1

    ut = "2026-09-26 15:21:48"           # 数据里 utime 的实测值（全部行同秒）
    await probe("基线（无过滤）", [])
    await probe("utime gt 2099（应 0）", [{"controlId": "utime", "dataType": 16,
                                          "filterType": 13, "value": "2099-01-01 00:00:00"}])
    await probe("utime gt 2000（应全量）", [{"controlId": "utime", "dataType": 16,
                                            "filterType": 13, "value": "2000-01-01 00:00:00"}])
    await probe("utime lt 2000（应 0）", [{"controlId": "utime", "dataType": 16,
                                          "filterType": 15, "value": "2000-01-01 00:00:00"}])
    await probe("utime gt 边界秒（应 0）", [{"controlId": "utime", "dataType": 16,
                                           "filterType": 13, "value": ut}])
    await probe("utime ge 边界秒（应全量）", [{"controlId": "utime", "dataType": 16,
                                            "filterType": 14, "value": ut}])
    await probe("autoid gt 999999（应 0）", [{"controlId": "autoid", "dataType": 6,
                                             "filterType": 13, "value": "999999"}])
    await probe("autoid gt 1（应 143）", [{"controlId": "autoid", "dataType": 6,
                                          "filterType": 13, "value": "1"}])

    # ---- 6. 规模 ----
    print("=" * 62)
    print("6. 规模")
    out = await call(base_url, auth, "/v2/open/worksheet/getFilterRows", {
        "worksheetId": ws, "listType": "1", "pageSize": 1000, "pageIndex": 1,
        "getSystemControl": True,
    })
    print(f"  第一页行数 {len((out.get('data') or {}).get('rows') or [])}，"
          f"total={((out.get('data') or {}).get('total'))}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
