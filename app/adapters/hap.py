"""HAP V3 协议适配器（明道云开放平台）

明道云开放 API 鉴权方式：**query 参数** appKey + sign（不是请求头）。
测试路径：GET /v1/open/app/get（返回应用信息）。

HAP V3 API 详情见 skills/hap/SKILL.md
"""
import urllib.parse
import httpx
import hashlib
import time
from datetime import datetime, timedelta
from typing import Any
from app.adapters.base import ProtocolAdapter
from app.adapters.errors import AdapterError

# 明道云 utime 字符串格式（getFilterRows 行内即此格式，过滤值必须同格式比较）
_HAP_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _shift_seconds(ts: str, seconds: int) -> str:
    """时间串平移（增量水位线回退重叠窗口用）；解析失败原样返回（交给服务端判定）"""
    try:
        return (datetime.strptime(str(ts), _HAP_TS_FORMAT)
                + timedelta(seconds=seconds)).strftime(_HAP_TS_FORMAT)
    except (ValueError, TypeError):
        return str(ts)


_CONTROL_CACHE: dict[tuple, tuple[float, list]] = {}

# V2 行数据自带这些只读键，但 getWorksheetInfo 通常不返回对应控件。
_SYSTEM_CONTROLS = (
    {"controlId": "ctime", "alias": "ctime", "controlName": "创建时间", "type": 16,
     "required": True, "readOnly": True},
    {"controlId": "utime", "alias": "utime", "controlName": "更新时间", "type": 16,
     "required": True, "readOnly": True},
    {"controlId": "rowid", "alias": "rowid", "controlName": "记录ID", "type": 2,
     "required": True, "readOnly": True, "primaryKey": True},
)


def _contains_watermark(value):
    if isinstance(value, str):
        return '{{last_watermark}}' in value
    if isinstance(value, dict):
        return any(_contains_watermark(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_watermark(item) for item in value)
    return False


def normalize_field_keys(rows: list[dict], controls: list[dict], mode: str) -> list[dict]:
    if mode == 'raw':
        return rows
    identities = {}
    for c in controls:
        cid, alias = c.get('controlId'), c.get('alias')
        if alias and alias in identities and identities[alias] != cid:
            raise AdapterError('field_key_conflict', f'重复字段别名: {alias}', 'field_resolve')
        if alias:
            identities[alias] = cid
    result = []
    for index, row in enumerate(rows):
        out = dict(row)
        for c in controls:
            cid, alias = c.get('controlId'), c.get('alias')
            if not cid or not alias or cid == alias:
                continue
            if cid in row and alias in row and row[cid] != row[alias]:
                raise AdapterError('field_key_conflict', f'字段ID与别名值不同: {alias}', 'field_resolve', details={'index': index})
            if cid not in row and alias not in row:
                continue
            value = row[alias] if alias in row else row[cid]
            if mode in ('alias', 'alias_or_id'):
                out[alias] = value
            if mode in ('id', 'alias_or_id'):
                out[cid] = value
            if mode == 'alias':
                out.pop(cid, None)
            elif mode == 'id':
                out.pop(alias, None)
        result.append(out)
    return result


class HapV3Adapter(ProtocolAdapter):
    """明道云 HAP V3 API 适配器

    Config 参数:
        base_url: API 根地址（默认 https://api.mingdao.com）
        hap_appkey: 应用 API 密钥（从 auth_config 或 config 根级别获取）
        hap_sign: 签名密钥
    """

    PROTOCOL_NAME = "hapv3"

    def __init__(self, config: dict):
        super().__init__(config)
        # 密钥来源：config 根级别 / auth_config，键名兼容
        # appKey: app_key | appKey | hap_appkey | HAP-Appkey
        # sign:   sign | hap_sign | HAP-Sign
        auth = config.get("auth_config", {}) or {}

        def _pick(*names: str) -> str:
            for source in (config, auth):
                for n in names:
                    if source.get(n):
                        return str(source[n])
            return ""

        self._appkey = _pick("app_key", "appKey", "hap_appkey", "HAP-Appkey")
        self._sign = _pick("sign", "hap_sign", "HAP-Sign")

    def _auth_params(self) -> dict[str, str]:
        """HAP V3 鉴权 query 参数（appKey + sign）"""
        params: dict[str, str] = {}
        if self._appkey:
            params["appKey"] = self._appkey
        if self._sign:
            params["sign"] = self._sign
        return params

    def _merge_headers(self, extra: dict | None = None) -> dict[str, str]:
        """合并 client headers（Content-Type + 连接级自定义）"""
        merged = {"Content-Type": "application/json"}
        merged.update(self.headers or {})
        if extra:
            merged.update(extra)
        return merged

    async def fetch_data(self, endpoint: str, params: dict | None = None) -> Any:
        """读取工作表行（v2 getFilterRows，自动翻页）

        worksheet_id 优先取 mapping_config["protocol"].worksheet_id（经
        build_adapter_config 注入 config），否则用 endpoint 传入。
        """
        worksheet_id = self.config.get("worksheet_id") or endpoint
        if not worksheet_id:
            raise ValueError(
                "HAP 缺少必需参数 worksheet_id"
                "（应配置在 mapping_config['protocol'].worksheet_id）")
        rows = await self.get_worksheet_rows(
            worksheet_id, view_id=self.config.get("view_id"), filters=self.config.get('filters'),
            limit=self.config.get('sample_limit'))
        return await self.normalize_records(worksheet_id, rows)

    async def normalize_records(self, worksheet_id: str, rows: list[dict]) -> list[dict]:
        mode = self.config.get('field_keys', 'raw')
        if mode == 'raw' or not rows:
            return rows
        # 凭据指纹隔离相同地址上的不同应用，缓存中不保存凭据。
        fingerprint = hashlib.sha256((self._appkey + ':' + self._sign).encode()).hexdigest()
        key = (self.base_url, self.config.get('connection_key'), fingerprint, worksheet_id)
        cached = _CONTROL_CACHE.get(key)
        if cached is None or time.monotonic() - cached[0] > 60:
            controls = await self.get_worksheet_controls(worksheet_id)
            _CONTROL_CACHE[key] = (time.monotonic(), controls)
        else:
            controls = cached[1]
        known = {c.get('controlId') for c in controls}
        missing = [ref.get('field_id') for ref in self.config.get('source_refs', []) if ref.get('field_id') not in known]
        if missing and cached is not None:
            controls = await self.get_worksheet_controls(worksheet_id)
            _CONTROL_CACHE[key] = (time.monotonic(), controls)
            known = {c.get('controlId') for c in controls}
            missing = [cid for cid in missing if cid not in known]
        if missing:
            raise AdapterError('schema_changed', '映射引用的源控件已不存在，请刷新字段', 'field_resolve')
        try:
            return normalize_field_keys(rows, controls, mode)
        except AdapterError:
            if cached is None:
                raise
            controls = await self.get_worksheet_controls(worksheet_id)
            _CONTROL_CACHE[key] = (time.monotonic(), controls)
            return normalize_field_keys(rows, controls, mode)

    async def push_data(self, endpoint: str, data: Any, method: str = "POST") -> dict:
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
        return {"status_code": 200, "body": {"row_ids": row_ids}}

    def _masked_request_url(self) -> str:
        """展示用请求 URL：appKey/sign 值打码"""
        params = self._auth_params()
        masked = {k: (str(v)[:6] + "***" if v else v) for k, v in params.items()}
        query = urllib.parse.urlencode(masked)
        return f"GET {self.base_url}/v1/open/app/get?{query}"

    async def test_connection(self) -> dict:
        """测试 HAP 连接 — 调用 /v1/open/app/get 验证（真实开放 API）"""
        import time
        headers = self._merge_headers()
        start = time.time()
        request_url = self._masked_request_url()
        try:
            async with httpx.AsyncClient(base_url=self.base_url, headers=headers,
                                          timeout=self.timeout) as client:
                response = await client.get("/v1/open/app/get",
                                            params=self._auth_params())
                latency = int((time.time() - start) * 1000)
                details = {
                    "request": request_url,
                    "status_code": response.status_code,
                    "response_excerpt": response.text[:1000],
                }
                if response.status_code != 200:
                    return {"status": "failed", "latency_ms": latency,
                            "error": f"HTTP {response.status_code}: {response.text[:200]}",
                            **details}
                data = response.json()
                if isinstance(data, dict) and data.get("success") is False:
                    return {"status": "failed", "latency_ms": latency,
                            "error": f"明道云返回失败: "
                                     f"{data.get('error_msg') or data}",
                            **details}
                app_info = (data.get("data") or {}) if isinstance(data, dict) else {}
                return {"status": "ok", "latency_ms": latency,
                        "app_name": app_info.get("name", "unknown"), **details}
        except Exception as e:
            return {"status": "failed", "latency_ms": 0, "error": str(e)}

    # ---- v2 工作表行操作（实测可用；appKey/sign 并入 JSON body） ----

    async def list_worksheets(self) -> list[dict]:
        """V2：GET /v1/open/app/get（query 参数鉴权，实测可用）

        返回 data.sections[].items[]，type=0 为工作表
        （type=1 等为自定义页面，剔除）。
        """
        headers = self._merge_headers()
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers,
                                      timeout=self.timeout) as client:
            response = await client.get("/v1/open/app/get",
                                        params=self._auth_params())
            response.raise_for_status()
            data = response.json()
        if not (isinstance(data, dict) and data.get("success")):
            raise RuntimeError(
                f"明道云 app/get(V2) 失败: "
                f"{data.get('error_msg') if isinstance(data, dict) else data}")
        app_info = data.get("data") or {}
        forms: list[dict] = []
        for section in app_info.get("sections") or []:
            for item in (section.get("items") or []):
                if not isinstance(item, dict) or item.get("type") != 0:
                    continue  # type=1 等为自定义页面，不是工作表
                forms.append({
                    "form_key": str(item.get("id") or ""),
                    "original_name": str(item.get("name") or ""),
                    "remark": str(item.get("notes") or item.get("remark") or ""),
                })
        return [f for f in forms if f["form_key"]]

    async def _post_worksheet(self, path: str, body: dict) -> dict:
        if getattr(self, 'before_request', None):
            await self.before_request()
        headers = self._merge_headers()
        merged = {**self._auth_params(), **body}
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers,
                                      timeout=self.timeout) as client:
            response = await client.post(self.base_url + path, json=merged)
            response.raise_for_status()
            return response.json()

    async def get_worksheet_rows(self, worksheet_id: str,
                                 view_id: str | None = None,
                                 filters: list[dict] | None = None,
                                 page_size: int = 1000, limit: int | None = None,
                                 max_pages: int = 100,
                                 sort: tuple[str, str] | None = None,
                                 return_total: bool = False
                                 ) -> list[dict] | tuple[list[dict], int | None]:
        """读取工作表全部行（getFilterRows，listType=1，自动翻页）

        - `limit`：取够即停（BI 表单读取上限用，返回不超过 limit 行）；
        - `max_pages`：防翻页失控（默认 100 页 = 10 万行；BI 增量拉取显式调小）；
        - `filters`（**参数名必须是 filters**，实测 `filterControls` 被服务端静默忽略）：
          `[{"controlId","dataType","filterType","value"}]`，filterType 13=大于、15=小于、
          2=等于、14=大于等于、11=区间；日期时间字段 dataType=16、数值=6、文本=2；
        - `sort`：`(sortId, "asc"|"desc")`，实测跨页稳定（BI 增量拉取按 utime 降序）；
        - `return_total=True`：返回 `(rows, total)`，total 取自响应 data.total
          （服务端**过滤后**的总数，增量判规模用）。
        """
        rows: list[dict] = []
        scan_bytes = 0
        expected_total = None
        total: int | None = None
        page = 1
        if limit is not None:
            page_size = min(page_size, max(1, limit))
        while True:
            if page > max_pages:
                raise RuntimeError(
                    f"明道云 getFilterRows 超过翻页上限（{max_pages} 页，已取 {len(rows)} 行），"
                    "请缩小数据集或调大 max_pages")
            resolved_filters = filters
            if filters is not None:
                from app.services.api_client import resolve_variables
                runtime_vars = self.config.get('_runtime_vars') or {}
                resolved_filters = resolve_variables(filters, runtime_vars)
                if runtime_vars.get('last_watermark') is None:
                    resolved_filters = [item for item in filters if not (isinstance(item, dict) and _contains_watermark(item))]
                if not isinstance(resolved_filters, list) or any(not isinstance(item, dict) for item in resolved_filters):
                    raise AdapterError('source_filter_invalid', 'HAP 过滤条件必须是对象数组', 'source_read')
            sort_body = {}
            if sort:
                # sortType 的**值**被服务端忽略（真机实测 asc/desc/2/ASC 同序），
                # 但 sortId 本身会把默认顺序从"创建序"切到"该字段序"——保留参数
                # 仅为语义自文档；P4 增量正确性靠 filters（utime > 水位线），不靠排序
                sort_body = {"sortId": sort[0], "sortType": sort[1]}
            elif self.config.get('_stable_scan'):
                # 稳定扫描（删差/对账/快照）：按 rowid 确定性**升序**分页。
                # 真机实测（2026-10-01，api.mingdao.com，scripts/hap_sorttype_check.py）：
                # 该端点真实的方向参数是 isAsc（true=升序/false=降序）；
                # sortType 的值被忽略（有 sortId 时默认降序）。第二轮审查曾误判
                # isAsc 为笔误并改成 sortType——真机回归推翻该结论，此为还原。
                sort_body = {"sortId": "rowid", "isAsc": True}
            out = await self._post_worksheet(
                "/v2/open/worksheet/getFilterRows",
                {"worksheetId": worksheet_id,
                 **({"viewId": view_id} if view_id else {}),
                 "listType": "1", "pageSize": page_size, "pageIndex": page,
                 **({"filters": resolved_filters} if resolved_filters is not None else {}),
                 **sort_body,
                 "getSystemControl": True})
            if not out.get("success"):
                raise RuntimeError(
                    f"明道云 getFilterRows 失败: {out.get('error_msg')}")
            data = out.get("data") or {}
            if isinstance(data.get("total"), int):
                total = data["total"]
            batch = data.get("rows") or []
            if self.config.get('_stable_scan'):
                from app.services.incremental import serialize
                if not isinstance(data.get('rows'), list) or len(batch) > page_size:
                    raise AdapterError('source_records_invalid', 'HAP 分页未返回合法记录数组', 'source_read')
                total_raw = data.get('total')
                if total_raw is not None:
                    if type(total_raw) is not int or total_raw < 0 or (expected_total is not None and total_raw != expected_total):
                        raise AdapterError('incremental_source_changed', 'HAP 分页总数变化或无效，请重试本轮', 'source_read')
                    expected_total = total_raw
                scan_bytes += len(serialize(batch).encode('utf-8'))
                if len(rows) + len(batch) > self.config['_scan_max_rows'] or scan_bytes > self.config['_scan_max_bytes']:
                    raise AdapterError('incremental_round_too_large', 'HAP 完整读取超过条数或字节上限', 'source_read')
            rows.extend(batch)
            if limit is not None and len(rows) >= limit:
                rows = rows[:limit]
                break
            if len(batch) < page_size:
                if expected_total is not None and len(rows) != expected_total:
                    raise AdapterError('incremental_source_changed', 'HAP 分页返回记录数不完整', 'source_read')
                break
            page += 1
        if return_total:
            return rows, total
        return rows

    async def get_rows_since(
        self, worksheet_id: str, since: str | None, *, inclusive: bool = False,
        view_id: str | None = None,
        page_size: int = 1000, max_pages: int = 10,
    ) -> tuple[list[dict], int | None]:
        """按 `utime` 取"自 since 起"的行，返回 `(rows, total)`

        - `inclusive=False`（默认）→ `utime > since`（**严格大于**，实测语义）：
          真增量（新增 + 变更），水位线用严格大于才不会重复扫旧行；
        - `inclusive=True` → `utime >= since`：取"与水位线同秒"的边界行，
          用于刷新边界（防止同秒内的后续变更漏采）；调用方按行数上限决定是否采用，
          行数过大即跳过（不可让边界集=整表的表把增量拖成全量）；
        - `since=None` → 只取第一页（建立基线，不全量扫）；
        - `sortId=utime` 降序 + 翻页上限：异常放大时宁少不多（调用方按 total 判定）。
        """
        filters: list[dict] = []
        if since:
            filters.append({
                "controlId": "utime", "dataType": 16,
                "filterType": 14 if inclusive else 13,     # 14=大于等于 / 13=大于
                "value": str(since),
            })
        return await self.get_worksheet_rows(          # type: ignore[return-value]
            worksheet_id, view_id=view_id, page_size=page_size,
            max_pages=max_pages, filters=filters or None,
            sort=("utime", "desc"), return_total=True)

    async def get_rows_with_key(self, worksheet_id: str, key_control_id: str,
                                view_id: str | None = None) -> dict[str, str]:
        """读取目标现有行，返回 {业务键值: rowId}

        用于删除同步对账与 push 去重。getFilterRows 返回扁平结构
        （controlId 是行顶层键，值均为标量字符串），key_control_id 即
        目标侧业务键对应的控件 ID。键值取 str().strip() 归一。
        """
        rows = await self.get_worksheet_rows(worksheet_id, view_id)
        result: dict[str, str] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            v = r.get(key_control_id)
            if v is None:
                continue
            key_val = str(v).strip()
            if not key_val:
                continue
            row_id = r.get("rowid")
            if row_id is not None:
                result[key_val] = str(row_id)
        return result

    async def add_worksheet_row(self, worksheet_id: str, controls: dict) -> str | None:
        """新增工作表行（controls = {controlId: 值}），返回 rowId"""
        out = await self._post_worksheet("/v2/open/worksheet/addRow", {
            "worksheetId": worksheet_id,
            "controls": [{"controlId": k, "value": v} for k, v in controls.items()],
        })
        if not out.get("success"):
            raise RuntimeError(f"明道云 addRow 失败: {out.get('error_msg')}")
        return out.get("data")  # v2 直接返回 rowId 字符串

    async def add_worksheet_rows(self, worksheet_id: str, rows: list[list[dict]],
                                 trigger_workflow: bool = False) -> list[str]:
        """V2 批量新增：二维控件数组。调用者必须回查，不依赖返回 ID 顺序。"""
        out = await self._post_worksheet('/v2/open/worksheet/addRows', {
            'worksheetId': worksheet_id, 'rows': rows, 'returnRowIds': True,
            'triggerWorkflow': trigger_workflow,
        })
        ids = out.get('data')
        if (out.get('success') is not True or not isinstance(ids, list)
                or len(ids) != len(rows) or any(not isinstance(v, str) or not v for v in ids)
                or len(set(ids)) != len(ids)):
            raise AdapterError('hap_add_unconfirmed', '批量新增返回结果未完整确认，必须回查', 'hap_add')
        return ids

    async def edit_worksheet_rows(self, worksheet_id: str, row_ids: list[str],
                                  controls: list[dict], trigger_workflow: bool = False) -> None:
        """V2 批量更新：所有 rowIds 共用同一份 controls。"""
        out = await self._post_worksheet('/v2/open/worksheet/editRows', {
            'worksheetId': worksheet_id, 'rowIds': row_ids, 'controls': controls,
            'triggerWorkflow': trigger_workflow,
        })
        if out.get('success') is not True or out.get('data') is not True:
            raise AdapterError('hap_edit_unconfirmed', '批量更新返回结果未确认，必须回查', 'hap_edit')

    async def get_worksheet_row(self, worksheet_id: str, row_id: str) -> dict:
        if getattr(self, 'before_request', None):
            await self.before_request()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(self.base_url.rstrip('/') + '/v2/open/worksheet/getRowById',
                params={**self._auth_params(), 'worksheetId': worksheet_id, 'rowId': row_id},
                headers=self._merge_headers())
            response.raise_for_status()
            out = response.json()
        if out.get('success') is not True or not isinstance(out.get('data'), dict):
            raise AdapterError('hap_readback_unavailable', '目标详情未返回可比对记录', 'hap_verify')
        return out['data']

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
        """获取业务控件与系统只读字段，供字段目录及源引用校验共用。"""
        out = await self._post_worksheet("/v2/open/worksheet/getWorksheetInfo", {
            "worksheetId": worksheet_id,
        })
        if not out.get("success"):
            raise AdapterError('worksheet_metadata_unavailable', '无法获取明道云字段定义', 'field_resolve')
        controls = (out.get("data") or {}).get("controls") or []
        system_ids = {c['controlId'] for c in _SYSTEM_CONTROLS}
        return [c for c in controls if c.get('controlId') not in system_ids] + [
            dict(c) for c in _SYSTEM_CONTROLS
        ]

    # ---- HAP 专用工具方法 ----

    @staticmethod
    def build_worksheet_url(worksheet_id: str) -> str:
        """构建工作表 API 路径"""
        return f"/v3/app/worksheets/{worksheet_id}/rows"

    @staticmethod
    def build_filter(conditions: list[dict], logic: str = "AND") -> dict:
        """构建 HAP Filter 对象

        Args:
            conditions: [{"field": "字段ID", "operator": "eq", "value": ["值"]}, ...]
            logic: AND/OR
        """
        return {
            "type": "group",
            "logic": logic,
            "children": [
                {"type": "condition", "field": c["field"],
                 "operator": c["operator"], "value": c["value"]}
                for c in conditions
            ],
        }

    @staticmethod
    def build_update_fields(fields: dict) -> list[dict]:
        """构建 HAP 更新字段列表: {"字段ID": 值} → [{"id": "...", "value": ...}]"""
        return [{"id": k, "value": v} for k, v in fields.items()]
