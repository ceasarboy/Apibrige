# add-bi-hap-incremental 规格增量

## MODIFIED Requirements

### Requirement: 表单数据源

系统 SHALL 支持 `form` 数据源：以「连接 + 表单」为聚合单位，经平台同源读行路径
（HAP 工作表 / DB 表）拉取真实业务数据行。**HAP 源在表规模较大时 SHALL 走增量拉取**，
以「增量 + 定期全量对账」代替每轮整表重拉。

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

#### Scenario: HAP 增量拉取

- **WHEN** HAP 工作表行数超过启用阈值（默认 2 万行）且已有水位线
- **THEN** 系统 MUST 只拉取「最近变化」的行（服务端按 `utime` 严格大于
  `水位线 − 重叠窗口` 过滤并降序返回），按行标识**幂等合并**进快照
- **AND** 合并后的快照 MUST 与全量拉取结果逐行一致
- **AND** 增量行数超过单轮上限或服务端 `total` 异常增大时 MUST 回退全量拉取

#### Scenario: 定期全量对账

- **WHEN** 距上次全量拉取超过对账间隔（默认 600 秒）
- **THEN** 系统 MUST 执行一次全量拉取以消除被删除/漏采的行
- **AND** 系统 SHALL 在读取响应中暴露当前模式、最近增量行数、水位线与上次全量时间，
  使「删除最迟何时消失」可被外部观测

#### Scenario: 增量模式的开关与降级

- **WHEN** 环境变量关闭增量、或工作表规模低于阈值、或行缺少行标识/更新时间字段
- **THEN** 系统 MUST 走全量拉取路径（行为与本变更前一致）
