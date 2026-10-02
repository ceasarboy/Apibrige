# probe-security Specification

## Purpose
定义探针与管理 API 的安全边界：谁能调用（共享 token）、能对谁调用（目标白名单与私网拒绝）、能拿回多少（响应体上限），并把已有的无鉴权转发端点收编进受控探针。
## Requirements
### Requirement: API 调试转发端点收编

系统 SHALL NOT 暴露无鉴权、无目标限制的通用 HTTP 转发端点。原 `/api/v1/debug/proxy` 与 `/api/v1/debug/quick-proxy` 的能力 MUST 并入受控探针接口，并具备 token 鉴权、目标白名单与响应体上限。

#### Scenario: 裸端点已移除

- **WHEN** 客户端调用 `POST /api/v1/debug/proxy` 或 `GET /api/v1/debug/quick-proxy`
- **THEN** 系统 MUST 返回 404

#### Scenario: 受控等价能力可用

- **WHEN** 已鉴权用户需要自由试一个接口
- **THEN** 系统 MUST 通过探针接口提供等价能力，且该调用同样受白名单与响应体上限约束

