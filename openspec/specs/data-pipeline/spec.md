# data-pipeline Specification

## Purpose
定义 APIBridge 数据管道的混合存储策略与混合传输模式，确立 Redis、PostgreSQL、RabbitMQ 三者的职责分工，以及同步/异步传输的切换规则，作为双API数据桥接的架构基础。
## Requirements
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

系统 MUST 支持同步传输模式，在单个 HTTP 请求生命周期内完成"拉取源数据→转换→推送目标"全链路，不经过消息队列。读取任何连接凭据前 MUST 完成解密，并在成功推送后落幂等标记。

#### Scenario: 同步模式成功

- **WHEN** 任务配置 sync_mode 为 sync 且数据量低于阈值
- **THEN** 系统 MUST 在单个 HTTP 请求内完成全链路处理
- **AND** 同步返回处理结果（成功/失败）
- **AND** P99 响应时间 SHALL 小于 1 秒

#### Scenario: 凭据解密后再注入

- **WHEN** 同步链路构建源或目标的认证请求头
- **THEN** 系统 MUST 先经 `decrypt_auth_config` 解密
- **AND** 发往外部系统的凭证值 SHALL NOT 带 `enc:` 前缀

#### Scenario: 加密密钥缺失时明确失败

- **WHEN** 连接存在加密凭据但 `APIBRIDGE_ENCRYPT_KEY` 未配置
- **THEN** 系统 MUST 返回可识别的失败原因（如 `encrypt_key_missing`）
- **AND** SHALL NOT 返回未处理的 500，也 SHALL NOT 把密文当凭证发出

#### Scenario: 同步模式失败转异步

- **WHEN** 任务配置 sync_mode 为 sync 且 fallback_to_async 为 true 且同步处理失败
- **THEN** 系统 MUST 将该数据转为异步模式重新处理
- **AND** 返回异步任务 ID 供后续查询

#### Scenario: 同步模式失败不转异步

- **WHEN** sync_mode 为 sync 且 fallback_to_async 为 false（默认）且同步处理失败
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

系统 MUST 为数据库连接器提供缓存策略以降低数据库压力，且缓存策略 MUST 被真实调用。

#### Scenario: 表结构查询缓存

- **WHEN** 系统查询数据库表结构
- **THEN** 系统 MUST 先查询 Redis 缓存，未命中时查询数据库并写入 Redis（TTL 1 小时）

#### Scenario: 增量同步批量预取

- **WHEN** 执行数据拉取且数据量可能超过单批上限
- **THEN** 系统 MUST 通过 `batch_fetch` 按 `batch_size`（默认 1000）分批拉取
- **AND** SHALL NOT 一次性加载全量数据到内存
- **AND** 探针 L2 的小样本拉取 SHALL 复用同一分页实现

#### Scenario: 批量写入缓冲

- **WHEN** 向目标数据库批量写入数据
- **THEN** 系统 MUST 在单次调用内完成提交（executemany / 单次 commit）
- **AND** 系统 SHALL NOT 提供跨请求的写入缓冲组件（WriteBuffer 已移除，
  因其超时自刷新会静默丢失未满批次）

### Requirement: 传输层指标上报

系统 MUST 对同步与异步链路的传输结果与耗时上报 Prometheus 指标，使 `configs/alerts.yml` 与 Grafana 面板依赖的指标真实存在。系统 MUST 保证其声明的每个指标都具备真实赋值来源：进程级指标 MUST 注册对应 collector，Gauge MUST 有周期性赋值路径；SHALL NOT 存在"仅声明、无赋值"的指标。

#### Scenario: 同步计数与耗时

- **WHEN** 一次传输完成（成功或失败）
- **THEN** 系统 MUST 递增 `apibridge_sync_total{status,task_type}`
- **AND** MUST 观测 `apibridge_sync_duration_seconds{task_type}`

#### Scenario: 外部 API 调用指标

- **WHEN** 系统对任一外部 API 发起调用
- **THEN** 系统 MUST 递增 `apibridge_api_calls_total{target,status}`
- **AND** MUST 观测 `apibridge_api_call_duration_seconds{target}`

#### Scenario: 告警规则可求值

- **WHEN** Prometheus 抓取 `/metrics`
- **THEN** `alerts.yml` 中引用 `apibridge_sync_total` 与 `apibridge_sync_duration_seconds_bucket` 的规则 MUST 能取到非空序列

#### Scenario: 进程级指标真实存在

- **WHEN** Prometheus 抓取 `/metrics`
- **THEN** `process_resident_memory_bytes` 与 `process_cpu_seconds_total` MUST 出现在输出中
- **AND** 其值 MUST 来自真实进程采集，而非硬编码

#### Scenario: Gauge 指标有真实赋值

- **WHEN** 系统中存在活跃任务、活跃连接或数据库连接池
- **THEN** `apibridge_active_tasks`、`apibridge_active_connections`、`apibridge_db_pool_connections`、`apibridge_db_pool_size` MUST 反映真实数量
- **AND** SHALL NOT 恒为 `0` 或缺失

#### Scenario: 抓取目标可达

- **WHEN** 以 `docker-compose` 部署运行
- **THEN** `configs/prometheus.yml` 的 scrape target 主机名 MUST 与 `docker-compose.yml` 中的服务名一致

#### Scenario: 指标刷新失败不破坏抓取

- **WHEN** 平台库或消息队列在刷新指标时短暂不可达
- **THEN** `/metrics` MUST 仍返回 200 与已有指标值
- **AND** MUST NOT 因刷新失败返回 5xx

#### Scenario: 面板与告警引用的指标均存在

- **WHEN** 运行自动化守卫测试
- **THEN** `configs/grafana-dashboard.json` 与 `configs/alerts.yml` 中引用的每个 `apibridge_*` 指标名 MUST 能在 `/metrics` 输出中找到至少一条序列
- **AND** 守卫 SHALL 覆盖进程级指标（`process_*`）

### Requirement: 数据哈希写入使冲突检测生效

系统 MUST 在数据进入缓冲时写入 `data_buffer.data_hash`，使既有的哈希冲突检测具备输入。

#### Scenario: 写入哈希

- **WHEN** 一条数据被写入 `data_buffer`
- **THEN** 系统 MUST 同时写入其内容哈希（与 `compute_record_hash` 口径一致）

#### Scenario: 冲突策略真实生效

- **WHEN** 任务配置 `conflict_strategy=skip` 且存在同哈希历史记录
- **THEN** 系统 MUST 跳过该条并计入冲突数
- **AND** 跳过条数 SHALL 可在执行日志中查到

### Requirement: 更新时保留未变更的加密凭据

系统 MUST 在更新连接时，对回传值为脱敏掩码的敏感字段保留数据库中原有值。

#### Scenario: 掩码值不覆盖真实凭证

- **WHEN** PUT 连接请求中某敏感字段值为 `***`
- **THEN** 系统 MUST 保留该字段原有存储值
- **AND** SHALL NOT 把掩码字符串加密为新凭证

#### Scenario: 显式新值才覆盖

- **WHEN** PUT 请求中该字段为掩码以外的值
- **THEN** 系统 MUST 以新值加密覆盖

### Requirement: 按连接字段字典进行读写过滤

系统 SHALL 在同步传输中按连接字段字典过滤字段：源连接字段字典中 `readable=false` 的字段不参与读取与删除比对，目标连接字段字典中 `writable=false` 的字段不推送到目标。无字典（未保存过）时行为与现状一致（全字段参与）。

#### Scenario: 源字段禁读后不产生转换结果

- **WHEN** 源连接字段字典标记某映射字段 `readable=false`
- **THEN** 该字段在 transform 输出中被剔除
- **AND** 删除同步的 records_full/源键集基于过滤后的记录计算

#### Scenario: 目标字段禁写后不出现在推送载荷

- **WHEN** 目标连接字段字典标记某映射字段 `writable=false`
- **THEN** 推送载荷（REST body / DB upsert dict / HAP controls）中不包含该字段
- **AND** 删除同步键集不受目标侧过滤影响

#### Scenario: 无字段字典时行为不回归

- **WHEN** 源/目标连接均未保存过字段字典
- **THEN** 同步按映射全量字段读取/推送，与现状完全一致

### Requirement: 中间数据明细保留与清理

系统 SHALL 为 `data_buffer` 明细数据提供保留策略与清理能力，避免无限增长；聚合结果 SHALL 不受明细清理影响。

#### Scenario: 超过保留期的明细被清理

- **WHEN** 存在创建时间早于保留期的 `data_buffer` 明细
- **THEN** 清理任务 MUST 分批删除这些明细（单批行数有上限）
- **AND** MUST NOT 在单个事务中删除全部（避免长事务与锁竞争）

#### Scenario: 保留期可配置

- **WHEN** 保留天数配置项被调整
- **THEN** 清理行为 MUST 按新配置生效
- **AND** MUST NOT 需要改动代码

#### Scenario: 清理不影响聚合结果

- **WHEN** 明细数据被清理后
- **THEN** `bi_metric_buckets` 中对应区间的聚合结果 MUST 保持可查询

#### Scenario: 清理失败不影响主链路

- **WHEN** 清理任务执行失败
- **THEN** MUST NOT 影响同步传输与聚合任务
- **AND** MUST 记录可诊断的失败日志

