"""HAP 适配器接线 v2 工作表 API（读行/翻页/加行/删行/控件定义）+ fetch/push 对接"""
from pathlib import Path

p = Path("app/adapters/hap.py")
src = p.read_text(encoding="utf-8")

# 1. fetch_data 改接 v2 工作表读行
old = '''    async def fetch_data(self, endpoint: str, params: dict | None = None) -> Any:
        """HAP V3 GET 拉取数据"""
        headers = self._merge_headers()
        merged_params = {**(params or {}), **self._auth_params()}
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers,
                                      timeout=self.timeout) as client:
            response = await client.get(endpoint, params=merged_params)
            response.raise_for_status()
            return response.json()'''
new = '''    async def fetch_data(self, endpoint: str, params: dict | None = None) -> Any:
        """读取工作表行（v2 getFilterRows，自动翻页）

        worksheet_id 优先取 mapping_config["protocol"].worksheet_id（经
        build_adapter_config 注入 config），否则用 endpoint 传入。
        """
        worksheet_id = self.config.get("worksheet_id") or endpoint
        if not worksheet_id:
            raise ValueError(
                "HAP 缺少必需参数 worksheet_id"
                "（应配置在 mapping_config['protocol'].worksheet_id）")
        return await self.get_worksheet_rows(
            worksheet_id, view_id=self.config.get("view_id"))'''
assert old in src, "fetch block"
src = src.replace(old, new)

# 2. push_data 改接 v2 addRow
old = '''    async def push_data(self, endpoint: str, data: Any, method: str = "POST") -> dict:
        """HAP V3 POST/PUT/DELETE 推送数据"""
        headers = self._merge_headers()
        separator = "&" if "?" in endpoint else "?"
        url_with_auth = endpoint + separator + urllib.parse.urlencode(self._auth_params())
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers,
                                      timeout=self.timeout) as client:
            response = await client.request(method, url_with_auth, json=data)
            response.raise_for_status()
            return {"status_code": response.status_code,
                    "body": response.json() if response.content else None}'''
new = '''    async def push_data(self, endpoint: str, data: Any, method: str = "POST") -> dict:
        """写入工作表行（v2 addRow，逐行新增）"""
        worksheet_id = self.config.get("worksheet_id") or endpoint
        if not worksheet_id:
            raise ValueError(
                "HAP 缺少必需参数 worksheet_id"
                "（应配置在 mapping_config['protocol'].worksheet_id）")
        records = data if isinstance(data, list) else [data]
        row_ids = []
        for rec in records:
            if isinstance(rec, dict) and rec:
                row_ids.append(await self.add_worksheet_row(worksheet_id, rec))
        return {"status_code": 200, "body": {"row_ids": row_ids}}'''
assert old in src, "push block"
src = src.replace(old, new)

# 3. 追加 v2 工作表方法（放在 HAP 专用工具方法注释之前）
marker = "    # ---- HAP 专用工具方法 ----"
v2_methods = '''    # ---- v2 工作表行操作（实测可用；appKey/sign 并入 JSON body） ----

    async def _post_worksheet(self, path: str, body: dict) -> dict:
        headers = self._merge_headers()
        merged = {**self._auth_params(), **body}
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers,
                                      timeout=self.timeout) as client:
            response = await client.post(self.base_url + path, json=merged)
            response.raise_for_status()
            return response.json()

    async def get_worksheet_rows(self, worksheet_id: str,
                                 view_id: str | None = None,
                                 page_size: int = 1000) -> list[dict]:
        """读取工作表全部行（getFilterRows，listType=1，自动翻页）"""
        rows: list[dict] = []
        page = 1
        while True:
            out = await self._post_worksheet(
                "/v2/open/worksheet/getFilterRows",
                {"worksheetId": worksheet_id,
                 **({"viewId": view_id} if view_id else {}),
                 "listType": "1", "pageSize": page_size, "pageIndex": page,
                 "getSystemControl": True})
            if not out.get("success"):
                raise RuntimeError(
                    f"明道云 getFilterRows 失败: {out.get('error_msg')}")
            batch = (out.get("data") or {}).get("rows") or []
            rows.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        return rows

    async def add_worksheet_row(self, worksheet_id: str, controls: dict) -> str | None:
        """新增工作表行（controls = {controlId: 值}），返回 rowId"""
        out = await self._post_worksheet("/v2/open/worksheet/addRow", {
            "worksheetId": worksheet_id,
            "controls": [{"controlId": k, "value": v} for k, v in controls.items()],
        })
        if not out.get("success"):
            raise RuntimeError(f"明道云 addRow 失败: {out.get('error_msg')}")
        return (out.get("data") or {}).get("rowId")

    async def delete_worksheet_row(self, worksheet_id: str, row_id: str,
                                   thorough: bool = True) -> None:
        """删除工作表行（默认物理删除）"""
        out = await self._post_worksheet("/v2/open/worksheet/deleteRow", {
            "worksheetId": worksheet_id, "rowId": row_id,
            "triggerWorkflow": False, "thoroughDelete": thorough,
        })
        if not out.get("success"):
            raise RuntimeError(f"明道云 deleteRow 失败: {out.get('error_msg')}")

    async def get_worksheet_controls(self, worksheet_id: str) -> list[dict]:
        """获取工作表控件（字段）定义：controlId/name/alias/type/required"""
        out = await self._post_worksheet("/v2/open/worksheet/getWorksheetInfo", {
            "worksheetId": worksheet_id,
        })
        return (out.get("data") or {}).get("controls") or []

'''
assert marker in src, "tools marker"
src = src.replace(marker, v2_methods + marker, 1)

p.write_text(src, encoding="utf-8", newline="\n")
print("hap v2 wiring complete")
