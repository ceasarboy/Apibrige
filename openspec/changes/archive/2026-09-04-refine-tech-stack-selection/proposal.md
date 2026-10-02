## Why

初步方案已确定技术栈（FastAPI + PostgreSQL + Redis + RabbitMQ + Celery），但缺少对"双API与数据库交互场景下，高速缓存与自带数据库如何分工协作"以及"同步与异步传输如何切换"的明确设计。这两项是数据桥接平台的核心架构决策，直接影响性能、可靠性和复杂度，必须在实现前定案。

## What Changes

- **明确 Redis 职责边界**：Redis 不做主队列，仅承担幂等去重、分布式锁、API限流、热缓存四项职责。主队列由 RabbitMQ 承担。
- **明确 PostgreSQL 中间数据库职责**：作为数据持久化与可追溯的真相源，`data_buffer` 表记录所有中转数据，不承担高速中转。
- **新增同步传输模式**：FastAPI 路由内直接完成"拉取→转换→推送"全链路，不经过消息队列，延迟最低。
- **新增异步传输模式**：数据先写 PostgreSQL（持久化）→ 投递 RabbitMQ → 立即返回任务ID → Worker 消费处理。
- **新增任务级传输模式配置**：每个同步任务可配置 `sync_mode: sync|async|auto`，`auto` 模式按数据量阈值自动选择。
- **新增 `fallback_to_async` 开关**：同步模式失败时是否自动转异步，默认关闭，按需开启。
- **数据库连接器缓存策略**：表结构查询缓存到 Redis（TTL 1h），批量预取（batch_size=1000），写入缓冲。

## 非目标

- 不实现 Redis 主从/集群高可用（本期单节点，后续迭代）
- 不实现跨数据中心双活部署
- 不实现 GraphQL/gRPC 协议（本期聚焦 REST + DB，gRPC/GraphQL 后续迭代）
- 不实现前端可视化配置界面（本期聚焦后端架构）
- 不实现多租户隔离（本期单租户，SaaS化后续迭代）
- 不实现自定义转换脚本引擎（本期仅支持声明式字段映射）

## Capabilities

### New Capabilities

- `data-pipeline`: 数据管道核心能力，定义混合存储策略（Redis缓存 + PostgreSQL持久化）与混合传输模式（同步/异步/auto），是 APIBridge 双API桥接的架构基础。

### Modified Capabilities

无（本项目首次建立 spec，无既有能力修改）。

## Impact

- **架构影响**：确立 Redis/RabbitMQ/PostgreSQL 三者职责分工，避免职责重叠。
- **代码影响**：需新增同步传输处理器、异步传输处理器、传输模式路由器、Redis 缓存层、数据库连接器缓存策略。
- **数据库影响**：`sync_tasks` 表新增 `sync_mode`、`fallback_to_async`、`batch_size` 字段。
- **配置影响**：任务配置需支持传输模式选择，Redis 需配置限流/锁/缓存参数。
- **性能影响**：同步模式 P99 目标 <1秒，异步模式吞吐目标 ≥500条/秒。
- **权衡**：混合模式增加约 20% 代码复杂度，换取性能与可靠性的平衡。
