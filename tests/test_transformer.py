# -*- coding: utf-8 -*-
"""transform_payload 列表包装键解析（MES ListResponse 顶层 rows）"""
from app.services.transformer import transform_payload

MAPPING = {"fields": [
    {"source": "materialCode", "target": "物料编码", "transform": "string"},
    {"source": "materialName", "target": "物料名称", "transform": "string"},
]}


class TestTransformPayloadRows:
    def test_top_level_rows_list(self):
        source = {
            "success": True, "code": 200, "msg": "查询成功",
            "rows": [
                {"materialCode": "SUB-BGA-78", "materialName": "BGA78 基板"},
                {"materialCode": "WIRE-AU-1.0", "materialName": "金线 1.0mil"},
            ],
            "count": 2, "pageNo": 1, "pageSize": 20, "pageCount": 1,
        }
        out = transform_payload(source, MAPPING)
        assert len(out) == 2
        assert out[0]["物料编码"] == "SUB-BGA-78"
        assert out[1]["物料名称"] == "金线 1.0mil"

    def test_empty_rows(self):
        source = {"success": True, "rows": [], "count": 0}
        assert transform_payload(source, MAPPING) == []