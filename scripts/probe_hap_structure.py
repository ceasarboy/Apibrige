# -*- coding: utf-8 -*-
"""探测 getFilterRows 扁平结构下值形态（临时脚本，用完删除）"""
import json
import os
import sys
import urllib.request

APP_KEY = os.environ.get("MINGDAO_APP_KEY")
SIGN = os.environ.get("MINGDAO_SIGN")
if not (APP_KEY and SIGN):
    print("缺少明道云凭证：请设置环境变量 MINGDAO_APP_KEY / MINGDAO_SIGN 后重跑", file=sys.stderr)
    sys.exit(2)
WS_ID = json.load(open("data/_case2_ws.json", encoding="utf-8"))["worksheet_id"]
CTRLS = json.load(open("data/_case2_ws.json", encoding="utf-8"))["controls"]
KEY_CTRL = CTRLS["行编号"]


def call(body: dict) -> dict:
    req = urllib.request.Request(
        "https://api.mingdao.com/v2/open/worksheet/getFilterRows",
        data=json.dumps({**{"appKey": APP_KEY, "sign": SIGN}, **body}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


resp = call({"worksheetId": WS_ID, "viewId": "", "listType": 1,
             "pageSize": 3, "pageIndex": 1, "useControlId": True})
rows = (resp.get("data") or {}).get("rows") or []
print(f"KEY_CTRL(行编号)={KEY_CTRL}")
for i, r in enumerate(rows[:3]):
    v = r.get(KEY_CTRL)
    rid = r.get("rowid")
    print(f"[{i}] rowid={rid}")
    print(f"    行编号 value_type={type(v).__name__} value={json.dumps(v, ensure_ascii=False)[:80]}")
    # 展示一个文本控件和一个数值控件的值形态
    print(f"    版号 value={json.dumps(r.get(CTRLS['版号']), ensure_ascii=False)[:60]}")
    print(f"    数量 value_type={type(r.get(CTRLS['数量'])).__name__} "
          f"value={json.dumps(r.get(CTRLS['数量']), ensure_ascii=False)[:60]}")