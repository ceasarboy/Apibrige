# -*- coding: utf-8 -*-
"""MES mock REST 源 → 明道云 HAP 目标 端到端同步验证

覆盖核心目标（读写均测）：
  1. 写：首轮同步后目标表有 N 行
  2. 回读值校验：明道云侧行内容与源字段一致
  3. 去重：重复 run 不追加（HAP push 按 key_field 去重）
  4. 删除同步：源删 1 行 → 再次 run → 目标对应行被删

用法: python scripts/mes_to_hap_e2e.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.smoke_agent_flow import SERVER, TOKEN, http, start_server, wait_ready  # noqa: E402

APP_KEY = os.environ.get("MINGDAO_APP_KEY")
SIGN = os.environ.get("MINGDAO_SIGN")
if not APP_KEY or not SIGN:
    print("缺少明道云凭证：请设置环境变量 MINGDAO_APP_KEY / MINGDAO_SIGN 后重跑", file=sys.stderr)
    sys.exit(2)
MES_PORT = 8888
MES_BASE = f"http://127.0.0.1:{MES_PORT}"
MES_MATERIAL_ENDPOINT = "/material/listPage"

WS = json.load(open(ROOT / "data" / "_mes_ws.json", encoding="utf-8"))
WS_ID = WS["worksheet_id"]
C = WS["controls"]  # 控件名 -> controlId
KEY_CTRL = C["物料编码"]

# MES mock MATERIALS 字段（源）→ 明道云控件 ID（目标）
FIELDS = [
    {"source": "materialCode", "target": C["物料编码"], "transform": "string"},
    {"source": "materialName", "target": C["物料名称"], "transform": "string"},
    {"source": "materialDesc", "target": C["物料描述"], "transform": "string"},
    {"source": "mtlcategoryName", "target": C["物料类别"], "transform": "string"},
    {"source": "mtlUomname", "target": C["单位"], "transform": "string"},
    {"source": "activeFlagName", "target": C["状态"], "transform": "string"},
    {"source": "safetyStock", "target": C["安全库存"], "transform": "string"},
    {"source": "supplierName", "target": C["供应商"], "transform": "string"},
]

# MES 源 3 条物料（对齐 tests/mes_mock_api.py MATERIALS）
EXPECT_CODES = ["SUB-BGA-78", "WIRE-AU-1.0", "MC-EMC-01"]


# ---- 明道云 v2 直接读写（目标侧验证用，与 APIBridge 无关）----

def md_post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        "https://api.mingdao.com/v2/open/worksheet" + path,
        data=json.dumps({**{"appKey": APP_KEY, "sign": SIGN}, **body}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:300]}


def md_read_rows() -> list[dict]:
    """读目标表全部行（扁平结构：controlId 键 + rowid 顶层键）"""
    r = md_post("/getFilterRows", {
        "worksheetId": WS_ID, "viewId": "", "listType": 1,
        "pageSize": 1000, "pageIndex": 1, "getSystemControl": True,
        "useControlId": True,
    })
    if r.get("http_error"):
        raise RuntimeError(f"getFilterRows 失败: {r}")
    return (r.get("data") or {}).get("rows") or []


def md_clear() -> None:
    """清空目标表（测试从干净态开始）"""
    for row in md_read_rows():
        md_post("/deleteRow", {
            "worksheetId": WS_ID, "rowId": row.get("rowid"),
            "triggerWorkflow": False, "thoroughDelete": True,
        })


def mes_delete_material(code: str) -> dict:
    """通过 MES mock REST 删除一条源物料（制造源侧减少）"""
    with urllib.request.urlopen(
            f"{MES_BASE}/material/delete?materialCode={urllib.parse.quote(code)}",
            timeout=30) as resp:
        return json.loads(resp.read().decode())


# ---- 主流程 ----

def main() -> int:
    results = []

    # 0）起 MES mock 源 + APIBridge
    db_file = ROOT / "data" / "mes_hap.db"
    if db_file.exists():
        db_file.unlink()

    from cryptography.fernet import Fernet
    encrypt_key = Fernet.generate_key().decode()

    mock_env = dict(os.environ)
    mock_env["MES_MOCK_PORT"] = str(MES_PORT)
    mock = subprocess.Popen(
        [sys.executable, "-m", "tests.mes_mock_api"],
        cwd=ROOT, env=mock_env, stdout=subprocess.DEVNULL)
    wait_ready(f"{MES_BASE}{MES_MATERIAL_ENDPOINT}")
    server = start_server("data/mes_hap.db", encrypt_key=encrypt_key)

    try:
        # 干净起始态
        print("[0] 清空目标表…")
        md_clear()

        # 1）建连接（MES REST 源 + 明道云 HAP 目标）
        print("[1] 建连接/映射/任务…")
        _, src_conn = http("POST", f"{SERVER}/api/v1/connections", {
            "name": "MES物料-REST源", "connection_type": "source",
            "protocol": "rest", "base_url": MES_BASE,
        })
        _, tgt_conn = http("POST", f"{SERVER}/api/v1/connections", {
            "name": "明道云-物料目标", "connection_type": "target",
            "protocol": "hap", "base_url": "https://api.mingdao.com",
            "auth_type": "custom",
            "auth_config": {"HAP-Appkey": APP_KEY, "HAP-Sign": SIGN},
        })
        assert src_conn.get("id") and tgt_conn.get("id"), (src_conn, tgt_conn)

        _, mapping = http("POST", f"{SERVER}/api/v1/mappings", {
            "name": "MES物料→明道云",
            "source_connection_id": src_conn["id"],
            "target_connection_id": tgt_conn["id"],
            "source_endpoint": MES_MATERIAL_ENDPOINT,
            "target_endpoint": WS_ID,
            "mapping_config": {
                "fields": FIELDS,
                "key_field": KEY_CTRL,
                "delete_sync": True,
                "protocol": {"worksheet_id": WS_ID},
            },
            "sync_mode": "full",
        })
        assert mapping.get("id"), mapping

        _, task = http("POST", f"{SERVER}/api/v1/tasks", {
            "mapping_id": mapping["id"], "task_type": "realtime",
            "sync_mode": "sync",
        })
        assert task.get("id"), task
        task_id = task["id"]

        def run_sync():
            status, run = http("POST", f"{SERVER}/api/v1/tasks/{task_id}/run")
            assert status in (200, 201), run
            return run

        # 2）首轮同步 → 写 + 回读值校验
        print("[2] 首轮同步…")
        run_sync()
        rows = md_read_rows()
        by_code = {r.get(KEY_CTRL): r for r in rows}
        assert len(rows) == len(EXPECT_CODES), \
            f"首轮写入应为 {len(EXPECT_CODES)} 行，实际 {len(rows)}: {sorted(by_code)}"
        # 回读值校验（文本字段精确 + 数值字段 str 归一）
        first = by_code["SUB-BGA-78"]
        assert first[C["物料名称"]] == "BGA78 基板", first
        assert first[C["供应商"]] == "欣兴电子", first
        assert str(first[C["安全库存"]]) == "1000", first
        results.append({"step": "首轮同步+回读校验", "rows": len(rows)})
        print(f"    写入 {len(rows)} 行 ✓，回读值一致（BGA78基板/欣兴电子/安全库存1000）")

        # 3）去重：重复 run 不追加
        print("[3] 去重验证（重复 run）…")
        run_sync()
        rows2 = md_read_rows()
        assert len(rows2) == len(EXPECT_CODES), \
            f"重复 run 不应追加，实际 {len(rows2)}"
        results.append({"step": "去重验证", "rows": len(rows2)})
        print(f"    重复 run 后仍 {len(rows2)} 行 ✓（无追加）")

        # 4）删除同步：源删 1 行 → 目标对应行被删
        print("[4] 删除同步验证（源删 SUB-BGA-78 → 目标删该行）…")
        del_out = mes_delete_material("SUB-BGA-78")
        assert del_out.get("success") and del_out.get("data", {}).get("deleted") == 1, del_out
        run_sync()
        rows3 = md_read_rows()
        codes3 = {r.get(KEY_CTRL) for r in rows3}
        assert len(rows3) == 2, f"删除同步后应为 2 行，实际 {len(rows3)}: {sorted(codes3)}"
        assert "SUB-BGA-78" not in codes3, f"SUB-BGA-78 应已删除: {sorted(codes3)}"
        results.append({"step": "删除同步验证", "rows": len(rows3), "codes": sorted(codes3)})
        print(f"    删除同步后 {len(rows3)} 行，SUB-BGA-78 已移除 ✓")

        print(json.dumps(results, ensure_ascii=False, indent=1))
        print("MES→明道云 E2E ALL PASS")
        return 0
    finally:
        mock.terminate()
        server.terminate()


if __name__ == "__main__":
    sys.exit(main())