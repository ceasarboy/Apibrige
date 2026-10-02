# monitoring-dashboard Specification

## Purpose
提供实时监控看板，展示数据同步系统的关键运行指标（同步量、成功率、延迟、队列深度），帮助用户快速掌握系统运行状况和发现异常。
## Requirements
### Requirement: 同步量趋势展示

系统 SHALL 在监控看板中展示按时间维度的同步量趋势图，支持按小时/天/周聚合。

#### Scenario: 查看同步量趋势

- **WHEN** 用户进入监控看板页面
- **THEN** 系统展示最近24小时的同步量趋势折线图，包含成功数和失败数两条线

#### Scenario: 切换时间范围

- **WHEN** 用户选择时间范围（最近1小时/24小时/7天/30天）
- **THEN** 趋势图按选择的时间范围刷新数据

### Requirement: 成功率指标展示

系统 SHALL 展示当前周期的同步成功率（成功数/总数），并以仪表盘或数字卡片形式呈现。

#### Scenario: 查看成功率

- **WHEN** 用户查看监控看板
- **THEN** 系统展示当前24小时的同步成功率百分比，成功率低于99.5%时标红显示

### Requirement: 延迟指标展示

系统 SHALL 展示同步任务的延迟指标（P50/P95/P99），以柱状图或数字卡片形式呈现。

#### Scenario: 查看延迟分布

- **WHEN** 用户查看监控看板
- **THEN** 系统展示当前24小时的 P50/P95/P99 延迟值（毫秒），P99超过1000ms时标红显示

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

### Requirement: 任务状态分布

系统 SHALL 展示所有同步任务的状态分布（活跃/暂停/错误）以饼图形式呈现。

#### Scenario: 查看任务状态分布

- **WHEN** 用户查看监控看板
- **THEN** 系统展示所有任务的状态分布饼图，点击某状态可跳转到对应任务列表

### Requirement: 数据自动刷新

系统 SHALL 支持监控看板数据自动刷新，默认30秒刷新一次，用户可手动调整刷新间隔。

#### Scenario: 自动刷新

- **WHEN** 用户停留在监控看板页面
- **THEN** 看板数据每30秒自动刷新一次，刷新时显示加载状态

#### Scenario: 手动刷新

- **WHEN** 用户点击"刷新"按钮
- **THEN** 系统立即重新获取数据并更新看板

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

