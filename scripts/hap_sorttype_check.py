# -*- coding: utf-8 -*-
"""HAP `_stable_scan` 排序参数真机回归（2026-10-01，只读）

背景：审查发现删差/对账的稳定扫描用了 `{"sortId":"rowid","isAsc":true}`，
代码已改为 `{"sortId":"rowid","sortType":"asc"}`——本脚本对**真实端点**做
参数矩阵实验，决定 `_stable_scan` 的最终参数形态：

- rowid × {sortType=asc, sortType=desc, isAsc, 无参数} 的顺序与单调性；
- utime/ctime × sortType 作正交对照（P4 已实测 utime+desc 有效）；
- 稳定性：同请求两次一致、第 1/2 页无重叠（删差双读/分页的前提）。

用法: python scripts/hap_sorttype_check.py [worksheet_id]
只读：只调 getFilterRows。凭据从开发线仓库 smoke_case1.db 解密读取。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hap_incremental_probe as _probe  # noqa: E402
_probe.DB = Path(os.environ.get(
    "HAP_CRED_DB", "E:/GPOST/data/smoke_case1.db"))   # 凭据库路径可按环境覆盖
from hap_incremental_probe import DEFAULT_WS, call, load_credentials  # noqa: E402


async def fetch(base_url, auth, ws, page=1, **extra):
    out = await call(base_url, auth, "/v2/open/worksheet/getFilterRows", {
        "worksheetId": ws, "listType": "1", "pageSize": 5, "pageIndex": page,
        "getSystemControl": True, **extra,
    })
    rows = (out.get("data") or {}).get("rows") or []
    return out.get("success"), out.get("error_msg"), [r.get("rowid") for r in rows]


def mono(seq, reverse=False):
    s = sorted(seq, reverse=reverse)
    return seq == s


async def main() -> int:
    ws = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_WS
    base_url, auth = load_credentials()
    print(f"工作表: {ws}\n")

    variants = {
        "rowid+sortType=asc":  dict(sortId="rowid", sortType="asc"),
        "rowid+sortType=desc": dict(sortId="rowid", sortType="desc"),
        "rowid+isAsc=true":    dict(sortId="rowid", isAsc=True),
        "rowid+无排序参数":     dict(),
        "utime+sortType=asc":  dict(sortId="utime", sortType="asc"),
        "utime+sortType=desc": dict(sortId="utime", sortType="desc"),
        "ctime+sortType=desc": dict(sortId="ctime", sortType="desc"),
    }
    seqs = {}
    for label, extra in variants.items():
        ok, err, seq = await fetch(base_url, auth, ws, **extra)
        seqs[label] = seq
        print(f"[{label}] success={ok} err={err}")
        print("   ", seq)

    base_seq = seqs["rowid+无排序参数"]
    if len(base_seq) < 2:
        print("!! 表行数不足（<2），排序对照不可判定；换一张有数据的表重跑")
        return 1

    print("\n--- 单调性检查（rowid 字典序）---")
    for label in variants:
        seq = seqs[label]
        print(f"  {label:<22} 升序={mono(seq)}  降序={mono(seq, True)}")

    print("\n--- 序列等同性（相同者列出）---")
    labels = list(variants)
    for i, a in enumerate(labels):
        for b in labels[i + 1:]:
            if seqs[a] == seqs[b]:
                print(f"  {a!r} == {b!r}")

    # 稳定性（删差双读/分页的前提）
    ok2, _, again = await fetch(base_url, auth, ws, sortId="rowid", sortType="asc")
    print(f"\n[稳定性] rowid+sortType=asc 同请求两次一致: "
          f"{'✓' if ok2 and again == seqs['rowid+sortType=asc'] else '✗'}")

    out2 = await call(base_url, auth, "/v2/open/worksheet/getFilterRows", {
        "worksheetId": ws, "listType": "1", "pageSize": 5, "pageIndex": 2,
        "getSystemControl": True, "sortId": "rowid", "sortType": "asc",
    })
    page2 = [r.get("rowid") for r in ((out2.get("data") or {}).get("rows") or [])]
    overlap = set(seqs["rowid+sortType=asc"]) & set(page2)
    print(f"[分页] 第2页 rows={len(page2)} 与第1页重叠 {len(overlap)} "
          f"{'✓ 无重叠' if page2 and not overlap else '（表行数≤5 时第2页为空，可忽略）'}")

    print("\n== 结论：见上方单调性/等同性矩阵（决定 _stable_scan 最终参数形态）==")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
