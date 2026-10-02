## MODIFIED Requirements

### Requirement: 队列深度展示

系统 SHALL 展示"待处理积压"指标，并 MUST 区分两种口径：`apibridge_queue_depth` 表示 RabbitMQ 任务队列的就绪消息数（外部 MQ 不可达时 SHALL NOT 导出该序列）；`apibridge_buffer_pending` 表示平台库 `data_buffer` 中待处理（`pending`/`processing`）行数，SHALL 始终可导出。

#### Scenario: 查看队列状态

- **WHEN** Prometheus 抓取 `/metrics`
- **THEN** `apibridge_buffer_pending` MUST 出现在输出中
- **AND** 其值 MUST 等于 `data_buffer` 中 `status ∈ {pending, processing}` 的行数

#### Scenario: MQ 就绪数取自真实队列

- **WHEN** RabbitMQ 可达且任务队列存在
- **THEN** `apibridge_queue_depth` MUST 等于该队列的就绪消息数
- **AND** 该读取 SHALL NOT 消费或改动队列消息

#### Scenario: MQ 不可达时不导出假值

- **WHEN** RabbitMQ 不可达或任务队列不存在
- **THEN** 系统 SHALL NOT 导出 `apibridge_queue_depth` 序列
- **AND** SHALL NOT 以 `0` 或缓冲计数冒充该指标

## ADDED Requirements

### Requirement: 按任务维度的执行指标

系统 SHALL 提供按任务维度聚合的执行指标查询，覆盖给定时间窗口内的成功/失败次数、最后运行时间、平均耗时与搬运行数。

#### Scenario: 查询任务维度指标

- **WHEN** 客户端 GET `/api/v1/metrics/tasks?period=24h`
- **THEN** 返回每个任务一项，含 `task_id`、`task_name`、`success`、`failed`、`last_run_at`、`avg_duration_ms`、`moved_rows`
- **AND** 无执行记录的任务 `success`/`failed` 为 `0`，接口不报错

#### Scenario: 时间窗口生效

- **WHEN** 客户端分别以 `period=1h` 与 `period=7d` 查询
- **THEN** 统计结果仅覆盖各自窗口内的 `execution_logs`

### Requirement: 按连接维度的调用指标

系统 SHALL 提供按连接维度聚合的调用指标（成功率与平均延迟）。连接与执行的关联 MUST 经 `field_mappings` 推导，因为 `execution_logs` 不直接记录连接。

#### Scenario: 查询连接维度指标

- **WHEN** 客户端 GET `/api/v1/metrics/connections?period=24h`
- **THEN** 返回每条被映射引用的连接，含 `connection_id`、`connection_name`、`side`、`success_rate`、`avg_latency_ms`

#### Scenario: 同一映射两端分别统计

- **WHEN** 某映射源连接为 A、目标连接为 B，且存在执行记录
- **THEN** 结果中 A（`side=source`）与 B（`side=target`）各计一次，互不混淆

### Requirement: 错误聚合与日志跳转

系统 SHALL 聚合时间窗口内的失败原因，产出可跳转到日志页的诊断入口。

#### Scenario: 错误 Top N

- **WHEN** 客户端 GET `/api/v1/metrics/errors?period=24h&limit=10`
- **THEN** 返回按归一化错误文本分组的 Top N 项，每项含 `count`、`last_seen` 与最多 50 条原样本

#### Scenario: 跳转过滤条件可复现

- **WHEN** 用户从某个错误项点击跳转到日志页
- **THEN** 跳转携带足以复现该分组的过滤条件（状态、时间范围、任务）

### Requirement: 单任务执行下钻

系统 SHALL 提供单任务最近执行的明细查询，供看板下钻使用。

#### Scenario: 查看任务最近执行

- **WHEN** 客户端 GET `/api/v1/metrics/tasks/{task_id}/runs?limit=20`
- **THEN** 返回该任务最近 N 次执行的 `created_at`、`action`、`status`、`duration_ms`、`error_message`
- **AND** 任务不存在时返回 404

### Requirement: 看板筛选与下钻交互

系统 SHALL 在监控看板提供时间范围与任务/连接筛选器，并支持从指标行下钻到执行明细。

#### Scenario: 筛选器联动

- **WHEN** 用户切换时间范围，或选择某个任务/连接
- **THEN** 看板全部图表与表格按筛选条件刷新

#### Scenario: 行点击下钻

- **WHEN** 用户在任务健康度表点击某任务行
- **THEN** 打开抽屉展示该任务最近执行明细与错误项
