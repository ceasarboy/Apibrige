## Purpose

定义桥接创建的"人工批准"关卡：探针产物先进入待确认清单，由人查看、可选修改映射、批准后在单个事务内创建连接、字段映射与同步任务。这是 agent 与生产写入之间唯一的合法通路。

## ADDED Requirements

### Requirement: 待确认清单查询

系统 SHALL 提供待确认清单查询接口，默认只返回 `pending_review` 状态会话，支持按协议、时间、验证级别过滤。

#### Scenario: 默认视图只含待确认

- **WHEN** 客户端 GET `/api/v1/probes` 不带状态参数
- **THEN** 系统 MUST 只返回 `status=pending_review` 的会话
- **AND** 返回项含源/目标标识、协议、达到的验证级别、创建时间

#### Scenario: 未达 L2 不入清单

- **WHEN** 一个会话仅达到 `L1_reachable`
- **THEN** 系统 MUST NOT 使其出现在待确认清单
- **AND** 接口 SHALL 拒绝将其提交至 `pending_review` 并说明缺失条件

### Requirement: 会话详情对照展示

系统 SHALL 提供会话详情接口，返回可供人并排查看的三组内容：源样本与源 schema、映射草案、目标 schema 与比对结果（含差异明细）。

#### Scenario: 详情包含差异明细

- **WHEN** 客户端 GET `/api/v1/probes/{id}`
- **THEN** 系统 MUST 返回 `source_sample`、`source_schema`、`mapping_draft`、`target_schema`、`comparison`
- **AND** `comparison` 中每个差异 MUST 含字段路径、期望值、实际值、原因

#### Scenario: 认证凭证不外泄

- **WHEN** 返回会话详情
- **THEN** 系统 MUST 对其中任何认证配置字段执行脱敏

### Requirement: 人工批准并原子创建桥接

系统 SHALL 提供批准接口，在单个数据库事务内创建所需的连接、字段映射与同步任务，任一步失败则全部回滚。

#### Scenario: 批准创建三张记录

- **WHEN** 人批准一个 `pending_review` 会话且该会话引用了尚不存在的新连接
- **THEN** 系统 MUST 在同一事务内创建 `ApiConnection`、`FieldMapping`、`SyncTask` 各一条
- **AND** 会话状态置为 `committed` 并记录三者的 ID

#### Scenario: 批准时允许修改映射

- **WHEN** 批准请求携带修改后的映射配置
- **THEN** 系统 MUST 对修改后的映射重新执行确定性校验
- **AND** 校验不通过时拒绝批准并保持 `pending_review`

#### Scenario: 批准前强制重跑 L2

- **WHEN** 会话的 L2 结果已过期（超过配置时效）或映射已被修改
- **THEN** 系统 MUST 要求重新执行 L2 后才允许批准

#### Scenario: 事务失败全回滚

- **WHEN** 批准过程中任一创建步骤失败
- **THEN** 系统 MUST 回滚本事务内全部写入
- **AND** 会话状态 SHALL 保持 `pending_review`

### Requirement: 人工驳回

系统 SHALL 提供驳回接口，需填写原因，驳回后会话转 `rejected` 且不可再批准。

#### Scenario: 驳回保留记录

- **WHEN** 人驳回一个会话
- **THEN** 系统 MUST 记录驳回原因与操作者
- **AND** SHALL NOT 删除会话记录

#### Scenario: 已驳回不可批准

- **WHEN** 客户端尝试批准状态为 `rejected` 的会话
- **THEN** 系统 MUST 返回 409 与当前状态

### Requirement: 清单是唯一建桥通路

系统 MUST 使探针与 agent 侧不具备直接创建 `ApiConnection`/`FieldMapping`/`SyncTask` 的能力；agent 可达的最远动作是 `apibridge protocol apply`，其语义为**提交至待确认清单**。

#### Scenario: apply 不建桥

- **WHEN** agent 对一个已达 L2 的会话执行 `apibridge protocol apply`
- **THEN** 系统 MUST 将会话推进 `pending_review`
- **AND** `api_connections`、`field_mappings`、`sync_tasks` 三表 SHALL NOT 发生任何新增

#### Scenario: 批准仅在界面完成

- **WHEN** 检查 CLI 子命令集与探针接口
- **THEN** 系统 MUST NOT 提供可由 agent 或终端直接调用的批准入口
- **AND** 批准 MUST 经由管理界面的待确认清单页完成

#### Scenario: agent 无法绕过清单

- **WHEN** 尝试通过 CLI 或探针接口直接创建同步任务
- **THEN** 系统 MUST NOT 提供此类子命令或端点
- **AND** 既有 CRUD 端点 SHALL 仅对具备 admin token 的人工侧调用开放

#### Scenario: 批准动作可审计

- **WHEN** 一次批准完成
- **THEN** 系统 MUST 留下可追溯记录：操作者、时间、批准时达到的验证级别、最终采用的映射摘要

### Requirement: 前端待确认页面

系统 SHALL 提供待确认清单页面，支持列表浏览、三栏对照查看、批准（含就地编辑映射）、驳回、L3 授权。

#### Scenario: 授权 L3 需二次确认

- **WHEN** 用户在详情页点击"授权端到端验证（L3）"
- **THEN** 前端 MUST 弹出二次确认并明确提示将向目标系统写入 1 条真实数据
- **AND** 确认后才发起请求

#### Scenario: 批准按钮的启用条件

- **WHEN** 会话验证级别低于 L2
- **THEN** 前端 MUST 禁用"批准"按钮并说明原因
