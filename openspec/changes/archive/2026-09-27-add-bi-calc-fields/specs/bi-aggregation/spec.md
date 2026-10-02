# add-bi-calc-fields 规格增量（已归档；基线合并结果见 specs/bi-aggregation）

## MODIFIED Requirements

### Requirement: 聚合定义配置化

系统 SHALL 以数据配置（而非代码）描述每一组聚合指标，并支持增删改与启用/停用。

#### Scenario: 定义包含必要要素

- **WHEN** 管理员创建一条聚合定义
- **THEN** MUST 指定 `metric_key`（唯一）、数据源、时间字段、时间粒度、维度、度量、图表类型
- **AND** 使用未支持的聚合算子时 MUST 被拒绝，并在错误中列出可用算子
- **AND** 度量算子 SHALL 含 `ratio`（聚合比：`numerator`/`denominator` 各含 `{field, agg}`，
  可选 `scale`）；ratio 缺少分子/分母 MUST 被拒绝

#### Scenario: 停用定义后不再计算

- **WHEN** 某条定义 `enabled=false`
- **THEN** 聚合任务 SHALL NOT 为其产生新的时间桶

### Requirement: 图表格式转换契约

系统 SHALL 提供格式转换层，把统一中间格式转换为前端图表可直接渲染的结构。

#### Scenario: 原始中间格式

- **WHEN** 请求 `format=raw`
- **THEN** MUST 返回统一中间格式（`dimensions` / `x` / `series`）

#### Scenario: 展示层修饰

- **WHEN** 定义声明 `display.percent`（占比）
- **THEN** series 结构数值 MUST 按各系列总和换算为百分比（保留 1 位）
- **AND** 定义声明 `display.cumulative`（累计）时 MUST 按 x 顺序累加；
  快照源（form）声明累计 MUST 在保存时被拒绝（每轮同一状态，累计无意义）

（其余场景与基线一致，见 specs/bi-aggregation）

## ADDED Requirements

### Requirement: 派生计算字段（行级）

系统 SHALL 支持在指标定义中声明行级派生列（computed），对被拉取的每一行求值生成新列，
供维度、度量与筛选引用。

#### Scenario: 表达式引用与求值

- **WHEN** 用户定义派生列（如 `[合格数] / [检验数] * 100`）
- **THEN** 系统 MUST 以白名单 AST 方式解析并逐行求值（禁止直接执行任意代码）
- **AND** 表达式引用 SHALL 以 `[字段名]` 书写，保存时 MUST 按表单字段元数据
  （中文显示名优先、字段键回落）规范化；引用不存在 MUST 报 400
- **AND** 可用函数白名单 SHALL 含数值（round/abs/ceil/floor/min/max/coalesce）、
  字符串（substring/upper/lower/trim/concat）与日期（date_part）三类

#### Scenario: 循环引用防护

- **WHEN** 派生列之间形成循环引用（A→B→A）
- **THEN** 定义保存 MUST 拒绝并给出环路径

#### Scenario: 行级容错

- **WHEN** 某行引用字段缺失或不可转数
- **THEN** 该行派生值 MUST 为 null（不参与计数/求和，不编造 0）

### Requirement: 维度值自定义分组

系统 SHALL 支持维度的值→组映射（groups + other）在聚合前归一。

#### Scenario: 分组归一

- **WHEN** 维度声明分组映射（如 A线/B线→一车间、C线→二车间，other=其他）
- **THEN** 聚合 MUST 按组名分组；未命中的值 MUST 归入 other（未声明 other 时保留原值）
