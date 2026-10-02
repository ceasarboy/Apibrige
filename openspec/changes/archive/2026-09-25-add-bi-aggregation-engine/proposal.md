## Why

平台每次同步都会把中间数据写入 `data_buffer`（`source_data` / `transformed_data`，JSON），并记录执行元数据到 `execution_logs`。但这两份数据目前**只用于传输与排障**，没有任何分析能力：

- React 看板只有全局聚合（`metrics.py` 两次 COUNT/分位查询），无法下钻；
- Grafana 只覆盖 Prometheus 时序指标，看不到中间数据内容；
- `data_buffer` 甚至**没有清理任务**（定义了 `expires_at` 却无 purge），数据无限增长却无人消费。

业务侧要的是**平台内置 BI**：对中间数据做多维聚合，用 ECharts / DataV 渲染，指标每分钟更新，单次更新 **1 万–5 万组数据，每组由 5–10 个字段构成**。

外部 BI 均不适配本场景：Power BI 需 Premium 或 SQL Server EE+SA 授权且不原生支持 SQLite；Metabase/DataEase 与已有 Grafana 职责重叠，且**同样要求先做预聚合**。聚合逻辑必须理解本平台的数据结构与维度语义，只能自建。

## What Changes

### 新增：BI 聚合引擎（每分钟增量批聚合）

- **聚合定义**（`bi_metric_defs`）：3–10 组指标以数据配置描述——数据源、时间字段、时间粒度、维度、度量（count/sum/avg/p99）、过滤条件、图表类型；支持管理员增删改。
- **预聚合结果表**（`bi_metric_buckets`）：按 `(metric_key, bucket_start, 维度)` 唯一，存结构化列。
- **水位线表**（`bi_aggregation_watermark`）：记录每个指标的聚合进度，支持断点续跑与回填。
- **每分钟调度**：复用进程内调度器模式（`scheduler.py` 的 tick + Redis 锁），**不侵入同步链路**。
- **幂等语义**：按窗口**整体重算覆盖**（先算该桶完整值再 UPSERT），而非累加——保证重跑/回填不产生重复计数。
- **数据源适配器**：`data_buffer`（中间数据 JSON）/ `execution_logs`（执行元数据）/ 自定义 SQL。

### 新增：BI 查询 API

- `GET /api/v1/bi/metrics` —— 列出指标定义（含启用状态与维度/度量配置）
- `POST` / `PUT` / `DELETE` `/api/v1/bi/metrics[/{metric_key}]` —— 定义管理（维度与度量配置、基数校验、时间点/时长校验）
- `GET /api/v1/bi/metrics/{metric_key}?from=&to=&dimensions=&format=` —— 查询聚合结果
- `POST /api/v1/bi/aggregate/run` —— 手动触发一次聚合（回填/排障用）
- `GET /api/v1/bi/dimensions` —— 返回候选维度（人/设备/物料/计划号/客户/类别/状态/地区）及其基数等级与用法提示

### 新增：格式转换层

统一中间格式（`{dimension, series[], rows[]}`）→ 图表适配器输出：

- `echarts-line` / `echarts-bar` / `echarts-pie` / `echarts-heatmap`（`{xAxis, series}`）
- `datav-number`（大屏翻牌 `{value, unit}`）
- `table`（`{columns, rows}`）

### 新增：前端 BI 大屏

- 引入 `echarts`（按需引入，控制体积）与 DataV 大屏组件；
- 新增「BI 大屏」页：指标卡 + 图表网格 + 时间范围/维度筛选；
- 与既有 Ant Design 管理界面并存的独立路由。

### 修正：`data_buffer` 保留策略

- 新增明细保留清理任务（按 `expires_at`），避免无限膨胀；
- 聚合结果长期保留（明细可删，聚合不删）。

## 非目标

- **不引入外部 BI 工具**（Power BI / Metabase / DataEase / Superset）——自建聚合层替代。
- **不做自助式拖拽建模**（用户自由组合维度/度量生成图表）——本期是**配置驱动**的固定指标集（3–10 组），自助建模另立变更。
- **不做实时流式聚合**——按用户要求"每分钟更新"，1 分钟延迟可接受；RabbitMQ 流式方案不采用（本机/离线可能无 MQ）。
- **不改动同步传输链路**——聚合只读，不参与数据搬运。
- **不做数据权限/行级隔离**——沿用现有 admin token 单层鉴权。
- **不替换 Grafana 面板**——时序指标仍归 Grafana，BI 引擎面向中间数据内容。

## 技术选型权衡

| 方案 | 结论 |
|---|---|
| **自建聚合引擎 + ECharts/DataV** | ✅ **采用**：唯一能理解本平台数据结构的方案；可视化层轻量可控 |
| 引入 Metabase / DataEase | ❌ 不采用：与 Grafana 重叠、仍需预聚合、多一套服务 |
| Power BI Report Server | ❌ 不采用：重量授权 + Windows 栈，与内网轻量离线冲突 |
| Apache Superset | ❌ 不采用：体量过重（元数据库 + Redis + Celery） |
| 只做 Grafana 面板 | ❌ 不采用：Grafana 面向时序指标，不适合中间数据多维聚合 |
| DataV 作为大屏装饰库 | ❌ **不采用**：主流实现面向 Vue，React 无官方版本；大屏装饰（翻牌/边框）改为**自研轻量 CSS 组件** |

**聚合模式选型**（详见 `design.md` D1）：

| 模式 | 结论 |
|---|---|
| 写入时聚合（同步链路上） | ❌ 耦合传输链路，风险高 |
| **每分钟增量批聚合** | ✅ **采用**：与"每分钟更新"需求天然匹配，解耦、可回填、可重跑 |
| PG 物化视图 + REFRESH | ❌ 全量重算会随数据增长变慢；SQLite 不支持 |
| 流式聚合（MQ 消费者） | ❌ 本机/离线无 MQ；延迟收益对分钟级需求无价值 |

## Capabilities

### New Capabilities

- `bi-aggregation`: BI 聚合引擎——聚合定义、预聚合桶存储、水位线增量调度、幂等窗口重算、数据源适配与图表格式转换。

### Modified Capabilities

- `data-pipeline`: `data_buffer` 明细保留策略（此前无清理逻辑，数据无限增长）。

## Impact

### 代码影响

- **新增** `app/models/bi.py`：`BiMetricDef` / `BiMetricBucket` / `BiAggregationWatermark` 三张表
- **新增** `app/services/bi_aggregator.py`：增量窗口聚合引擎（水位线推进 + 窗口重算 + UPSERT 覆盖）
- **新增** `app/services/bi_format.py`：格式转换层（中间格式 → ECharts / DataV / table）
- **新增** `app/services/bi_sources.py`：数据源适配器（`data_buffer` / `execution_logs` / 自定义 SQL，含 PG/SQLite JSON 取值差异）
- **新增** `app/api/bi.py`：BI 查询与手动触发端点（`main.py` 注册）
- **修改** `app/services/scheduler.py` 或新增 `bi_scheduler.py`：每分钟聚合 tick（Redis 锁防多 worker）
- **修改** `app/schemas.py`：BI 响应模型
- **修改** `app/models/sync.py`：`DataBuffer` 增清理相关索引（若需要）
- **新增** `frontend/src/pages/bi/`：BI 大屏页 + 路由菜单
- **新增** `frontend/src/components/charts/`：ECharts 按需封装 + DataV 组件封装
- **修改** `frontend/package.json`：新增 `echarts`（按需引入）；**不引入 DataV**（装饰组件自研，零外部依赖）
- **新增** `tests/test_bi_aggregation.py`：聚合正确性 / 幂等 / 水位线 / 格式转换

### 依赖变更

- 后端：无新依赖（SQLAlchemy + 现有栈足够）
- 前端：新增 `echarts`；DataV 需选 React 可用实现（待评估，见 `design.md` Open Questions）

### 部署影响

- 新增 3 张表（随 `create_all` 自动创建）
- 聚合任务为进程内调度，单 worker 部署下无需额外组件

### 风险

- **大屏装饰组件自研工作量**：已确认不引入 DataV（无可靠 React 实现），装饰组件（翻牌/边框/发光）全部自研——工作量已计入 `tasks.md` 5.2，风险可控。
- **JSON 字段提取性能（本轮更正后的主要瓶颈）**：每行 5–10 个字段意味着单窗口需提取 5 万 × 8 ≈ **40 万个字段值**；当前列类型为 `JSON`（非 JSONB），逐字段取值会各自重新解析文本文本，单指标聚合可能达 2–4 秒——`design.md` D5 给出对策（PG 迁 JSONB + 聚合前一次性类型转换）。
- **维度基数爆炸**：5–10 个字段若全部作为分组维度，组合数可达十万级，单桶产出海量行——`design.md` D11 给出硬限制与截断策略。
- **保留策略与合规**：删除 `data_buffer` 明细可能影响排障追溯，保留天数需用户确认。
