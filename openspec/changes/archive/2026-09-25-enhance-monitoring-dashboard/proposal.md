## Why

项目已内置完整监控栈（`docker-compose.yml` 的 Prometheus + Grafana、`configs/grafana-dashboard.json` 13 个面板、`configs/alerts.yml` 6 条告警、React 看板页），但**面板与告警大多取不到数**，属于"纸面看板"：

1. `apibridge_queue_depth`、`apibridge_active_tasks`、`apibridge_active_connections`、`apibridge_db_pool_*` 等 Gauge **只定义、从未 `.set()`** → 对应面板恒空、`QueueDepthHigh` 告警永不触发；
2. `app/metrics_collector.py` 使用自定义 `CollectorRegistry()` 且未注册 Process/Platform collector → `process_resident_memory_bytes`、`process_cpu_seconds_total` **不存在**，而 Grafana「内存使用」面板与 `CPUUsageHigh`/`MemoryUsageHigh` 告警正在引用它们；
3. `configs/prometheus.yml` 的 scrape target 是 `apibridge-server:8000`，`docker-compose.yml` 里的服务名却是 `backend` → 抓取目标解析失败；
4. `monitoring-dashboard` 与 `data-pipeline` 两个基线 spec 对"队列深度"的口径不一致（spec 要求 RabbitMQ 队列，实现是 `data_buffer` 待处理行数）。

同时 React 看板只有全局聚合，无法回答运维最需要的问题：**"哪个任务、哪个连接在失败"**。

本变更让既有监控栈**真正可用**（面板有数、告警可求值），并补齐按任务/连接维度的可定位能力。

## What Changes

### 修复：Prometheus 指标链路真实可求值

- 注册进程级 collector，使 `process_*` 指标真实存在（或改回默认 `REGISTRY` 并统一导出）；
- 为已定义但从未赋值的 Gauge 补上**真实赋值**来源与时机：`apibridge_queue_depth`、`apibridge_active_tasks`、`apibridge_active_connections`、`apibridge_db_pool_connections`、`apibridge_db_pool_size`；
- 修正 `configs/prometheus.yml` 的 scrape target，使其与 docker-compose 服务名一致（或反向统一服务名）；
- 修正/移除 `grafana-dashboard.json` 与 `alerts.yml` 中引用了不存在指标的查询与规则；
- 新增守卫测试：断言关键指标出现在 `/metrics` 输出中（防止再次退化为空面板）。

### 修正：队列深度口径对齐

- 明确 `apibridge_queue_depth` 的唯一定义：**RabbitMQ 就绪消息数**（对齐 `data-pipeline` spec）或 **`data_buffer` 待处理行数**（改 spec 承认现状）。二选一，消除 spec 与实现的语义冲突，并在 spec delta 中固化。

### 新增：React 看板的运维可定位能力

- **按任务维度**：成功/失败次数、最后运行时间、平均耗时、搬运行数；
- **按连接维度**：调用成功率、平均延迟；
- **错误 Top N**：按 `error_message` 归一聚合，可从看板跳转到日志页并带过滤条件；
- **单任务下钻**：最近 N 次执行明细（时间、状态、耗时、错误）；
- **筛选与下钻交互**：时间范围 + 任务/连接筛选器、行点击打开明细抽屉。

## 非目标

- **不做业务数据看板**（展示同步过去的业务数据本身）——若需要，另立变更。
- **不引入外部 BI 工具**（Power BI / Metabase / DataEase / Superset）——项目已内置 Grafana，时序指标归属它，不重复造轮子。
- **不切换平台库到 PostgreSQL**——`data-pipeline` 基线 spec 本就要求 PG 为真相源，当前用 SQLite 属既有偏差，作为**独立修复工作**处理，不混入本变更。
- **不部署 AlertManager**——`alerts.yml` 保持规则文件形态，由 Grafana/人工查看。
- **不改动同步传输链路语义**——本变更只动观测面，不动搬运数据的行为。

## 技术选型权衡

| 方案 | 结论 |
|---|---|
| 扩展 React 看板（自建） | ✅ **采用**：零新依赖，能下钻到 `execution_logs` 业务明细 |
| 修复并复用既有 Grafana 面板 | ✅ **采用**：时序指标栈已在 `docker-compose.yml` 内，只修不换 |
| 引入 Metabase / DataEase | ❌ 不采用：与 Grafana 职责重叠，多一套服务要维护 |
| 引入 Apache Superset | ❌ 不采用：对本项目体量偏重（额外元数据库 + Redis + Celery） |
| Power BI Report Server | ❌ 不采用：需 Power BI Premium(P SKU) 或 SQL Server EE+SA 授权 + Windows 栈，与"内网轻量离线"冲突，且不原生支持 SQLite |
| 自建时序存储 / 告警引擎 | ❌ 不采用：Prometheus 已在栈内 |

**分工原则**：`Grafana = 时序指标（Prometheus 拉取）`；`React 看板 = 业务明细（平台库聚合、可下钻）`。两者互补，不重叠。

## Capabilities

### Modified Capabilities

- `monitoring-dashboard`: 修正「队列深度」口径；新增按任务/连接维度指标、错误 Top N、单任务下钻与筛选交互。
- `data-pipeline`: 扩展「传输层指标上报」——看板/告警引用的每个指标 MUST 真实产生非空序列（含 Gauge 赋值与进程级指标注册），而非仅声明。

## Impact

### 代码影响

- **修改** `app/metrics_collector.py`：注册 process/platform collector；为 Gauge 提供赋值入口
- **修改** `app/api/metrics.py`：新增按任务、按连接、错误 Top N、单任务执行明细的聚合端点
- **修改** Gauge 刷新接线点（`app/services/scheduler.py` 或请求路径）：周期 `.set()` 队列/任务/连接池指标
- **修改** `app/main.py`：接入 Gauge 刷新（若走定时任务）
- **修改** `configs/prometheus.yml`：scrape target 主机名
- **修改** `configs/grafana-dashboard.json`、`configs/alerts.yml`：修正引用了不存在指标的查询与规则
- **修改** `frontend/src/pages/dashboard/index.tsx`：筛选器 + 任务健康度表 + 错误列表 + 下钻抽屉
- **新增** `tests/test_metrics_export.py`：指标可求值守卫测试

### 依赖变更

无新依赖（`prometheus_client` 已在 `requirements.txt`）。

### 部署影响

`docker-compose.yml` 监控栈已就绪，本变更使其真正可用；不新增容器。

### 风险

- Gauge 的赋值时机（进程内定时刷新 vs 请求时刷新）会影响多 worker 下的聚合语义——在 `design.md` 中决策。
- 修改 `alerts.yml` / `grafana-dashboard.json` 时需确认不误删尚在使用的指标引用。
