## Purpose

收口 `add-agent-bridge-authoring` 归档后仍遗留的运行时缺陷：看板 SQLite 兼容、幂等键回收、池空闲回收、rowcount 语义、增量/Cron/Webhook 落地、testing 路由前缀。

## ADDED Requirements

### Requirement: 看板接口跨数据库可用

监控看板查询 SHALL 在 SQLite 与 PostgreSQL 上均可用，SHALL NOT 使用 PG 专有语法（`interval`、`percentile_cont`）而无声崩溃。

#### Scenario: SQLite 下看板可查

- **WHEN** 应用以 SQLite 为真相源运行并请求看板接口
- **THEN** 系统 MUST 返回 200 与统计数据

### Requirement: 幂等键生命周期

幂等键 MUST 在推送结果确定后写入，失败 MUST 回收，使失败重试可重新执行。

#### Scenario: 失败重试不被误判重复

- **WHEN** 首次同步推送失败后重试同一数据
- **THEN** 系统 MUST 重新执行推送而非静默跳过
