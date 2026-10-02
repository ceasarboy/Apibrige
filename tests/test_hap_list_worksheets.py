# -*- coding: utf-8 -*-
"""HAP 列工作表 / 控件定义（V2 单链路，合并后实现）

合并背景：发行包（主分支）把 HAP 元数据收敛为 **V2 单链路**——
- 列表：GET /v1/open/app/get（query 参数鉴权），sections[].items[] 中 type=0 为工作表
- 控件：POST /v2/open/worksheet/getWorksheetInfo（业务控件 + _SYSTEM_CONTROLS 补 rowid/ctime/utime）

原 GPOST 线的 V3 首取 + V2 回退（list_worksheets_v3/v2、get_worksheet_controls_v3/v2、
get_worksheet_detail_v3，含"V2 对无视图空工作表报请求异常"的绕行）已随目标方实现下线；
本文件按合并后的实现重写契约测试，不打真实端点。
"""
import asyncio
from unittest.mock import AsyncMock, patch

from app.adapters.errors import AdapterError
from app.adapters.hap import HapV3Adapter


def _adapter() -> HapV3Adapter:
    return HapV3Adapter({
        "base_url": "https://api.example.com/",
        "auth_config": {"app_key": "ak123456", "sign": "sg123456"},
    })


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _fake_client_cls(captured: dict, response: _FakeResp):
    """替换 httpx.AsyncClient：记录构造参数与请求，返回预置响应"""

    class _Client:
        def __init__(self, **kw):
            captured["init"] = kw

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, path, **kw):
            captured.update(method="POST", path=path, **kw)
            return response

        async def get(self, path, **kw):
            captured.update(method="GET", path=path, **kw)
            return response

    return _Client


class TestListWorksheetsV2:

    def test_request_shape_and_parse(self):
        """GET /v1/open/app/get + query 鉴权；items[type=0] 才是工作表"""
        adapter = _adapter()
        captured: dict = {}
        payload = {"success": True, "data": {"sections": [
            {"items": [
                {"id": "ws1", "name": "物料", "type": 0, "notes": "主数据"},
                {"id": "page1", "name": "自定义页", "type": 1},
                {"id": "ws2", "name": "订单", "type": 0},
            ]},
        ]}}
        with patch("app.adapters.hap.httpx.AsyncClient",
                   _fake_client_cls(captured, _FakeResp(payload))):
            out = asyncio.run(adapter.list_worksheets())
        assert captured["method"] == "GET"
        assert captured["path"] == "/v1/open/app/get"
        # 鉴权走 query 参数（HAP 服务端要求），不进 header
        assert captured["params"]["appKey"] == "ak123456"
        assert out == [
            {"form_key": "ws1", "original_name": "物料", "remark": "主数据"},
            {"form_key": "ws2", "original_name": "订单", "remark": ""},
        ]

    def test_upstream_failure_raises(self):
        adapter = _adapter()
        captured: dict = {}
        with patch("app.adapters.hap.httpx.AsyncClient",
                   _fake_client_cls(captured, _FakeResp(
                       {"success": False, "error_msg": "appKey 无效"}))):
            try:
                asyncio.run(adapter.list_worksheets())
            except RuntimeError as exc:
                assert "appKey 无效" in str(exc)
            else:                                     # pragma: no cover - 失败即报错
                raise AssertionError("上游失败必须抛出")


class TestWorksheetControlsV2:

    def test_parses_controls_and_appends_system_fields(self):
        """getWorksheetInfo → 业务控件 + 系统只读字段（rowid/ctime/utime）"""
        adapter = _adapter()
        adapter._post_worksheet = AsyncMock(return_value={
            "success": True,
            "data": {"controls": [
                {"controlId": "c1", "alias": "qty", "controlName": "数量", "type": 6},
                {"controlId": "rowid", "alias": "rowid", "controlName": "记录ID", "type": 2},
            ]},
        })
        out = asyncio.run(adapter.get_worksheet_controls("ws1"))
        call_body = adapter._post_worksheet.await_args.args[1]
        assert call_body["worksheetId"] == "ws1"
        ids = [c["controlId"] for c in out]
        assert ids[0] == "c1"                       # 业务控件在前
        assert "ctime" in ids and "utime" in ids     # 系统字段兜底追加
        assert ids.count("rowid") <= 1               # 不重复追加

    def test_upstream_failure_raises_adapter_error(self):
        adapter = _adapter()
        adapter._post_worksheet = AsyncMock(return_value={
            "success": False, "error_msg": "请求异常"})
        try:
            asyncio.run(adapter.get_worksheet_controls("ws1"))
        except AdapterError as exc:
            assert exc.code == "worksheet_metadata_unavailable"
        else:                                         # pragma: no cover - 失败即报错
            raise AssertionError("上游失败必须抛出 AdapterError")
