## Purpose

定义 APIBridge 数据管道的混合存储策略与混合传输模式，确立 Redis、PostgreSQL、RabbitMQ 三者的职责分工，以及同步/异步传输的切换规则，作为双API数据桥接的架构基础。

## ADDED Requirements

### Requirement: Redis 职责限定

系统 MUST 将 Redis 的使用限定于幂等去重、分布式锁、API限流、热缓存四项职责。Redis SHALL NOT 承担数据中转主队列职责，主队列由 RabbitMQ 承担。

#### Scenario: Redis 不作为主队列

- **WHEN** 异步传输任务接收到数据
- **THEN** 系统将数据写入 PostgreSQL data_buffer 表并投递到 RabbitMQ 队列
- **AND** 系统 SHALL NOT 将中转数据存入 Redis 队列

#### Scenario: 幂等去重

- **WHEN** 同一条数据被重复提交同步
- **THEN** 系统通过 Redis 中的数据哈希识别重复
- **AND** 跳过重复数据的处理
- **AND** 返回首次处理的结果

#### Scenario: 分布式锁防并发

- **WHEN** 同一同步任务被并发触发
- **THEN** 系统通过 Redis 分布式锁确保同一任务同一时间只执行一次
- **AND** 后续触发等待锁释放或直接返回"正在执行"

### Requirement: PostgreSQL 作为持久化真相源

系统 MUST 将 PostgreSQL 作为数据持久化与可追溯的真相源。所有中转数据 SHALL 记录在 data_buffer 表，所有操作 SHALL 记录在 execution_logs 表。

#### Scenario: 中转数据持久化

- **WHEN** 数据进入传输管道（无论同步或异步模式）
- **THEN** 系统 MUST 将原始数据写入 data_buffer 表
- **AND** 记录状态为 pending、processing、completed 或 failed 之一

#### Scenario: 操作日志可追溯

- **WHEN** 执行拉取、转换、推送任一操作
- **THEN** 系统 MUST 在 execution_logs 表记录操作类型、状态、请求/响应数据、耗时
- **AND** 日志可通过任务ID和时间范围查询

### Requirement: 同步传输模式

系统 MUST 支持同步传输模式，在单个HTTP请求生命周期内完成"拉取源数据→转换→推送目标"全链路，不经过消息队列。

#### Scenario: 同步模式成功

- **WHEN** 任务配置 sync_mode 为 sync 且数据量低于阈值
- **THEN** 系统 HTTP 请求内完成全链路处理
- **AND** 同步返回处理结果（成功/失败）
- **AND** P99 响应时间 SHALL 小于 1 秒

#### Scenario: 同步模式失败转异步

- **WHEN** 任务配置 sync_mode 为 sync 且 fallback_to_async 为 true
- **AND** 同步处理失败
- **THEN** 系统 MUST 将该数据转为异步模式重新处理
- **AND** 返回异步任务ID供后续查询

#### Scenario: 同步模式失败不转异步

- **WHEN** 任务配置 sync_mode 为 sync 且 fallback_to_async 为 false（默认）
- **AND** 同步处理失败
- **THEN** 系统 MUST 直接返回失败结果
- **AND** SHALL NOT 自动转异步

### Requirement: 异步传输模式

系统 MUST 支持异步传输模式，数据先持久化到 PostgreSQL 再投递到 RabbitMQ，由 Worker 异步消费处理。

#### Scenario: 异步模式提交流程

- **WHEN** 任务配置 sync_mode 为 async
- **THEN** 系统 MUST 将数据写入 PostgreSQL data_buffer（status=pending）
- **AND** 投递消息到 RabbitMQ 队列
- **AND** 立即返回任务ID（不等待处理完成）

#### Scenario: 异步模式 Worker 消费

- **WHEN** Worker 从 RabbitMQ 消费到消息
- **THEN** 系统 MUST 执行幂等检查（Redis）
- **AND** 执行数据转换
- **AND** 执行限流检查（Redis）后推送目标
- **AND** 更新 data_buffer 状态为 completed 或 failed

#### Scenario: 异步模式吞吐量

- **WHEN** 异步模式处理批量数据
- **THEN** 吞吐量 SHALL 不低于 500 条/秒（标准生产环境配置）

### Requirement: 传输模式自动选择

系统 MUST 支持 auto 传输模式，根据数据量自动选择同步或异步。

#### Scenario: auto 模式小数据量选同步

- **WHEN** 任务配置 sync_mode 为 auto
- **AND** 预估数据量低于阈值（默认 100 条）
- **THEN** 系统 MUST 选择同步模式处理

#### Scenario: auto 模式大数据量选异步

- **WHEN** 任务配置 sync_mode 为 auto
- **AND** 预估数据量达到或超过阈值（默认 100 条）
- **THEN** 系统 MUST 选择异步模式处理

### Requirement: 数据库连接器缓存

系统 MUST 为数据库连接器提供缓存策略以降低数据库压力。

#### Scenario: 表结构查询缓存

- **WHEN** 系统查询数据库表结构
- **THEN** 系统 MUST 先查询 Redis 缓存
- **AND** 缓存未命中时查询数据库并写入 Redis（TTL 1 小时）

#### Scenario: 增量同步批量预取

- **WHEN** 执行数据库增量同步
- **THEN** 系统 MUST 按配置的 batch_size（默认 1000）分批拉取数据
- **AND** SHALL NOT 一次性加载全量数据到内存

#### Scenario: 批量写入缓冲

- **WHEN** 向目标数据库批量写入数据
- **THEN** 系统 MUST 按缓冲阈值批量提交
- **AND** 缓冲超时（默认 5 秒）后强制提交未满批次
