## MODIFIED Requirements

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

## ADDED Requirements

### Requirement: 传输层指标上报

系统 MUST 对同步与异步链路的传输结果与耗时上报 Prometheus 指标，使 `configs/alerts.yml` 与 Grafana 面板依赖的指标真实存在。

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
