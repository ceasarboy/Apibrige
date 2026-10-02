# 监控看板增强 设计

## Context

`docker-compose.yml` 已内置 Prometheus + Grafana 与 backend 服务，`configs/` 下已有 13 面板的 `grafana-dashboard.json` 与 6 条规则的 `alerts.yml`，`app/metrics_collector.py` 也定义了 13 个指标。但**声明与赋值之间存在断裂**，实测（静态核对代码）事实如下：

| 断裂点 | 事实 | 后果 |
|---|---|---|
| 进程级指标 | `CollectorRegistry()` 为自定义 registry，未注册 `ProcessCollector` | `process_resident_memory_bytes`、`process_cpu_seconds_total` 不存在 → 内存面板空、CPU/内存告警永不触发 |
| Gauge 无赋值 | `queue_depth`、`active_tasks`、`active_connections`、`db_pool_connections`、`db_pool_size` 仅定义 | 对应面板恒空、`QueueDepthHigh` 永不触发 |
| 抓取目标 | `prometheus.yml` 写 `apibridge-server:8000`，compose 服务名为 `backend` | 抓取失败（无此主机名） |
| 队列口径 | `data-pipeline` spec 要求 RabbitMQ 消息积压，React 看板实现用 `data_buffer` 计数 | spec/实现语义冲突，同名词两个含义 |
| 可定位性 | React 看板只有全局聚合 | 无法回答"哪个任务/连接在失败" |

## Goals / Non-Goals

**Goals**

1. 让既有 Grafana 面板与 Prometheus 告警**真实取到数**（有守卫测试防退化）。
2. 消除"队列深度"的 spec/实现语义冲突，明确唯一口径。
3. React 看板补齐按任务/连接维度的可定位能力与错误下钻。

**Non-Goals**：见 `proposal.md`「非目标」。

## Decisions

### D1：指标刷新时机 —— pull 时刷新，不新增后台循环

在 `/metrics` 端点内刷新 Gauge（Prometheus 抓取周期即刷新周期），**不**新增独立的采集协程。

- **理由**：零新增组件（符合"简单优先"）；抓取频率（15s）天然就是合适的刷新节奏；不需要处理多 worker 锁语义。
- **代价**：每次抓取多做一次聚合查询。该查询是索引计数（`data_buffer.status` 有索引 `idx_data_buffer_status`），15s 一次可忽略。
- **多 worker 说明**：项目部署约束已是单 worker（`AGENTS.md` §6）。每个 worker 的 registry 独立，多 worker 下抓取会轮转到不同实例导致序列跳变——此限制写入注释，不额外加锁。
- **降级**：刷新失败（DB/Redis 不可达）时仅记录日志，**不更新** Gauge，`/metrics` 仍返回已有值，不让抓取整体失败。

### D2：队列深度口径 —— 拆为两个诚实的指标

保留 `apibridge_queue_depth` 的 **RabbitMQ 语义**（对齐 `data-pipeline` spec，不撒谎），另加一个跨环境可靠的缓冲积压指标：

| 指标 | 口径 | 不可达时 |
|---|---|---|
| `apibridge_queue_depth` | RabbitMQ 任务队列就绪消息数（passive declare 读取，不消费） | **不导出该序列**（Prometheus 显示 no data，诚实暴露 MQ 不可用） |
| `apibridge_buffer_pending`（新增） | `data_buffer` 中 status ∈ {pending, processing} 的行数 | 始终可导出（平台库） |

- **理由**：本机/离线环境无 MQ（项目经验 13），若把 `queue_depth` 定义成 `data_buffer` 计数就会与 spec 冲突；若强行查 MQ 又会在无 MQ 时给假 0。拆成两个指标，各自诚实。
- **Grafana/alerts 调整**：`QueueDepthHigh` 告警与「队列深度」面板改用 `apibridge_buffer_pending`（跨环境可用）；MQ 就绪数作为补充面板。
- **备选（未采用）**：把 spec 改成"队列深度=data_buffer 计数"。否决理由：`data-pipeline` 明确 RabbitMQ 承担主队列职责，改掉会掩盖 MQ 链路的真实积压。

### D3：进程级指标 —— 显式注册 ProcessCollector

```python
from prometheus_client import ProcessCollector, PlatformCollector
ProcessCollector(registry=registry)
PlatformCollector(registry=registry)
```

- **理由**：保留自定义 registry 的隔离性（测试可控、不污染默认 `REGISTRY`），同时让 `process_*` 指标真实存在。
- **否决**：改用默认 `REGISTRY` —— 会引入 python/platform 默认指标，且与现有 `generate_latest(registry)` 调用口径不一致，改动面更大。

### D4：抓取目标 —— 统一到 compose 服务名

`configs/prometheus.yml` 的 `targets` 由 `apibridge-server:8000` 改为与 compose 服务名一致的 `backend:8000`。

- **理由**：`docker-compose.yml` 的服务名是 `backend`（`build: .`），Prometheus 与服务同网络，用服务名即可解析。改 compose 服务名会牵动卷/依赖声明，改动面更大，故改 prometheus.yml。

### D5：按连接统计 —— 经关联查询，不新增 execution_logs 列

`execution_logs` 无 `connection_id`。按连接维度统计走关联：

```
execution_logs.task_id → sync_tasks.mapping_id → field_mappings.source_connection_id / target_connection_id
```

- **理由**：零 schema 变更、零数据回填；映射数量级小，关联开销可接受。
- **代价**：无法区分同一次执行中源侧与目标侧的耗时（`execution_logs` 未记录侧别）。本变更接受该精度损失，若将来需要精确到侧，另立变更加 `connection_id` 列。
- **否决**：新增列 + 回填 —— 存量日志无法可靠回填侧别，会造出不可信数据。

### D6：既有 Grafana/alerts 的修正原则 —— 只修引用，不重排

- 仅修正引用了**不存在指标**的查询与规则（改为 D2 确定的口径）；
- 不重排面板布局、不改标题、不新增面板（除非 D2 要求的缓冲积压面板）；
- 每条被修改的规则/面板在 tasks 中列出，便于逐条核对。

### D7：新增指标的命名与标签

沿用 `apibridge_` 前缀。聚合端点返回业务明细（非 Prometheus 指标），标签口径：

- 按任务：`task_id`、`task_name`、`success`、`failed`、`last_run_at`、`avg_duration_ms`、`moved_rows`
- 按连接：`connection_id`、`connection_name`、`side`（source/target）、`success_rate`、`avg_latency_ms`
- 错误聚合：`error_message`（归一化：去 UUID/数字/时间戳后分组）、`count`、`last_seen`
- **高基数防护**：Prometheus 侧不为 per-task/per-connection 建标签（会爆基数）；这些维度只出现在 React 端点（按需查询），不进 `/metrics`。

## Risks

| 风险 | 缓解 |
|---|---|
| pull 时刷新让 `/metrics` 变慢，拖累 Prometheus 抓取 | 用索引计数查询；设置查询超时；失败降级不更新 |
| 多 worker 下序列跳变 | 单 worker 是既定部署约束（AGENTS.md §6），注释说明 |
| 修改 alerts.yml 误删仍在用的指标引用 | D6：逐条列出改动项，改动前后对照核查 |
| `apibridge_queue_depth` 在无 MQ 环境为空，被误判为故障 | 在 `docs` / 面板描述中标注"内网无 MQ 时不出现属预期"；`apibridge_buffer_pending` 作为主用指标 |
| 错误消息归一化过度聚合，丢失可诊断性 | 归一化仅用于分组 key，`last_seen` 与原样本保留在响应中 |

## Migration

- 无数据库 schema 变更，无数据迁移。
- 部署侧：`configs/prometheus.yml` 修正后需重启 Prometheus 生效；Grafana 面板随 provisioning 自动加载。
- 存量 Grafana/alerts 文件**就地修正**，不保留旧版（git 可回溯）。

## Open Questions

1. **D2 是否采纳**（拆双指标 vs 改 spec 口径）—— 倾向采纳 D2；若倾向本质简化，可改为只保留 `data_buffer` 口径并 MODIFIED spec。
2. 错误 Top N 的归一化规则（保留多少原样本）—— 实现时按可诊断优先，取前 50 条样本。
