# -*- coding: utf-8 -*-
"""MES REST API mock 服务器测试（独立验证，不依赖真实 MES 数据库）"""
import json
import threading
from urllib.parse import quote

import httpx
import pytest

from tests.mes_mock_api import MesApiHandler, ThreadingHTTPServer


@pytest.fixture(scope="module")
def mes_api_base():
    """在随机空闲端口启动 MES mock，测试结束后关闭。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), MesApiHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


# trust_env=False：系统代理（如 Clash）开启时 127.0.0.1 请求会被代理劫持返回 502
def _get(base, path):
    return httpx.get(f"{base}{path}", timeout=10.0, trust_env=False)


class TestResponseEnvelope:
    """响应封装契约：success/code/msg 恒在，列表含 rows/count/pageNo/pageSize/pageCount"""

    def test_list_envelope(self, mes_api_base):
        res = _get(mes_api_base, "/product/queryList?pageNo=1&pageSize=20").json()
        assert res["success"] is True
        assert res["code"] == 200
        assert "msg" in res
        for key in ("rows", "count", "pageNo", "pageSize", "pageCount"):
            assert key in res

    def test_entity_envelope(self, mes_api_base):
        res = _get(mes_api_base, "/product/queryById?id=prod-0001").json()
        assert res["success"] is True
        assert res["data"]["productCode"] == "P-8G-DDR5"


class TestProductApi:
    """产品表单：字段与 McProductEntity 对齐"""

    def test_query_list_returns_all(self, mes_api_base):
        res = _get(mes_api_base, "/product/queryList?pageNo=1&pageSize=20").json()
        assert res["count"] == 3
        assert len(res["rows"]) == 3
        first = res["rows"][0]
        # 关键字段存在
        for key in ("id", "productCode", "productName", "productDesc",
                    "productVer", "status", "statusName", "planCode", "planName",
                    "mtlUomName", "createBy", "createDate", "updateBy", "updateDate"):
            assert key in first, f"缺少字段 {key}"

    def test_query_list_filter_by_code(self, mes_api_base):
        res = _get(mes_api_base, "/product/queryList?pageNo=1&pageSize=20&productCode=DDR5").json()
        assert res["count"] == 1
        assert res["rows"][0]["productCode"] == "P-8G-DDR5"

    def test_query_list_pagination(self, mes_api_base):
        res = _get(mes_api_base, "/product/queryList?pageNo=1&pageSize=2").json()
        assert res["count"] == 3
        assert len(res["rows"]) == 2
        assert res["pageCount"] == 2
        # 第 2 页
        res2 = _get(mes_api_base, "/product/queryList?pageNo=2&pageSize=2").json()
        assert len(res2["rows"]) == 1

    def test_query_by_id_not_found(self, mes_api_base):
        res = _get(mes_api_base, "/product/queryById?id=none").json()
        assert res["data"] is None


class TestWorkOrderApi:
    """工单表单：字段与 MpWorkOrderEntity/MpWorkOrderDTO 对齐"""

    def test_list_page_envelope_and_fields(self, mes_api_base):
        res = _get(mes_api_base, "/work/order/listPage?pageNo=1&pageSize=20").json()
        assert res["count"] == 4
        first = res["rows"][0]
        for key in ("id", "woCode", "woType", "woTypeName", "woStatus", "woStatusName",
                    "orderNo", "itemNo", "productCode", "productName", "productDesc",
                    "customerName", "planWaferQty", "planDieQty", "priorityLevel",
                    "factoryName", "beginTime", "orderFinishTime", "createBy", "updateDate"):
            assert key in first, f"缺少字段 {key}"

    def test_list_filter_by_status(self, mes_api_base):
        res = _get(mes_api_base, "/work/order/listPage?pageNo=1&pageSize=20&woStatus=生产中").json()
        assert res["count"] == 1
        assert res["rows"][0]["woStatus"] == "生产中"

    def test_get_detail(self, mes_api_base):
        res = _get(mes_api_base, "/work/order/get?id=wo-0001").json()
        assert res["data"]["woCode"] == "WO20260911001"

    def test_init_returns_enums(self, mes_api_base):
        res = _get(mes_api_base, "/work/order/init").json()
        data = res["data"]
        for key in ("woTypeList", "priorityLeveList", "woStatusList"):
            assert key in data
            assert isinstance(data[key], list) and data[key]


class TestMaterialApi:
    """物料表单：字段与 McMtlMaterialEntity 对齐"""

    def test_list_page_envelope_and_fields(self, mes_api_base):
        res = _get(mes_api_base, "/material/listPage?pageNo=1&pageSize=20").json()
        assert res["success"] is True
        assert res["count"] == 3
        first = res["rows"][0]
        for key in ("id", "materialCode", "materialName", "materialDesc", "revision",
                    "mtlKind", "mtlKindName", "materialNature", "materialNatureName",
                    "mtlUom", "mtlUomname", "activeFlag", "activeFlagName", "issueType",
                    "providerName", "createBy", "createDateStr", "updateDateStr"):
            assert key in first, f"缺少字段 {key}"

    def test_list_filter_by_code(self, mes_api_base):
        res = _get(mes_api_base, "/material/listPage?pageNo=1&pageSize=20&materialCode=SUB").json()
        assert res["count"] == 1
        assert res["rows"][0]["materialCode"] == "SUB-BGA-78"

    def test_get_detail(self, mes_api_base):
        res = _get(mes_api_base, "/material/get?id=mtl-1001").json()
        assert res["success"] is True
        assert res["data"]["materialCode"] == "SUB-BGA-78"

    def test_init_returns_enums(self, mes_api_base):
        res = _get(mes_api_base, "/material/init").json()
        data = res["data"]
        for key in ("mtlKindList", "materialNatureList", "issueTypeList",
                    "expireUodList", "activeFlagList"):
            assert key in data
            assert isinstance(data[key], list) and data[key]


class TestMaterialDelete:
    """物料删除：制造"源侧减少"，供删除同步端到端测试"""

    def test_delete_then_restore(self, mes_api_base):
        import tests.mes_mock_api as m
        original = list(m.MATERIALS)
        try:
            res = _get(mes_api_base, "/material/delete?materialCode=SUB-BGA-78").json()
            assert res["success"] is True
            assert res["data"]["deleted"] == 1
            res2 = _get(mes_api_base, "/material/listPage?pageNo=1&pageSize=20").json()
            assert res2["count"] == 2
            assert all(r["materialCode"] != "SUB-BGA-78" for r in res2["rows"])
        finally:
            m.MATERIALS[:] = original


class TestLotApi:
    """批次表单：字段与 MpLotEntity 对齐"""

    def test_list_envelope_and_fields(self, mes_api_base):
        res = _get(mes_api_base, "/lot/list?pageNo=1&pageSize=20").json()
        assert res["success"] is True
        assert res["count"] == 4
        first = res["rows"][0]
        for key in ("id", "lotNo", "lotType", "lotTypeName", "woId", "woCode", "orderNo",
                    "productCode", "productName", "productDesc", "productVer",
                    "customerId", "customerCode", "customerName",
                    "planDieQty", "planWaferQty", "dieQty", "totalQty", "useQty", "defectQty",
                    "status", "statusName", "priorityLevelName", "parentLotNo",
                    "beginTime", "lotFinishTime", "factoryId", "stepName", "planName",
                    "createBy", "updateDate"):
            assert key in first, f"缺少字段 {key}"

    def test_list_filter_by_status(self, mes_api_base):
        res = _get(mes_api_base, "/lot/list?pageNo=1&pageSize=20&status=已完工").json()
        assert res["count"] == 1
        assert res["rows"][0]["lotNo"] == "LOT-FT-2609001"

    def test_detail(self, mes_api_base):
        res = _get(mes_api_base, "/lot/detail?id=lot-0001").json()
        assert res["data"]["lotNo"] == "LOT-BG-2609001"
        assert res["data"]["woCode"] == "WO20260911001"


class TestBomApi:
    """BOM表单：字段与 McProductBomEntity 对齐"""

    def test_list_page_envelope_and_fields(self, mes_api_base):
        res = _get(mes_api_base, "/mcProductBom/pageList?pageNo=1&pageSize=20").json()
        assert res["success"] is True
        assert res["count"] == 3
        first = res["rows"][0]
        for key in ("id", "productId", "productCode", "productName", "bomName",
                    "bomType", "bomTypeName", "bomVer", "unit", "status", "statusName",
                    "createByName", "updateByName"):
            assert key in first, f"缺少字段 {key}"

    def test_list_filter_by_code(self, mes_api_base):
        res = _get(mes_api_base, "/mcProductBom/pageList?pageNo=1&pageSize=20&productCode=DDR5").json()
        assert res["count"] == 1
        assert res["rows"][0]["bomName"] == "8Gb DDR5 封测BOM"

    def test_query_detail(self, mes_api_base):
        res = _get(mes_api_base, "/mcProductBom/queryDetail?id=bom-0002").json()
        assert res["success"] is True
        assert res["data"]["productCode"] == "P-MCU-32F"


class TestEquipmentApi:
    """设备表单：字段与 McEqpInfoEntity 对齐"""

    def test_list_envelope_and_fields(self, mes_api_base):
        res = _get(mes_api_base, "/eqp/list?pageNo=1&pageSize=20").json()
        assert res["success"] is True
        assert res["count"] == 4
        first = res["rows"][0]
        for key in ("id", "eqpCode", "eqpName", "eqpModel", "eqpDesc",
                    "revstate", "revstateName", "eqptypeName", "eqpKindName",
                    "factoryName", "locationCode", "theoreticalCap", "targetCap",
                    "stageTypeName", "createDateStr"):
            assert key in first, f"缺少字段 {key}"

    def test_list_filter_by_code(self, mes_api_base):
        res = _get(mes_api_base, "/eqp/list?pageNo=1&pageSize=20&eqpCode=WB").json()
        assert res["count"] == 1
        assert res["rows"][0]["eqpName"] == "键合机"

    def test_get_info(self, mes_api_base):
        res = _get(mes_api_base, "/eqp/info?id=eqp-0003").json()
        assert res["success"] is True
        assert res["data"]["eqpCode"] == "EQP-FT-001"


class TestStepApi:
    """工序表单：字段与 McStepDTO 对齐"""

    def test_list_envelope_and_fields(self, mes_api_base):
        res = _get(mes_api_base, "/step/page?pageNo=1&pageSize=20").json()
        assert res["success"] is True
        assert res["count"] == 5
        first = res["rows"][0]
        for key in ("id", "stepCode", "stepName", "sector", "sectorName",
                    "status", "statusName", "stageType", "stageTypeName",
                    "createDateStr", "updateDateStr"):
            assert key in first, f"缺少字段 {key}"

    def test_list_filter_by_code(self, mes_api_base):
        res = _get(mes_api_base, "/step/page?pageNo=1&pageSize=20&stepCode=FT").json()
        assert res["count"] == 1
        assert res["rows"][0]["stepName"] == "FT测试"


class TestRequestFormat:
    """前端把整包参数 JSON 编码后塞进 query（无 key），mock 需能解析"""

    def test_encoded_json_query(self, mes_api_base):
        payload = json.dumps(dict(pageNo=1, pageSize=20, woStatus="已完工"), ensure_ascii=False)
        res = httpx.get(f"{mes_api_base}/work/order/listPage?{quote(payload)}",
                        timeout=10.0, trust_env=False).json()
        assert res["count"] == 1
        assert res["rows"][0]["woStatus"] == "已完工"

    def test_missing_route_404(self, mes_api_base):
        res = httpx.get(f"{mes_api_base}/no/such/route", timeout=10.0, trust_env=False)
        assert res.status_code == 404
        assert res.json()["success"] is False