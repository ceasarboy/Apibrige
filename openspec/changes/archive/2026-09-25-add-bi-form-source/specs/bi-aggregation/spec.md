## ADDED Requirements

### Requirement: 表单数据源

系统 SHALL 支持 `form` 数据源：以「连接 + 表单」为聚合单位，经平台同源读行路径
（HAP 工作表 / DB 表）拉取真实业务数据行。

#### Scenario: 定义指向连接表单

- **WHEN** 管理员创建 `source_type=form` 的聚合定义
- **THEN** `source_ref` MUST 为含非空 `connection_id` 与 `form_key` 的 JSON
- **AND** 结构不合法时 MUST 被校验拒绝

#### Scenario: 拉取行数据走同源路径

- **WHEN** 聚合或明细查询需要表单行数据
- **THEN** HAP 连接 MUST 经 `ProtocolAdapterFactory` 构造适配器读取（与运行时同一条传输路径）
- **AND** DB 连接 MUST 经数据库连接器读取（凭证在其解密路径内流转，不进日志与响应）

#### Scenario: 行键与字段端点一致

- **WHEN** 维度或度量引用某字段
- **THEN** 字段标识 MUST 与字段端点 `path`（HAP 为 alias||controlId）一致

#### Scenario: 拉取行数有硬上限

- **WHEN** 单次拉取行数超过上限（10 万行）
- **THEN** 系统 MUST 截断并记录告警日志

### Requirement: 快照聚合语义

`form` 数据源 SHALL 采用快照语义：每次计算取当前全量行，结果代表计算时刻的表单状态。

#### Scenario: 只计算最新完整窗口

- **WHEN** 聚合 tick 执行 form 源指标
- **THEN** MUST 只计算最新一个已完整结束的窗口，并把水位线直接前跳到该窗口
- **AND** 水位线已追平时 SHALL NOT 重复拉取与计算

#### Scenario: 不支持回填

- **WHEN** 对 form 源定义执行区间回填
- **THEN** 系统 MUST 拒绝并说明快照语义不支持回填

#### Scenario: 行缓存共享

- **WHEN** 同一表单在缓存有效期内被多个指标或明细查询使用
- **THEN** 系统 MUST 只向源拉取一次行数据

### Requirement: 表单字段元数据与逻辑类型

系统 SHALL 提供表单字段元数据查询，并给出统一的 BI 逻辑类型分类。

#### Scenario: 字段元数据复用统一口径

- **WHEN** 查询某连接某表单的字段元数据
- **THEN** 返回的 `path` MUST 与字段端点一致（可直接用作维度/度量字段标识）
- **AND** 字段中文名 MUST 合并字段字典的用户命名

#### Scenario: 逻辑类型归一

- **WHEN** 返回字段元数据
- **THEN** 每个字段 MUST 附带 `bi_type`（dimension/number/time），由字段类型归一计算

### Requirement: 表单明细查询

系统 SHALL 支持明细表图表（`chart_type=detail`，仅限 form 源），直接展示表单原始行。

#### Scenario: 明细查询实时取行

- **WHEN** 查询 detail 类型指标
- **THEN** 系统 MUST 走表单行读取（与聚合共享行缓存）而非预聚合桶
- **AND** 返回行数 MUST 受上限约束（≤500）

#### Scenario: detail 仅限 form 源

- **WHEN** 非 form 源的定义声明 `chart_type=detail`
- **THEN** 校验 MUST 拒绝

### Requirement: 内置指标默认停用

平台运营类内置指标 SHALL 默认停用（撤屏），引擎保留全部数据源类型，用户可手动启用。

#### Scenario: 新环境内置指标不上屏

- **WHEN** 全新环境完成内置指标种子写入
- **THEN** 所有内置指标 MUST 为停用状态

#### Scenario: 存量环境一次性撤屏

- **WHEN** 部署方执行内置指标收敛脚本
- **THEN** 存量库中 builtin 且仍启用的指标 MUST 被停用
- **AND** 脚本 MUST 幂等（重复执行无副作用）

## MODIFIED Requirements

### Requirement: 数据源与 JSON 方言适配

系统 SHALL 支持 `form`、`data_buffer`、`execution_logs`、自定义 SQL 四类数据源，
并 MUST 正确处理 PostgreSQL 与 SQLite 的 JSON 取值差异。

#### Scenario: 两种数据库都能提取 JSON 维度

- **WHEN** 同一聚合定义分别在 PostgreSQL 与 SQLite 上运行
- **THEN** MUST 均能正确提取配置的 JSON 路径值
- **AND** SHALL NOT 因 SQL 方言差异报错

#### Scenario: 自定义 SQL 只读校验

- **WHEN** 数据源为 `custom_sql`
- **THEN** 系统 MUST 拒绝非只读语句（含写操作关键字）
