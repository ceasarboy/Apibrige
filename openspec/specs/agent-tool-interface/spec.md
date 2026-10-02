# agent-tool-interface Specification

## Purpose
定义 agent 与 APIBridge 之间的接口层。外部 agent（pi agent、Claude Code 等）通过 shell 驱动 `apibridge` CLI 完成联调循环；CLI 只做参数解析、HTTP 调用与输出格式化，不含业务逻辑。服务端零模型依赖，模型地址与密钥只存在于工程师本机的 agent 配置中。
## Requirements
### Requirement: CLI 为 agent 唯一入口

系统 SHALL 通过 `python -m app.probe.cli` 提供面向 agent 的全部联调能力，且 CLI MUST NOT 内嵌任何业务逻辑（业务逻辑只存在于 `app/probe/` 与 `app/api/probes.py`）。本期 SHALL NOT 提供 MCP server。

#### Scenario: CLI 只经 HTTP 访问服务端

- **WHEN** agent 执行任一 CLI 子命令
- **THEN** CLI MUST 通过带 admin token 的 HTTP 请求委托服务端执行
- **AND** CLI SHALL NOT 在本地直接构造适配器或绕过鉴权与目标白名单

#### Scenario: 远程可用

- **WHEN** CLI 运行在与 APIBridge 不同的主机上
- **THEN** 全部子命令 MUST 可正常工作（无本地文件、无进程内 import 依赖）

### Requirement: 机器可读输出契约

CLI 的 `--json` 输出 MUST 是带版本号的稳定契约，供 agent 解析。

#### Scenario: 契约版本标记

- **WHEN** 使用 `--json` 执行命令
- **THEN** 输出 MUST 含 `schema_version` 字段
- **AND** 已发布字段的名称与类型 SHALL NOT 在大版本号不变时被改名或删除

#### Scenario: 退出码语义可区分

- **WHEN** 命令结束
- **THEN** 退出码 MUST 区分：`0` 成功、`2` 验证未通过、`3` 被拒绝（鉴权失败、预算耗尽、状态不足）、`4` 调用错误

#### Scenario: 失败自带下一步提示

- **WHEN** 命令以验证未通过或被拒绝结束
- **THEN** 输出 MUST 含 `next_action`，指明 agent 下一步应执行的具体动作
- **AND** SHALL NOT 仅返回异常堆栈或空泛错误串

### Requirement: 子命令面与建桥边界

CLI SHALL 提供 `probe`、`infer`、`draft`、`verify`、`protocol apply`、`session show`、`review list`、`review show`。CLI MUST NOT 提供直接创建连接、字段映射或同步任务的子命令。

#### Scenario: apply 只提交清单不建桥

- **WHEN** agent 执行 `apibridge protocol apply --session <id>`
- **THEN** 系统 MUST 将该会话推进 `pending_review`
- **AND** SHALL NOT 创建 `ApiConnection`、`FieldMapping` 或 `SyncTask` 任一记录

#### Scenario: apply 的准入门槛

- **WHEN** 会话最高验证级别低于 L2，或为 `L2_partial_no_contract`
- **THEN** `protocol apply` MUST 被拒绝（退出码 3）并说明缺失条件

#### Scenario: 批准不经 CLI

- **WHEN** 尝试通过 CLI 批准一个待确认会话
- **THEN** CLI MUST NOT 提供该子命令
- **AND** `review approve` SHALL 返回"批准请在管理界面的待确认清单中完成"

#### Scenario: CLI 只读回报状态

- **WHEN** agent 执行 `review list` 或 `review show`
- **THEN** 系统 MUST 返回会话状态供 agent 向人汇报
- **AND** 返回内容中的认证字段 SHALL 已脱敏

### Requirement: L3 由人授权后 agent 才可跑

系统 MUST 使 L3（真实写入 1 条）只能在人于清单页面完成授权后，由 agent 通过 `verify --level L3` 触发。

#### Scenario: 未授权时拒绝

- **WHEN** agent 在未获人工授权的会话上执行 `verify --level L3`
- **THEN** 系统 MUST 返回 `requires_human_authorization`（退出码 3）
- **AND** SHALL NOT 向目标系统写入任何数据

### Requirement: 凭证不进入命令行参数

CLI MUST 只从环境变量或配置文件读取 admin token 与服务端地址，SHALL NOT 接受把 token 作为命令行参数传入。

#### Scenario: 拒绝 token 参数

- **WHEN** 使用者传入 `--token`
- **THEN** CLI MUST 拒绝并提示改用环境变量

#### Scenario: 说明书警示

- **WHEN** 阅读 CLI 使用说明书
- **THEN** 文档 MUST 明确说明不得把 token 写在命令行（会泄露至进程列表与 shell 历史）

### Requirement: 协议与 CLI 说明书

系统 SHALL 为每个已支持协议提供 `skills/<protocol>/SKILL.md`，并额外提供 `skills/apibridge-cli/SKILL.md`，供 agent 读取以了解子命令用法、输出契约、认证注入约定与常见失败。

#### Scenario: 说明书与实际行为一致

- **WHEN** 适配器或 CLI 的契约发生变化
- **THEN** 对应说明书 MUST 同步更新
- **AND** 说明书中的示例命令 SHALL 可被 agent 原样执行成功

#### Scenario: 消除悬空引用

- **WHEN** 检查 `app/adapters/hap.py:6` 引用的说明书路径
- **THEN** 该文件 MUST 真实存在

### Requirement: 服务端模型无关

APIBridge 服务端 MUST NOT 发起任何大模型调用，SHALL NOT 持有模型地址或模型密钥。

#### Scenario: 无模型环境下功能完整

- **WHEN** 部署环境中不存在任何模型端点
- **THEN** 探针 L1 与 L2、CLI 全部子命令、待确认清单 MUST 均可用
- **AND** 映射草案 SHALL 以"同名 + 类型兼容"规则退化生成

#### Scenario: 代码中不存在模型客户端

- **WHEN** 检查后端依赖清单与应用代码
- **THEN** MUST NOT 存在模型 SDK、模型 HTTP 客户端或模型端点配置项

