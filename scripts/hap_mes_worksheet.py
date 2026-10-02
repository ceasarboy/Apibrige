# -*- coding: utf-8 -*-
"""Phase 1：明道云建 MES 物料测试表 + V2 读写能力验证

建表 → 拿控件ID → addRow(写) → getFilterRows(读列表) → getRowById(回读详情)
→ deleteRow(删) → 保存 worksheet 结构到 data/_mes_ws.json 供后续端到端同步使用。

控件类型: 2=文本 6=数值。key_field 用「物料编码」(源 materialCode)。
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

APP_KEY = os.environ.get("MINGDAO_APP_KEY")
SIGN = os.environ.get("MINGDAO_SIGN")
APP_ID = os.environ.get("MINGDAO_APP_ID")
if not (APP_KEY and SIGN and APP_ID):
    print("缺少明道云凭证：请设置环境变量 MINGDAO_APP_KEY / MINGDAO_SIGN / MINGDAO_APP_ID 后重跑",
          file=sys.stderr)
    sys.exit(2)
BASE = "https://api.mingdao.com/v2/open/worksheet"

# MES 物料表单关键字段（对齐 tests/mes_mock_api.py 的 MATERIALS）
CONTROLS = [
    {"type": 2, "controlName": "物料编码"},
    {"type": 2, "controlName": "物料名称"},
    {"type": 2, "controlName": "物料描述"},
    {"type": 2, "controlName": "物料类别"},
    {"type": 2, "controlName": "单位"},
    {"type": 2, "controlName": "状态"},
    {"type": 6, "controlName": "安全库存"},
    {"type": 2, "controlName": "供应商"},
]


def post(path, body):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps({**{"appKey": APP_KEY, "sign": SIGN}, **body}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:400]}


def get(path, params):
    url = f"{BASE}{path}?" + urllib.parse.urlencode(
        {"appKey": APP_KEY, "sign": SIGN, **params})
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:400]}


def main():
    # 1) 建表
    r = post("/addWorksheet", {"appId": APP_ID, "name": "MES物料同步",
                                "controls": CONTROLS})
    ws_id = r.get("data")
    print("addWorksheet:", json.dumps(r, ensure_ascii=False)[:200])
    if r.get("http_error") or not ws_id:
        raise SystemExit(f"建表失败: {r}")
    print("worksheetId:", ws_id)

    # 2) 拿控件 ID
    info = post("/getWorksheetInfo", {"worksheetId": ws_id})
    data = info.get("data") or {}
    ctrls = data.get("controls") or (data.get("template") or {}).get("controls") or []
    mapping = {}
    for c in ctrls:
        mapping[c.get("controlName")] = c.get("controlId")
    print("controls:", json.dumps(mapping, ensure_ascii=False))
    assert len(mapping) == len(CONTROLS), f"控件数不符: {mapping}"

    # 3) 写一行（addRow）
    add = post("/addRow", {
        "worksheetId": ws_id,
        "controls": [
            {"controlId": mapping["物料编码"], "value": "SUB-BGA-78"},
            {"controlId": mapping["物料名称"], "value": "BGA78 基板"},
            {"controlId": mapping["物料描述"], "value": "BGA-78 载板"},
            {"controlId": mapping["物料类别"], "value": "基板类"},
            {"controlId": mapping["单位"], "value": "片"},
            {"controlId": mapping["状态"], "value": "启用"},
            {"controlId": mapping["安全库存"], "value": "1000"},
            {"controlId": mapping["供应商"], "value": "欣兴电子"},
        ],
    })
    row_id = add.get("data")
    print("addRow:", json.dumps(add, ensure_ascii=False)[:200])
    assert add.get("success") and row_id, f"写失败: {add}"
    print("rowId:", row_id)

    # 4) 读列表（getFilterRows，带 useControlId 探扁平结构）
    flt = post("/getFilterRows", {
        "worksheetId": ws_id, "viewId": "", "listType": 1,
        "pageSize": 1000, "pageIndex": 1, "useControlId": True,
    })
    rows = (flt.get("data") or {}).get("rows") or []
    print(f"getFilterRows: total={(flt.get('data') or {}).get('total')} "
          f"rows={len(rows)}")
    if rows:
        print("首行 keys:", sorted(rows[0].keys()))
        print("首行(物料编码):", rows[0].get(mapping["物料编码"]))
        print("首行(安全库存):", rows[0].get(mapping["安全库存"]))

    # 5) 回读详情（getRowById，GET + query）
    detail = get("/getRowById", {"worksheetId": ws_id, "rowId": row_id})
    d = detail.get("data") or {}
    print("getRowById:", json.dumps(detail, ensure_ascii=False)[:300])
    if d:
        print("详情 keys:", sorted(d.keys()) if isinstance(d, dict) else type(d).__name__)
        print("详情(物料编码):", d.get(mapping["物料编码"]) if isinstance(d, dict) else d)

    # 6) 删测试行（deleteRow，清理，保持表干净）
    dele = post("/deleteRow", {
        "worksheetId": ws_id, "rowId": row_id,
        "triggerWorkflow": False, "thoroughDelete": True,
    })
    print("deleteRow:", json.dumps(dele, ensure_ascii=False)[:150])

    # 7) 保存结构供后续端到端使用
    with open("data/_mes_ws.json", "w", encoding="utf-8") as f:
        json.dump({"worksheet_id": ws_id, "controls": mapping, "name": "MES物料同步"},
                  f, ensure_ascii=False, indent=1)
    print("saved -> data/_mes_ws.json")


if __name__ == "__main__":
    main()