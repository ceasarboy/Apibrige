# add-bi-sql-pushdown 规格增量

## MODIFIED Requirements

### Requirement: 快照聚合语义

系统 SHALL 以「连接表单当前全量行」为输入计算最新完整窗口（快照语义）；
DB 源在可下推时 MUST 以 SQL GROUP BY 聚合代替全量拉行，语义 MUST 与内存路径一致。

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

#### Scenario: 快照窗口（内存路径）

- **WHEN** form 源指标聚合（HAP 源或不可下推的 DB 定义）
- **THEN** MUST 取表单当前全量行、只算最新一个完整窗口，忽略 start/end 时间窗

#### Scenario: DB 源聚合下推

- **WHEN** DB 源定义满足可下推判定（维度/数值度量/结构化过滤均为可下推子集）
- **THEN** 系统 MUST 将「定义级筛选 → WHERE + 维度 → GROUP BY + 度量 → 聚合函数」
  下推为 SQL 执行，**MUST NOT** 拉取全量行
- **AND** 下推结果 MUST 与内存路径逐项一致（维度值 NULL→"未知"、数值 float 化、count 计数）

#### Scenario: 下推回退

- **WHEN** 定义含不可下推能力（分位算子/计算字段/自定义分组/resolver/文本模糊过滤/
  custom_sql/HAP/标识符非 ASCII/Oracle 默认）
- **THEN** 系统 MUST 回退内存路径并保持行为与现状一致
- **AND** 下推执行出现任何异常时 MUST 回退内存路径并缓存该定义的不可下推标记

#### Scenario: 单源行数上限语义

- **WHEN** DB 源定义走聚合下推路径
- **THEN** MUST 不受 `MAX_FORM_ROWS`（10 万）约束（不下拉全量行）
- **AND** HAP 源与回退路径 MUST 维持 10 万硬上限与 5 万分档提示

#### Scenario: 并发隔离

- **WHEN** 聚合（下推或内存）与投放页查询同时进行
- **THEN** 内存聚合 MUST 在事件循环外执行（线程池）
- **AND** 在飞重查询 MUST 受信号量约束（默认 ≤4）；投放读桶查询 MUST NOT 受限

#### Scenario: 下推执行点收敛且可关闭

- **WHEN** 平台执行聚合下推或内存回退
- **THEN** 源库 SQL MUST 只经单一执行入口 `app/connectors/grouped_pushdown.py`
  构造与执行（标识符白名单 + 全值绑定 + 表达式编译，不做字符串拼接）
- **AND** 连接器能力标志 `supports_group_pushdown=False`（Oracle 默认）或 db_type
  未知时 MUST 回退内存路径
- **AND** 可下推判定与失败标记 SHALL 进程内缓存（到达 TTL 后自愈），
  避免逐窗口重复判定与重复重试

#### Scenario: 下推数值与源库 SQL 逐项一致

- **WHEN** 对同一 DB 源定义分别取"下推聚合结果"与"源库 SQL 直查结果"
- **THEN** 分组维度值集合与各度量值 MUST 逐项一致（相对误差 ≤1e-9）
- **AND** 对照 MUST 覆盖分组 / 无维度汇总 / 定义级过滤三类形态
