## Purpose

定义探针与管理 API 的安全边界：谁能调用（共享 token）、能对谁调用（目标白名单与私网拒绝）、能拿回多少（响应体上限），并把已有的无鉴权转发端点收编进受控探针。

## ADDED Requirements

### Requirement: 管理 API token 鉴权

系统 MUST 对 `/api/v1/*` 全部端点要求 `Authorization: Bearer <APIBRIDGE_ADMIN_TOKEN>`，`/health` 与 `/metrics` 豁免。

#### Scenario: 缺失或错误 token 被拒

- **WHEN** 客户端调用任意 `/api/v1/` 端点且未携带或携带错误 token
- **THEN** 系统 MUST 返回 401 且不执行业务逻辑

#### Scenario: 未配置 token 时的显式选择

- **WHEN** `APIBRIDGE_ADMIN_TOKEN` 为空
- **THEN** 系统 MUST 在启动日志中以醒目级别告警"管理 API 处于无鉴权状态"
- **AND** 生产模式下 SHALL 拒绝启动（`probe-security` 默认要求非空）

#### Scenario: 豁免端点

- **WHEN** 客户端 GET `/health` 或 `/metrics`
- **THEN** 系统 MUST 无需 token 即可访问

### Requirement: 探针目标地址白名单

系统 MUST 使探针（含由探针承载的一切对外调用）的目标地址命中 `APIBRIDGE_PROBE_ALLOWED_HOSTS` 方可发起。

#### Scenario: 私网与元数据默认拒绝

- **WHEN** 探针目标解析到回环、RFC1918、链路本地或云厂商元数据地址且未显式允许
- **THEN** 系统 MUST 拒绝并返回 `target_not_allowed`
- **AND** SHALL NOT 发起任何网络连接

#### Scenario: 先解析后校验

- **WHEN** 目标以主机名给出
- **THEN** 系统 MUST 在建立连接前解析并校验全部解析结果
- **AND** 任一地址落在禁止范围即整体拒绝

#### Scenario: 重定向逐跳复校验

- **WHEN** 探针调用收到重定向响应
- **THEN** 系统 MUST 对每个跳转目标重新执行白名单与私网校验
- **AND** 不通过时中断跟随

### Requirement: 探针响应体上限

系统 MUST 限制探针单次调用可读取与返回的响应体大小（`APIBRIDGE_PROBE_MAX_BODY_BYTES`）。

#### Scenario: 超限截断而非撑爆内存

- **WHEN** 目标返回体超过上限
- **THEN** 系统 MUST 截断并标记 `body_truncated=true`
- **AND** 返回体中 MUST NOT 包含完整超限内容

#### Scenario: schema 推断在截断样本上仍可工作

- **WHEN** 样本因截断不完整
- **THEN** 推断结果 MUST 标注置信度受限，且 L2 比对 SHALL 提示需扩大上限复跑

### Requirement: 探针调用预算

系统 SHALL 对单个探针会话针对同一目标的调用次数设上限，超限后拒绝继续。

#### Scenario: 预算耗尽

- **WHEN** 单会话对同一目标发起探针超过上限
- **THEN** 系统 MUST 返回 `probe_budget_exhausted` 并要求人工介入

## ADDED Requirements

### Requirement: API 调试转发端点收编

系统 SHALL NOT 暴露无鉴权、无目标限制的通用 HTTP 转发端点。原 `/api/v1/debug/proxy` 与 `/api/v1/debug/quick-proxy` 的能力 MUST 并入受控探针接口，并具备 token 鉴权、目标白名单与响应体上限。

#### Scenario: 裸端点已移除

- **WHEN** 客户端调用 `POST /api/v1/debug/proxy` 或 `GET /api/v1/debug/quick-proxy`
- **THEN** 系统 MUST 返回 404

#### Scenario: 受控等价能力可用

- **WHEN** 已鉴权用户需要自由试一个接口
- **THEN** 系统 MUST 通过探针接口提供等价能力，且该调用同样受白名单与响应体上限约束
