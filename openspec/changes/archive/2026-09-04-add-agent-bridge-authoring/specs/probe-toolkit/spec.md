## Purpose

定义 APIBridge 的联调探针内核：以与运行时同一条传输路径，把"两个系统之间能否可靠搬数据"分解为三级可复现状态（L1/L2/L3），并产出可被人工确认的桥接草案（连接配置 + 字段映射）。本模块模型无关，可在无 LLM 环境下独立使用。

## ADDED Requirements

### Requirement: 探针输入形态

系统 SHALL 支持两种探针输入：尚未入库的内联连接配置（agent 从裸 URL 起步时使用）与已入库连接 ID（复验既有桥接时使用）。两种形态 MUST 走同一适配器构造逻辑。

#### Scenario: 内联配置凭证不落库

- **WHEN** 探针以内联连接配置执行（携带明文凭证）
- **THEN** 系统 MUST NOT 为该配置创建 `ApiConnection` 记录
- **AND** 会话内仅保存脱敏后的配置摘要

#### Scenario: 已存连接走解密路径

- **WHEN** 探针以 `connection_id` 执行
- **THEN** 系统 MUST 经 `decrypt_auth_config` 解密后构造 adapter_config
- **AND** 结果与运行时 `route_transfer` 构造结果一致

### Requirement: 探针与运行时同源

系统 MUST 使探针的数据传输走 `ProtocolAdapterFactory` 创建的协议适配器，与 `route_transfer` 使用同一构造逻辑（同一份 adapter_config：解密后的凭据、合并后的请求头、变量解析后的端点）。探针 SHALL NOT 自行发起未经适配器封装的 HTTP 调用。

#### Scenario: 探针使用适配器工厂

- **WHEN** 探针以 `protocol=grpc` 的连接配置执行 L1
- **THEN** 系统 MUST 通过 `ProtocolAdapterFactory.create("grpc", config)` 获得适配器并调用
- **AND** 系统 SHALL NOT 退化为 REST 直连

#### Scenario: 运行时与探针结果一致

- **WHEN** 同一连接配置在探针中达到 L2，且映射未被修改
- **THEN** 运行时 `route_transfer` MUST 走与探针相同的拉取与推送代码路径
- **AND** 探针结论 SHALL 可直接用于判断运行时行为

#### Scenario: 凭据解密一致性

- **WHEN** 连接的 `auth_config` 为加密存储
- **THEN** 探针与运行时 MUST 都先解密再注入认证信息
- **AND** 系统 SHALL NOT 向外部系统发送以 `enc:` 前缀开头的凭证值

### Requirement: 三级验证 L1 端点可达

系统 SHALL 提供 L1 验证：确认端点可达且认证有效，不搬运业务数据。

#### Scenario: L1 通过

- **WHEN** 执行 `apibridge probe` 且目标返回非 401/403/407 且非连接层错误
- **THEN** 会话状态 MUST 置为 `L1_reachable`
- **AND** 记录 `latency_ms`、`status_code`、认证方式标识

#### Scenario: L1 认证失败

- **WHEN** 目标返回 401 或 403
- **THEN** 系统 MUST 记录失败原因并返回可诊断信息（不含凭证明文）
- **AND** 会话状态 SHALL 保持 `probing` 或转 `failed`

#### Scenario: L1 不搬运数据

- **WHEN** 执行 L1
- **THEN** 系统 SHALL NOT 向目标系统写入任何业务数据

### Requirement: 三级验证 L2 转换已验证

系统 SHALL 提供 L2 验证：真实拉取源数据小样本、真实执行转换与校验、与目标端契约比对，且**不写入目标系统**。

#### Scenario: L2 样本量上限

- **WHEN** 执行 L2
- **THEN** 源数据拉取量 MUST 不超过配置上限（默认 20 条）
- **AND** 超过上限时按 `batch_fetch` 分页取前 N 条

#### Scenario: L2 使用真实转换器与校验器

- **WHEN** 执行 L2
- **THEN** 系统 MUST 调用 `transform_data` 与 `validate_batch` 处理真实源数据
- **AND** SHALL NOT 使用桩实现或样本近似

#### Scenario: L2 目标契约比对

- **WHEN** 目标为数据库连接
- **THEN** 系统 MUST 用 `get_table_schema` 比对映射输出的字段与类型
- **WHEN** 目标为 API 连接
- **THEN** 系统 MUST 比对映射输出与目标响应/请求契约

#### Scenario: L2 不写入目标

- **WHEN** 执行 L2
- **THEN** 系统 SHALL NOT 向目标系统发起任何创建或更新请求

#### Scenario: API 目标无契约时不得谎称 L2 通过

- **WHEN** 目标为 API 且既无目标表结构可查，也未声明目标字段契约
- **THEN** 系统 MUST 将结果标记为 `L2_partial_no_contract`，仅说明映射输出自身的类型自洽性
- **AND** 系统 SHALL NOT 将 `L2_partial_no_contract` 视为达到 L2

#### Scenario: 无契约会话不得进入待确认清单

- **WHEN** 会话最高验证级别为 `L2_partial_no_contract`
- **THEN** 系统 MUST 拒绝其进入 `pending_review`
- **AND** 返回补齐契约所需的下一步（提供目标字段清单或改用可回读目标）

#### Scenario: L2 通过进入待确认

- **WHEN** L2 全部字段比对通过且校验无失败
- **THEN** 会话状态 MUST 转为 `transform_verified`
- **AND** 该会话 SHALL 有资格被提交至待确认清单

### Requirement: 三级验证 L3 端到端已验证

系统 SHALL 提供 L3 验证：在人工显式授权后，向目标写入 1 条真实数据、回读并使用比对引擎核验一致性。

#### Scenario: L3 需要人工授权

- **WHEN** agent 请求执行 L3 而该会话未被人在清单中授权
- **THEN** 系统 MUST 拒绝并返回 `requires_human_authorization`
- **AND** SHALL NOT 写入目标系统

#### Scenario: L3 比对使用既有引擎

- **WHEN** L3 写入成功且目标可回读
- **THEN** 系统 MUST 使用 `DataComparator` 做字段级比对（含类型宽容与浮点容差）
- **AND** 会话状态置为 `e2e_verified` 并记录差异明细

#### Scenario: L3 目标不可回读时不得谎称完整

- **WHEN** L3 写入成功但目标系统不提供回读能力
- **THEN** 系统 MUST 标记为 `L3_partial` 并说明缺口
- **AND** SHALL NOT 标记为 `e2e_verified`

### Requirement: Schema 推断

系统 SHALL 从源响应样本推断字段清单，并支持目标端结构（数据库表结构或 API 契约）并列输出。

#### Scenario: 推断输出结构

- **WHEN** 传入源响应样本
- **THEN** 系统 MUST 返回每项含 `path`、`type`、`nullable`、`is_array`、`sample_value`
- **AND** 数组元素 MUST 以 `items[].field` 形式表达路径

#### Scenario: 嵌套路径

- **WHEN** 样本含多层嵌套对象
- **THEN** 推断结果 MUST 给出完整点分路径（如 `data.order.customer.name`）

### Requirement: 映射草案的确定性校验

系统 SHALL 接受外部（agent 或人）提议的字段对应关系，并用代码而非模型判断其有效性。校验包括源字段存在性、目标字段存在性、类型转换可行性、必填覆盖。

#### Scenario: 引用不存在的源字段被拒

- **WHEN** 提议的映射中某 `source` 路径在源 schema 中不存在
- **THEN** 系统 MUST 逐项返回 `unknown_source_field` 及该字段路径
- **AND** SHALL NOT 将该条写入草案

#### Scenario: 类型转换不可行被标记

- **WHEN** 源字段为数组而目标列为标量且无数组路径声明
- **THEN** 系统 MUST 返回 `type_infeasible` 并说明冲突点

#### Scenario: 无模型时退化可用

- **WHEN** 未提供提议映射且无可用的模型端点
- **THEN** 系统 MUST 以"同名 + 类型兼容"规则自动生成草案
- **AND** L1 与 L2 功能 SHALL 不受影响

### Requirement: 探针会话与状态机

系统 MUST 将每次联调的产物与状态持久化到 PostgreSQL `probe_sessions` 表，遵循 data-pipeline 的真相源原则。

#### Scenario: 会话状态迁移受约束

- **WHEN** 会话处于 `draft`
- **THEN** 系统 SHALL 只接受迁移到 `probing` 或 `failed`
- **AND** 非法迁移 MUST 被拒绝并返回当前状态

#### Scenario: 会话可审计

- **WHEN** 会话发生状态迁移
- **THEN** 系统 MUST 记录迁移时间、触发来源（`mcp`/`cli`/`ui`）、结果摘要

#### Scenario: 会话过期

- **WHEN** 会话创建后超过有效期（默认 7 天）仍未进入 `pending_review`
- **THEN** 系统 MUST 将其标记为 `expired`

#### Scenario: 探针样本落库前脱敏

- **WHEN** 会话保存源数据样本
- **THEN** 系统 MUST 对样本中的敏感字段套用脱敏规则
- **AND** 完整明文业务数据 SHALL NOT 存入 `probe_sessions`

### Requirement: 探针调用受控

系统 SHALL 对探针的外部调用施加限流与次数上限，避免 agent 循环打爆外部系统。

#### Scenario: 会话内次数上限

- **WHEN** 单个会话对同一目标发起探针超过上限（默认 10 次）
- **THEN** 系统 MUST 拒绝并返回 `probe_budget_exhausted`

#### Scenario: 复用既有速率限流

- **WHEN** 探针推送类调用触发目标限流键
- **THEN** 系统 MUST 复用 `app/cache/rate_limiter.py` 的滑动窗口结果
- **AND** 被限流时返回可重试提示而非笼统失败
