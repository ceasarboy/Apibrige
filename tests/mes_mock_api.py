# -*- coding: utf-8 -*-
"""MES 项目 REST API 模拟服务器（测试替身）

背景：E:\\mes-project 是 Java Spring Cloud + Nacos 微服务 MES，后端因 Oracle 不可达无法运行。
本模块用标准库 http.server 起一个零依赖的 mock，还原其 REST 契约，供 APIBridge 做 REST 源/目标的
联调与功能实测（不依赖真实 MES 数据库）。

依据 E:\\mes-project 代码还原的关键契约：

1. 响应封装（cn.tfinfo.microservice.baseconstruct.response）：
   - BaseResponse   : {success, code, msg}
   - EntityResponse :  + data   （详情）
   - ListResponse   :  + rows, count, pageNo, pageSize, pageCount （分页列表）
   前端判定：`res.data.success !== true` 视为失败，提示 `res.data.msg`。

2. 请求格式：
   - 前端把全部参数 JSON.stringify 后 encodeURIComponent 塞进 query（`?%7B...%7D`，无 key）。
   - 也兼容标准 query 形式 `?pageNo=1&pageSize=20`。
   - 鉴权：前端带 `Authorization: <ticket>` 头；mock 不校验，仅透传忽略。

3. 表单/字段（依据实体类）：
   - 产品 McProductEntity（mes-basic）：/product/queryList、/product/queryById
   - 工单 MpWorkOrderEntity/MpWorkOrderDTO（mes）：/work/order/listPage、/work/order/get、/work/order/init

运行：`python -m tests.mes_mock_api` 或 `python tests/mes_mock_api.py`（默认 8888 端口，
可用环境变量 MES_MOCK_PORT 覆盖）。
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

# 服务默认端口（可被 MES_MOCK_PORT 环境变量覆盖）
DEFAULT_PORT = 8888

# ---------------------------------------------------------------------------
# 种子数据（2 个表单，字段与实体类对齐）
# ---------------------------------------------------------------------------

# 产品：McProductEntity 关键字段（id/delFlag/createBy/createDate/updateBy/updateDate 来自 DataEntity）
PRODUCTS = [
    {
        "id": "prod-0001", "parentId": None, "productVerId": "pv-0001",
        "baseProductId": "bp-0001", "productId": "bp-0001",
        "productCode": "P-8G-DDR5", "productName": "8Gb DDR5 内存芯片",
        "productDesc": "FBGA-78", "productVer": 1.0, "type": "封测产品",
        "typeName": "封测产品", "status": "Y", "statusName": "启用",
        "levelFlag": "Y", "planId": "plan-001", "planCode": "ROUTE-DDR5",
        "planName": "DDR5 封测工艺", "planSource": "系统",
        "materialId": "mtl-0001", "materialNature": "成品", "mtlKind": "CP",
        "mtlKindName": "芯片", "mtltypeId": "mtype-01", "mtltypeName": "内存类",
        "mtlUom": "uom-pcs", "mtlUomName": "颗", "series": "DDR",
        "seriesName": "DDR 系列", "prodType": "封测", "prodTypeName": "封测",
        "pakType": "盘装", "pakTypName": "盘装", "standardNum": 1000,
        "yield": 0.98, "combinedFlag": "N",
        "delFlag": "0", "remarks": "", "createBy": "admin",
        "createDate": "2026-08-01 10:00:00", "updateBy": "admin",
        "updateDate": "2026-08-01 10:00:00",
    },
    {
        "id": "prod-0002", "parentId": None, "productVerId": "pv-0002",
        "baseProductId": "bp-0002", "productId": "bp-0002",
        "productCode": "P-MCU-32F", "productName": "32位 MCU 微控制器",
        "productDesc": "QFP-64", "productVer": 2.1, "type": "封测产品",
        "typeName": "封测产品", "status": "Y", "statusName": "启用",
        "levelFlag": "N", "planId": "plan-002", "planCode": "ROUTE-MCU",
        "planName": "MCU 封测工艺", "planSource": "系统",
        "materialId": "mtl-0002", "materialNature": "成品", "mtlKind": "FT",
        "mtlKindName": "芯片", "mtltypeId": "mtype-02", "mtltypeName": "MCU类",
        "mtlUom": "uom-pcs", "mtlUomName": "颗", "series": "MCU",
        "seriesName": "MCU 系列", "prodType": "封测", "prodTypeName": "封测",
        "pakType": "编带", "pakTypName": "编带", "standardNum": 2500,
        "yield": 0.95, "combinedFlag": "N",
        "delFlag": "0", "remarks": "", "createBy": "admin",
        "createDate": "2026-08-03 14:30:00", "updateBy": "admin",
        "updateDate": "2026-08-03 14:30:00",
    },
    {
        "id": "prod-0003", "parentId": None, "productVerId": "pv-0003",
        "baseProductId": "bp-0003", "productId": "bp-0003",
        "productCode": "P-PM-PMIC", "productName": "电源管理芯片 PMIC",
        "productDesc": "WLCSP-20", "productVer": 1.3, "type": "封测产品",
        "typeName": "封测产品", "status": "Y", "statusName": "启用",
        "levelFlag": "N", "planId": "plan-003", "planCode": "ROUTE-PMIC",
        "planName": "PMIC 封测工艺", "planSource": "系统",
        "materialId": "mtl-0003", "materialNature": "成品", "mtlKind": "FT",
        "mtlKindName": "芯片", "mtltypeId": "mtype-03", "mtltypeName": "电源类",
        "mtlUom": "uom-pcs", "mtlUomName": "颗", "series": "PMIC",
        "seriesName": "电源系列", "prodType": "封测", "prodTypeName": "封测",
        "pakType": "盘装", "pakTypName": "盘装", "standardNum": 3000,
        "yield": 0.97, "combinedFlag": "N",
        "delFlag": "0", "remarks": "", "createBy": "admin",
        "createDate": "2026-08-05 09:00:00", "updateBy": "admin",
        "updateDate": "2026-08-05 09:00:00",
    },
]

# 工单：MpWorkOrderEntity/MpWorkOrderDTO 关键字段
WORK_ORDERS = [
    {
        "id": "wo-0001", "woCode": "WO20260911001", "woType": "封测工单",
        "woTypeName": "封测工单", "woStatus": "生产中", "woStatusName": "生产中",
        "orderNo": "PO202609001", "itemNo": "XM-2026-001",
        "customerOrderNo": "CO-2026-1001", "productId": "bp-0001",
        "productCode": "P-8G-DDR5", "productName": "8Gb DDR5 内存芯片",
        "productDesc": "FBGA-78", "productVer": "1.0", "customerId": "cust-01",
        "customerName": "华为海思", "planWaferQty": 1000, "planDieQty": 8000,
        "waferQty": 600, "dieQty": 4800, "standardQty": 8000,
        "primaryDieFlag": "Y", "priorityLevel": "紧急", "priorityLevelName": "紧急",
        "factoryId": "factory-01", "factoryName": "封测一厂", "componyId": "comp-01",
        "beginTime": "2026-09-10 08:00:00", "orderFinishTime": "2026-09-20 18:00:00",
        "waferId": "wafer-001", "waferCode": "W-8G", "chipSize": "8x8",
        "delFlag": "0", "remarks": "", "createBy": "admin",
        "createDate": "2026-09-10 08:00:00", "updateBy": "admin",
        "updateDate": "2026-09-11 10:00:00",
    },
    {
        "id": "wo-0002", "woCode": "WO20260911002", "woType": "封测工单",
        "woTypeName": "封测工单", "woStatus": "确认投产", "woStatusName": "确认投产",
        "orderNo": "PO202609002", "itemNo": "XM-2026-002",
        "customerOrderNo": "CO-2026-1002", "productId": "bp-0002",
        "productCode": "P-MCU-32F", "productName": "32位 MCU 微控制器",
        "productDesc": "QFP-64", "productVer": "2.1", "customerId": "cust-02",
        "customerName": "比亚迪半导体", "planWaferQty": 2000, "planDieQty": 20000,
        "waferQty": 0, "dieQty": 0, "standardQty": 20000,
        "primaryDieFlag": "Y", "priorityLevel": "一般", "priorityLevelName": "一般",
        "factoryId": "factory-02", "factoryName": "封测二厂", "componyId": "comp-02",
        "beginTime": "2026-09-15 08:00:00", "orderFinishTime": "2026-09-28 18:00:00",
        "waferId": "wafer-002", "waferCode": "W-MCU", "chipSize": "6x6",
        "delFlag": "0", "remarks": "", "createBy": "admin",
        "createDate": "2026-09-11 09:00:00", "updateBy": "admin",
        "updateDate": "2026-09-11 09:00:00",
    },
    {
        "id": "wo-0003", "woCode": "WO20260910003", "woType": "封测工单",
        "woTypeName": "封测工单", "woStatus": "已完工", "woStatusName": "已完工",
        "orderNo": "PO202609003", "itemNo": "XM-2026-003",
        "customerOrderNo": "CO-2026-1003", "productId": "bp-0003",
        "productCode": "P-PM-PMIC", "productName": "电源管理芯片 PMIC",
        "productDesc": "WLCSP-20", "productVer": "1.3", "customerId": "cust-03",
        "customerName": "闻泰科技", "planWaferQty": 3000, "planDieQty": 30000,
        "waferQty": 3000, "dieQty": 29100, "standardQty": 30000,
        "primaryDieFlag": "N", "priorityLevel": "加急", "priorityLevelName": "加急",
        "factoryId": "factory-01", "factoryName": "封测一厂", "componyId": "comp-01",
        "beginTime": "2026-09-01 08:00:00", "orderFinishTime": "2026-09-10 18:00:00",
        "waferId": "wafer-003", "waferCode": "W-PMIC", "chipSize": "4x4",
        "delFlag": "0", "remarks": "返工工单", "createBy": "admin",
        "createDate": "2026-09-01 08:00:00", "updateBy": "admin",
        "updateDate": "2026-09-10 18:00:00",
    },
    {
        "id": "wo-0004", "woCode": "WO20260911004", "woType": "封测工单",
        "woTypeName": "封测工单", "woStatus": "制单", "woStatusName": "制单",
        "orderNo": "PO202609004", "itemNo": "XM-2026-004",
        "customerOrderNo": "CO-2026-1004", "productId": "bp-0001",
        "productCode": "P-8G-DDR5", "productName": "8Gb DDR5 内存芯片",
        "productDesc": "FBGA-78", "productVer": "1.0", "customerId": "cust-01",
        "customerName": "华为海思", "planWaferQty": 500, "planDieQty": 4000,
        "waferQty": 0, "dieQty": 0, "standardQty": 4000,
        "primaryDieFlag": "Y", "priorityLevel": "一般", "priorityLevelName": "一般",
        "factoryId": "factory-01", "factoryName": "封测一厂", "componyId": "comp-01",
        "beginTime": "2026-09-20 08:00:00", "orderFinishTime": "2026-09-30 18:00:00",
        "waferId": "wafer-001", "waferCode": "W-8G", "chipSize": "8x8",
        "delFlag": "0", "remarks": "", "createBy": "admin",
        "createDate": "2026-09-11 11:00:00", "updateBy": "admin",
        "updateDate": "2026-09-11 11:00:00",
    },
]

# 工单 init：下拉枚举（woTypeList / priorityLeveList / woStatusList）
WORK_ORDER_INIT = {
    "woTypeList": [
        {"dictCode": "封测工单", "dictName": "封测工单"},
        {"dictCode": "研发工单", "dictName": "研发工单"},
    ],
    "priorityLeveList": [
        {"dictCode": "一般", "dictName": "一般"},
        {"dictCode": "紧急", "dictName": "紧急"},
        {"dictCode": "加急", "dictName": "加急"},
    ],
    "woStatusList": [
        {"dictCode": "制单", "dictName": "制单"},
        {"dictCode": "确认投产", "dictName": "确认投产"},
        {"dictCode": "已开批", "dictName": "已开批"},
        {"dictCode": "生产中", "dictName": "生产中"},
        {"dictCode": "待完工", "dictName": "待完工"},
        {"dictCode": "已完工", "dictName": "已完工"},
    ],
}


# 物料：McMtlMaterialEntity 关键字段（mes-basic，/material 前缀）
MATERIALS = [
    {
        "id": "mtl-1001", "materialCode": "SUB-BGA-78", "materialAllCode": "SUB-BGA-78",
        "materialName": "BGA78 基板", "materialDesc": "BGA-78 载板", "revision": "A",
        "mtlKind": "物料", "mtlKindName": "物料", "materialNature": "主要材料",
        "materialNatureName": "主要材料", "mtlcategoryCode": "C-SUB", "mtlcategoryName": "基板类",
        "mtltypeId": "mtype-sub", "mtltypeName": "基板", "mtlType": "基板",
        "mtlUom": "uom-pcs", "mtlUomname": "片", "safetyStock": 1000, "stdNum": 0,
        "activeFlag": "Y", "activeFlagName": "启用", "issueType": "push", "issueTypeName": "推式发料",
        "providerName": "欣兴电子", "supplierName": "欣兴电子", "series": "BGA", "seriesName": "BGA 系列",
        "delFlag": "0", "remarks": "", "createBy": "admin", "createByStr": "管理员",
        "createDateStr": "2026-08-06 09:00:00", "updateBy": "admin", "updateByStr": "管理员",
        "updateDateStr": "2026-08-06 09:00:00",
    },
    {
        "id": "mtl-1002", "materialCode": "WIRE-AU-1.0", "materialAllCode": "WIRE-AU-1.0",
        "materialName": "金线 1.0mil", "materialDesc": "键合金线 φ1.0mil", "revision": "A",
        "mtlKind": "物料", "mtlKindName": "物料", "materialNature": "主要材料",
        "materialNatureName": "主要材料", "mtlcategoryCode": "C-WIRE", "mtlcategoryName": "键合线",
        "mtltypeId": "mtype-wire", "mtltypeName": "金线", "mtlType": "金线",
        "mtlUom": "uom-m", "mtlUomname": "米", "safetyStock": 50000, "stdNum": 0,
        "activeFlag": "Y", "activeFlagName": "启用", "issueType": "push", "issueTypeName": "推式发料",
        "providerName": "贺利氏", "supplierName": "贺利氏", "series": "键合线", "seriesName": "键合线",
        "delFlag": "0", "remarks": "", "createBy": "admin", "createByStr": "管理员",
        "createDateStr": "2026-08-06 09:00:00", "updateBy": "admin", "updateByStr": "管理员",
        "updateDateStr": "2026-08-06 09:00:00",
    },
    {
        "id": "mtl-1003", "materialCode": "MC-EMC-01", "materialAllCode": "MC-EMC-01",
        "materialName": "环氧塑封料 EMC", "materialDesc": "环氧塑封料", "revision": "B",
        "mtlKind": "物料", "mtlKindName": "物料", "materialNature": "辅助材料",
        "materialNatureName": "辅助材料", "mtlcategoryCode": "C-MC", "mtlcategoryName": "塑封料",
        "mtltypeId": "mtype-mc", "mtltypeName": "塑封料", "mtlType": "塑封料",
        "mtlUom": "uom-kg", "mtlUomname": "千克", "safetyStock": 200, "stdNum": 0,
        "activeFlag": "Y", "activeFlagName": "启用", "issueType": "pull", "issueTypeName": "拉式发料",
        "providerName": "住友电木", "supplierName": "住友电木", "series": "塑封料", "seriesName": "塑封料",
        "delFlag": "0", "remarks": "", "createBy": "admin", "createByStr": "管理员",
        "createDateStr": "2026-08-07 10:00:00", "updateBy": "admin", "updateByStr": "管理员",
        "updateDateStr": "2026-08-07 10:00:00",
    },
]

# 物料 init：下拉枚举（McMtlInitResponse）
MATERIAL_INIT = {
    "mtlKindList": [
        {"dictCode": "物料", "dictName": "物料"},
        {"dictCode": "tooling", "dictName": "tooling"},
        {"dictCode": "半成品", "dictName": "半成品"},
    ],
    "materialNatureList": [
        {"dictCode": "主要材料", "dictName": "主要材料"},
        {"dictCode": "辅助材料", "dictName": "辅助材料"},
    ],
    "issueTypeList": [
        {"dictCode": "push", "dictName": "推式发料"},
        {"dictCode": "pull", "dictName": "拉式发料"},
    ],
    "expireUodList": [
        {"dictCode": "day", "dictName": "天"},
        {"dictCode": "month", "dictName": "月"},
    ],
    "activeFlagList": [
        {"dictCode": "Y", "dictName": "启用"},
        {"dictCode": "N", "dictName": "停用"},
    ],
}

# 批次：MpLotEntity 关键字段（mes，/lot 前缀）
LOTS = [
    {
        "id": "lot-0001", "lotNo": "LOT-BG-2609001", "lotType": "BG", "lotTypeName": "磨划",
        "woId": "wo-0001", "woCode": "WO20260911001", "orderNo": "PO202609001",
        "productId": "bp-0001", "productCode": "P-8G-DDR5", "productName": "8Gb DDR5 内存芯片",
        "productDesc": "FBGA-78", "productVer": "1.0",
        "customerId": "cust-01", "customerCode": "C-HW", "customerName": "华为海思",
        "planDieQty": 8000, "planWaferQty": 1000, "waferQty": 600, "preDieQty": 4800,
        "dieQty": 4800, "totalQty": 4800, "useQty": 1200, "defectQty": 60,
        "status": "生产中", "statusName": "生产中", "primaryDieFlag": "Y",
        "waferId": "wafer-001", "parentLotId": None, "parentLotNo": None,
        "priorityLevel": "紧急", "priorityLevelName": "紧急",
        "beginTime": "2026-09-10 08:00:00", "lotFinishTime": "2026-09-20 18:00:00",
        "orderFinishTime": "2026-09-20 18:00:00", "factoryId": "factory-01",
        "stepId": "step-01", "stepCode": "BG", "stepName": "磨划",
        "planId": "plan-001", "planCode": "ROUTE-DDR5", "planName": "DDR5 封测工艺",
        "delFlag": "0", "remarks": "", "createBy": "admin",
        "createDate": "2026-09-10 08:00:00", "updateBy": "admin",
        "updateDate": "2026-09-11 10:00:00",
    },
    {
        "id": "lot-0002", "lotNo": "LOT-ASSY-2609001", "lotType": "ASSY", "lotTypeName": "封装",
        "woId": "wo-0001", "woCode": "WO20260911001", "orderNo": "PO202609001",
        "productId": "bp-0001", "productCode": "P-8G-DDR5", "productName": "8Gb DDR5 内存芯片",
        "productDesc": "FBGA-78", "productVer": "1.0",
        "customerId": "cust-01", "customerCode": "C-HW", "customerName": "华为海思",
        "planDieQty": 4800, "planWaferQty": 0, "waferQty": 0, "preDieQty": 4800,
        "dieQty": 0, "totalQty": 0, "useQty": 0, "defectQty": 0,
        "status": "已开立", "statusName": "已开立", "primaryDieFlag": "Y",
        "waferId": "wafer-001", "parentLotId": "lot-0001", "parentLotNo": "LOT-BG-2609001",
        "priorityLevel": "紧急", "priorityLevelName": "紧急",
        "beginTime": "2026-09-12 08:00:00", "lotFinishTime": "2026-09-20 18:00:00",
        "orderFinishTime": "2026-09-20 18:00:00", "factoryId": "factory-01",
        "stepId": "step-02", "stepCode": "ASSY", "stepName": "封装",
        "planId": "plan-001", "planCode": "ROUTE-DDR5", "planName": "DDR5 封测工艺",
        "delFlag": "0", "remarks": "", "createBy": "admin",
        "createDate": "2026-09-11 09:00:00", "updateBy": "admin",
        "updateDate": "2026-09-11 09:00:00",
    },
    {
        "id": "lot-0003", "lotNo": "LOT-FT-2609001", "lotType": "FT", "lotTypeName": "FT测试",
        "woId": "wo-0003", "woCode": "WO20260910003", "orderNo": "PO202609003",
        "productId": "bp-0003", "productCode": "P-PM-PMIC", "productName": "电源管理芯片 PMIC",
        "productDesc": "WLCSP-20", "productVer": "1.3",
        "customerId": "cust-03", "customerCode": "C-WT", "customerName": "闻泰科技",
        "planDieQty": 30000, "planWaferQty": 0, "waferQty": 0, "preDieQty": 29100,
        "dieQty": 29100, "totalQty": 29100, "useQty": 29000, "defectQty": 100,
        "status": "已完工", "statusName": "已完工", "primaryDieFlag": "N",
        "waferId": "wafer-003", "parentLotId": None, "parentLotNo": None,
        "priorityLevel": "加急", "priorityLevelName": "加急",
        "beginTime": "2026-09-01 08:00:00", "lotFinishTime": "2026-09-10 18:00:00",
        "orderFinishTime": "2026-09-10 18:00:00", "factoryId": "factory-01",
        "stepId": "step-03", "stepCode": "FT", "stepName": "FT测试",
        "planId": "plan-003", "planCode": "ROUTE-PMIC", "planName": "PMIC 封测工艺",
        "delFlag": "0", "remarks": "", "createBy": "admin",
        "createDate": "2026-09-01 08:00:00", "updateBy": "admin",
        "updateDate": "2026-09-10 18:00:00",
    },
    {
        "id": "lot-0004", "lotNo": "LOT-BG-2609002", "lotType": "BG", "lotTypeName": "磨划",
        "woId": "wo-0002", "woCode": "WO20260911002", "orderNo": "PO202609002",
        "productId": "bp-0002", "productCode": "P-MCU-32F", "productName": "32位 MCU 微控制器",
        "productDesc": "QFP-64", "productVer": "2.1",
        "customerId": "cust-02", "customerCode": "C-BYD", "customerName": "比亚迪半导体",
        "planDieQty": 20000, "planWaferQty": 2000, "waferQty": 0, "preDieQty": 0,
        "dieQty": 0, "totalQty": 0, "useQty": 0, "defectQty": 0,
        "status": "已扣留", "statusName": "已扣留", "primaryDieFlag": "Y",
        "waferId": "wafer-002", "parentLotId": None, "parentLotNo": None,
        "priorityLevel": "一般", "priorityLevelName": "一般",
        "beginTime": "2026-09-15 08:00:00", "lotFinishTime": "2026-09-28 18:00:00",
        "orderFinishTime": "2026-09-28 18:00:00", "factoryId": "factory-02",
        "stepId": "step-01", "stepCode": "BG", "stepName": "磨划",
        "planId": "plan-002", "planCode": "ROUTE-MCU", "planName": "MCU 封测工艺",
        "delFlag": "0", "remarks": "质量扣留", "createBy": "admin",
        "createDate": "2026-09-11 10:00:00", "updateBy": "admin",
        "updateDate": "2026-09-11 10:00:00",
    },
]

# 产品BOM：McProductBomEntity 关键字段（mes-basic，/mcProductBom 前缀）
BOMS = [
    {
        "id": "bom-0001", "productId": "bp-0001", "productCode": "P-8G-DDR5",
        "productName": "8Gb DDR5 内存芯片", "productDesc": "FBGA-78",
        "bomName": "8Gb DDR5 封测BOM", "bomType": "FT", "bomTypeName": "封测BOM",
        "bomVer": "V1", "unit": "颗", "status": "启用", "statusName": "启用",
        "createByName": "管理员", "updateByName": "管理员", "originBomId": None,
        "isLastCopy": "Y", "isHistory": "N",
        "delFlag": "0", "remarks": "", "createBy": "admin",
        "createDate": "2026-08-01 10:00:00", "updateBy": "admin",
        "updateDate": "2026-08-01 10:00:00",
    },
    {
        "id": "bom-0002", "productId": "bp-0002", "productCode": "P-MCU-32F",
        "productName": "32位 MCU 微控制器", "productDesc": "QFP-64",
        "bomName": "MCU 封测BOM", "bomType": "FT", "bomTypeName": "封测BOM",
        "bomVer": "V2", "unit": "颗", "status": "启用", "statusName": "启用",
        "createByName": "管理员", "updateByName": "管理员", "originBomId": None,
        "isLastCopy": "Y", "isHistory": "N",
        "delFlag": "0", "remarks": "", "createBy": "admin",
        "createDate": "2026-08-03 14:30:00", "updateBy": "admin",
        "updateDate": "2026-08-03 14:30:00",
    },
    {
        "id": "bom-0003", "productId": "bp-0003", "productCode": "P-PM-PMIC",
        "productName": "电源管理芯片 PMIC", "productDesc": "WLCSP-20",
        "bomName": "PMIC 封测BOM", "bomType": "FT", "bomTypeName": "封测BOM",
        "bomVer": "V1", "unit": "颗", "status": "停用", "statusName": "停用",
        "createByName": "管理员", "updateByName": "管理员", "originBomId": None,
        "isLastCopy": "Y", "isHistory": "N",
        "delFlag": "0", "remarks": "", "createBy": "admin",
        "createDate": "2026-08-05 09:00:00", "updateBy": "admin",
        "updateDate": "2026-08-05 09:00:00",
    },
]

# 设备：McEqpInfoEntity 关键字段（mes-basic，/eqp 前缀）
EQUIPMENTS = [
    {
        "id": "eqp-0001", "eqpCode": "EQP-BG-001", "eqpName": "晶圆减薄机",
        "eqpModel": "DGD8761", "eqpDesc": "磨划减薄", "revstate": "Y", "revstateName": "启用",
        "eqptypeCode": "ET-BG", "eqptypeName": "减薄机", "eqpKind": "磨划", "eqpKindName": "磨划",
        "subFlag": "N", "companyName": "封测集团", "factoryName": "封测一厂",
        "prodorgName": "磨划车间", "workshopName": "磨划车间", "locationCode": "A-01",
        "theoreticalCap": 2000, "targetCap": 1800, "stageType": "BG", "stageTypeName": "磨划",
        "nature": "生产设备", "natureName": "生产设备",
        "delFlag": "0", "remarks": "", "createBy": "admin",
        "createDate": "2026-07-01 08:00:00", "createDateStr": "2026-07-01 08:00:00",
        "updateBy": "admin", "updateDate": "2026-07-01 08:00:00",
        "updateDateStr": "2026-07-01 08:00:00",
    },
    {
        "id": "eqp-0002", "eqpCode": "EQP-WB-001", "eqpName": "键合机",
        "eqpModel": "IConn", "eqpDesc": "金线键合", "revstate": "Y", "revstateName": "启用",
        "eqptypeCode": "ET-WB", "eqptypeName": "键合机", "eqpKind": "封装", "eqpKindName": "封装",
        "subFlag": "N", "companyName": "封测集团", "factoryName": "封测一厂",
        "prodorgName": "封装车间", "workshopName": "封装车间", "locationCode": "B-03",
        "theoreticalCap": 1500, "targetCap": 1400, "stageType": "ASSY", "stageTypeName": "封装",
        "nature": "生产设备", "natureName": "生产设备",
        "delFlag": "0", "remarks": "", "createBy": "admin",
        "createDate": "2026-07-02 08:00:00", "createDateStr": "2026-07-02 08:00:00",
        "updateBy": "admin", "updateDate": "2026-07-02 08:00:00",
        "updateDateStr": "2026-07-02 08:00:00",
    },
    {
        "id": "eqp-0003", "eqpCode": "EQP-FT-001", "eqpName": "测试机",
        "eqpModel": "T5503", "eqpDesc": "FT 测试", "revstate": "Y", "revstateName": "启用",
        "eqptypeCode": "ET-FT", "eqptypeName": "测试机", "eqpKind": "FT", "eqpKindName": "FT测试",
        "subFlag": "N", "companyName": "封测集团", "factoryName": "封测二厂",
        "prodorgName": "测试车间", "workshopName": "测试车间", "locationCode": "C-05",
        "theoreticalCap": 3000, "targetCap": 2800, "stageType": "FT", "stageTypeName": "FT测试",
        "nature": "生产设备", "natureName": "生产设备",
        "delFlag": "0", "remarks": "", "createBy": "admin",
        "createDate": "2026-07-03 08:00:00", "createDateStr": "2026-07-03 08:00:00",
        "updateBy": "admin", "updateDate": "2026-07-03 08:00:00",
        "updateDateStr": "2026-07-03 08:00:00",
    },
    {
        "id": "eqp-0004", "eqpCode": "EQP-ASSY-001", "eqpName": "贴片机",
        "eqpModel": "ASM-X", "eqpDesc": "装片", "revstate": "N", "revstateName": "停用",
        "eqptypeCode": "ET-ASSY", "eqptypeName": "贴片机", "eqpKind": "封装", "eqpKindName": "封装",
        "subFlag": "N", "companyName": "封测集团", "factoryName": "封测一厂",
        "prodorgName": "封装车间", "workshopName": "封装车间", "locationCode": "B-01",
        "theoreticalCap": 1200, "targetCap": 1100, "stageType": "ASSY", "stageTypeName": "封装",
        "nature": "生产设备", "natureName": "生产设备",
        "delFlag": "0", "remarks": "保养中", "createBy": "admin",
        "createDate": "2026-07-04 08:00:00", "createDateStr": "2026-07-04 08:00:00",
        "updateBy": "admin", "updateDate": "2026-07-04 08:00:00",
        "updateDateStr": "2026-07-04 08:00:00",
    },
]

# 工序/站点：/step/page 返回 McStepDTO 的字段（mes-basic，/step 前缀）
STEPS = [
    {
        "id": "step-bg", "stepCode": "BG", "stepName": "磨划/减薄",
        "sector": "CP", "sectorName": "CP测试", "status": "Y", "statusName": "启用",
        "productTypeName": "封测", "stageType": "BG", "stageTypeName": "磨划",
        "remarks": "", "createBy": "admin", "updateBy": "admin",
        "createDateStr": "2026-07-01 08:00:00", "updateDateStr": "2026-07-01 08:00:00",
    },
    {
        "id": "step-diebond", "stepCode": "DIEBOND", "stepName": "装片",
        "sector": "封装", "sectorName": "封装", "status": "Y", "statusName": "启用",
        "productTypeName": "封测", "stageType": "ASSY", "stageTypeName": "封装",
        "remarks": "", "createBy": "admin", "updateBy": "admin",
        "createDateStr": "2026-07-01 08:00:00", "updateDateStr": "2026-07-01 08:00:00",
    },
    {
        "id": "step-wirebond", "stepCode": "WIREBOND", "stepName": "键合",
        "sector": "封装", "sectorName": "封装", "status": "Y", "statusName": "启用",
        "productTypeName": "封测", "stageType": "ASSY", "stageTypeName": "封装",
        "remarks": "", "createBy": "admin", "updateBy": "admin",
        "createDateStr": "2026-07-01 08:00:00", "updateDateStr": "2026-07-01 08:00:00",
    },
    {
        "id": "step-mold", "stepCode": "MOLD", "stepName": "塑封",
        "sector": "封装", "sectorName": "封装", "status": "Y", "statusName": "启用",
        "productTypeName": "封测", "stageType": "ASSY", "stageTypeName": "封装",
        "remarks": "", "createBy": "admin", "updateBy": "admin",
        "createDateStr": "2026-07-01 08:00:00", "updateDateStr": "2026-07-01 08:00:00",
    },
    {
        "id": "step-ft", "stepCode": "FT", "stepName": "FT测试",
        "sector": "FT", "sectorName": "FT测试", "status": "Y", "statusName": "启用",
        "productTypeName": "封测", "stageType": "FT", "stageTypeName": "FT测试",
        "remarks": "", "createBy": "admin", "updateBy": "admin",
        "createDateStr": "2026-07-01 08:00:00", "updateDateStr": "2026-07-01 08:00:00",
    },
]

# ---------------------------------------------------------------------------
# 响应构造
# ---------------------------------------------------------------------------

def _entity(data, msg="操作成功", code=200):
    return {"success": True, "code": code, "msg": msg, "data": data}


def _list(rows, count, page_no, page_size, msg="查询成功", code=200):
    page_count = (count + page_size - 1) // page_size if page_size else 0
    return {
        "success": True, "code": code, "msg": msg,
        "rows": rows, "count": count,
        "pageNo": page_no, "pageSize": page_size, "pageCount": page_count,
    }


def _error(msg="参数错误", code=400):
    return {"success": False, "code": code, "msg": msg}


def _i(value):
    """安全整数转换，失败返回 0。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _contains(text, keyword):
    """不区分大小写的包含匹配；keyword 为空视为不限制。"""
    if keyword in (None, ""):
        return True
    return str(keyword).lower() in str(text or "").lower()


def _eq(value, expect):
    """相等匹配；expect 为空视为不限制。"""
    if expect in (None, ""):
        return True
    return str(value) == str(expect)


# ---------------------------------------------------------------------------
# 端点处理（纯逻辑，便于独立单测）
# ---------------------------------------------------------------------------

def query_products(args):
    page_no = max(_i(args.get("pageNo")) or 1, 1)
    page_size = max(_i(args.get("pageSize")) or 20, 1)

    rows = [p for p in PRODUCTS
            if _contains(p.get("productCode"), args.get("productCode"))
            and _contains(p.get("productName"), args.get("productName"))
            and _contains(p.get("productDesc"), args.get("productDesc"))
            and _eq(p.get("status"), args.get("status"))
            and _eq(p.get("type"), args.get("type"))]
    count = len(rows)
    start = (page_no - 1) * page_size
    return _list(rows[start:start + page_size], count, page_no, page_size)


def get_product(args):
    pid = args.get("id")
    for p in PRODUCTS:
        if p.get("id") == pid:
            return _entity(p)
    return _entity(None, msg="查询成功")


def query_work_orders(args):
    page_no = max(_i(args.get("pageNo")) or 1, 1)
    page_size = max(_i(args.get("pageSize")) or 20, 1)

    rows = [w for w in WORK_ORDERS
            if _contains(w.get("woCode"), args.get("woCode"))
            and _contains(w.get("productCode"), args.get("productCode"))
            and _contains(w.get("productName"), args.get("productName"))
            and _eq(w.get("woStatus"), args.get("woStatus"))
            and _eq(w.get("woType"), args.get("woType"))
            and _contains(w.get("orderNo"), args.get("orderNo"))
            and _contains(w.get("itemNo"), args.get("itemNo"))]
    count = len(rows)
    start = (page_no - 1) * page_size
    return _list(rows[start:start + page_size], count, page_no, page_size)


def get_work_order(args):
    wid = args.get("id")
    for w in WORK_ORDERS:
        if w.get("id") == wid:
            return _entity(w)
    return _entity(None, msg="查询成功")


def work_order_init(_args):
    return _entity(WORK_ORDER_INIT)


def query_materials(args):
    page_no = max(_i(args.get("pageNo")) or 1, 1)
    page_size = max(_i(args.get("pageSize")) or 20, 1)
    rows = [m for m in MATERIALS
            if _contains(m.get("materialCode"), args.get("materialCode"))
            and _contains(m.get("materialName"), args.get("materialName"))
            and _contains(m.get("materialDesc"), args.get("materialDesc"))
            and _eq(m.get("mtlKind"), args.get("mtlKind"))
            and _eq(m.get("activeFlag"), args.get("activeFlag"))]
    count = len(rows)
    start = (page_no - 1) * page_size
    return _list(rows[start:start + page_size], count, page_no, page_size)


def get_material(args):
    mid = args.get("id")
    for m in MATERIALS:
        if m.get("id") == mid:
            return _entity(m)
    return _entity(None, msg="查询成功")


def material_init(_args):
    return _entity(MATERIAL_INIT)


def query_lots(args):
    page_no = max(_i(args.get("pageNo")) or 1, 1)
    page_size = max(_i(args.get("pageSize")) or 20, 1)
    rows = [l for l in LOTS
            if _contains(l.get("lotNo"), args.get("lotNo"))
            and _contains(l.get("woCode"), args.get("woCode"))
            and _contains(l.get("productCode"), args.get("productCode"))
            and _eq(l.get("lotType"), args.get("lotType"))
            and _eq(l.get("status"), args.get("status"))]
    count = len(rows)
    start = (page_no - 1) * page_size
    return _list(rows[start:start + page_size], count, page_no, page_size)


def get_lot(args):
    lid = args.get("id")
    for l in LOTS:
        if l.get("id") == lid:
            return _entity(l)
    return _entity(None, msg="查询成功")


def query_boms(args):
    page_no = max(_i(args.get("pageNo")) or 1, 1)
    page_size = max(_i(args.get("pageSize")) or 20, 1)
    rows = [b for b in BOMS
            if _contains(b.get("productCode"), args.get("productCode"))
            and _contains(b.get("bomName"), args.get("bomName"))
            and _eq(b.get("status"), args.get("status"))
            and _eq(b.get("bomType"), args.get("bomType"))]
    count = len(rows)
    start = (page_no - 1) * page_size
    return _list(rows[start:start + page_size], count, page_no, page_size)


def get_bom(args):
    bid = args.get("id")
    for b in BOMS:
        if b.get("id") == bid:
            return _entity(b)
    return _entity(None, msg="查询成功")


def query_equipments(args):
    page_no = max(_i(args.get("pageNo")) or 1, 1)
    page_size = max(_i(args.get("pageSize")) or 20, 1)
    rows = [e for e in EQUIPMENTS
            if _contains(e.get("eqpCode"), args.get("eqpCode"))
            and _contains(e.get("eqpName"), args.get("eqpName"))
            and _eq(e.get("revstate"), args.get("revstate"))
            and _eq(e.get("eqpKind"), args.get("eqpKind"))]
    count = len(rows)
    start = (page_no - 1) * page_size
    return _list(rows[start:start + page_size], count, page_no, page_size)


def get_equipment(args):
    eid = args.get("id")
    for e in EQUIPMENTS:
        if e.get("id") == eid:
            return _entity(e)
    return _entity(None, msg="查询成功")


def query_steps(args):
    page_no = max(_i(args.get("pageNo")) or 1, 1)
    page_size = max(_i(args.get("pageSize")) or 20, 1)
    rows = [s for s in STEPS
            if _contains(s.get("stepCode"), args.get("stepCode"))
            and _contains(s.get("stepName"), args.get("stepName"))
            and _eq(s.get("status"), args.get("status"))
            and _eq(s.get("stageType"), args.get("stageType"))]
    count = len(rows)
    start = (page_no - 1) * page_size
    return _list(rows[start:start + page_size], count, page_no, page_size)


def delete_material(args):
    """按 materialCode 内存删除物料（供删除同步端到端测试制造"源侧减少"）"""
    code = args.get("materialCode")
    if not code:
        return _entity(None, msg="缺少 materialCode")
    before = len(MATERIALS)
    MATERIALS[:] = [m for m in MATERIALS if m.get("materialCode") != code]
    return _entity({"deleted": before - len(MATERIALS)})


# ---------------------------------------------------------------------------
# HTTP 处理层
# ---------------------------------------------------------------------------

ROUTES = {
    "/product/queryList": query_products,
    "/product/queryById": get_product,
    "/work/order/listPage": query_work_orders,
    "/work/order/get": get_work_order,
    "/work/order/init": work_order_init,
    "/material/init": material_init,
    "/material/listPage": query_materials,
    "/material/get": get_material,
    "/material/delete": delete_material,
    "/lot/list": query_lots,
    "/lot/detail": get_lot,
    "/mcProductBom/pageList": query_boms,
    "/mcProductBom/queryDetail": get_bom,
    "/eqp/list": query_equipments,
    "/eqp/info": get_equipment,
    "/step/page": query_steps,
}


def parse_args(raw_path):
    """解析请求参数。

    兼容两种格式：
    1. 前端实际格式：`?<encodeURIComponent(json)>`（整个 query 是一段编码后的 JSON，无 key）
    2. 标准格式：`?pageNo=1&pageSize=20`
    """
    parsed = urlparse(raw_path)
    qs = parsed.query
    if not qs:
        return {}
    if "=" in qs:
        out = {}
        for k, vals in parse_qs(qs, keep_blank_values=True).items():
            out[k] = vals[0] if len(vals) == 1 else vals
        return out
    try:
        data = json.loads(unquote(qs))
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


class MesApiHandler(BaseHTTPRequestHandler):
    """MES REST API mock 请求处理器。"""

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        handler = ROUTES.get(path)
        if handler is None:
            self._send_json(_error("接口不存在", 404), status=404)
            return
        args = parse_args(self.path)
        try:
            payload = handler(args)
        except Exception:  # mock 内部异常不外泄，统一转失败
            self._send_json(_error("服务器内部错误", 500), status=500)
            return
        self._send_json(payload)

    def do_POST(self):
        # MES 保存类接口（save/delete/status）也一并 mock 为成功，便于表单提交联调
        path = urlparse(self.path).path
        args = parse_args(self.path)
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                body = {}
            if isinstance(body, dict):
                args.update(body)
        self._send_json(_entity(None, msg="操作成功"))

    def log_message(self, *_args):  # 静默，避免污染控制台
        pass


def serve(port=None):
    port = port or int(os.environ.get("MES_MOCK_PORT", DEFAULT_PORT))
    server = ThreadingHTTPServer(("127.0.0.1", port), MesApiHandler)
    return server


if __name__ == "__main__":
    _port = int(os.environ.get("MES_MOCK_PORT", DEFAULT_PORT))
    _server = ThreadingHTTPServer(("0.0.0.0", _port), MesApiHandler)
    print(f"MES mock API running at http://127.0.0.1:{_port}")
    print("  产品:  GET /product/queryList  /product/queryById")
    print("  工单:  GET /work/order/listPage /work/order/get /work/order/init")
    print("  物料:  GET /material/listPage  /material/get  /material/init")
    print("  批次:  GET /lot/list  /lot/detail")
    print("  BOM :  GET /mcProductBom/pageList  /mcProductBom/queryDetail")
    print("  设备:  GET /eqp/list  /eqp/info")
    print("  工序:  GET /step/page")
    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        _server.shutdown()