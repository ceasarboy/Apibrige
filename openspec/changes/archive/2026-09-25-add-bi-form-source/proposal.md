# add-bi-form-source 提案

## Why

用户澄清（2026-09-25）：BI 大屏的用途是**展示连接的表单内容**（HAP 工作表 / DB 表的真实业务数据），
不是平台自身运营看板（同步吞吐、延迟分位等平台数据的展示已由监控看板承担）。
现有 BI 数据源仅有 `data_buffer` / `execution_logs` / `custom_sql`，无法直接以「连接 + 表单」
为数据源；内置 10 组指标全部面向平台运营数据，与定位不符。

设计参照 DataEase v3 源码研究（`docs/bi-dataease-study.md`）：
统一字段逻辑类型（deType+groupType）、查询防护三件套（超时/行数上限/TopN）、
直连+缓存二相性；**只借鉴设计，不复制代码**。

## What Changes

1. 新增 `form` 数据源：`source_ref` 存 `{"connection_id","form_key"}` JSON；
   取数经平台同源读行路径（HAP `HapV3Adapter.get_worksheet_rows` / DB `fetch_from_db`），
   行键 = 字段端点 `path`（HAP alias||controlId），与映射/字段字典同一口径。
2. 快照聚合语义：form 源每轮只计算最新一个完整窗口（行时间 = 拉取时刻），
   水位线直接前跳；不支持区间回填（快照无历史可回填）。
3. 进程内 60s TTL 行缓存：同一表单的多个指标 / 明细查询每分钟只拉一次源，并发按 key 去重；
   拉行设 10 万行硬上限（借鉴查询防护三件套）。
4. 新增 `GET /bi/form-fields`（字段元数据 + `bi_type` 归一：dimension/number/time，
   借鉴 deType+groupType）与 `GET /bi/form-rows`（原始行明细）；图表类型新增 `detail`（仅 form 源），
   查询格式新增 `bi-detail` 透传。
5. 内置 10 组指标全部默认停用（撤屏）：大屏定位回归业务数据；引擎保留全部源类型，
   旧指标可在定义页手动启用。

## Impact

- **specs**：`bi-aggregation` 新增 5 组需求 + 修改 1 组（数据源类型集合加入 form）
- **代码**：`app/services/bi_{defs,sources,aggregator,format}.py`、`app/api/bi.py`
- **前端**：定义页连接-表单-字段级联、大屏 detail 图表与「业务数据大屏」改版
- **兼容**：buffer/logs/custom_sql 三种源行为零变化；既有桶/水位线数据不受影响
