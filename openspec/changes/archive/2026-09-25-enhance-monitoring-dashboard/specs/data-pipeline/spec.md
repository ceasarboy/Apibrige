## MODIFIED Requirements

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
