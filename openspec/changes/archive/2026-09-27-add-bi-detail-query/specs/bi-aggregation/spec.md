# add-bi-detail-query 规格增量

## MODIFIED Requirements

### Requirement: 表单明细查询

系统 SHALL 支持明细表图表（`chart_type=detail`，仅限 form 源），直接展示表单原始行，
并提供分页、排序与关键字过滤（明细大表逐行分析的基础）。

#### Scenario: 明细查询实时取行

- **WHEN** 查询 detail 类型指标
- **THEN** 系统 MUST 走表单行读取（与聚合共享行缓存）而非预聚合桶
- **AND** 返回行数 MUST 受上限约束（≤500）

#### Scenario: detail 仅限 form 源

- **WHEN** 非 form 源的定义声明 `chart_type=detail`
- **THEN** 校验 MUST 拒绝

#### Scenario: 分页下推（DB 源）

- **WHEN** 请求 `GET /bi/form-rows` 携带 `page/page_size`（DB 源）
- **THEN** 系统 MUST 以参数化分页查询而非下推全量（复用驱动层白名单标识符 +
  LIMIT/OFFSET 参数绑定），每页只取 `page_size+1` 行判断 `has_more`
- **AND** 单页 MUST 受上限约束（≤200）

#### Scenario: HAP 源分页与降级标记

- **WHEN** 表单源为 HAP，或 DB 源请求了无法安全下推的能力（关键字过滤 / 降序排序）
- **THEN** 系统 MUST 在 60s 行缓存内完成过滤/排序/切片，并在响应中标记
  `fallback`（`memory_paging` / `keyword_memory` / `sort_memory`）
- **AND** 关键字过滤 MUST 大小写不敏感地匹配任一字段值
- **AND** 前端 MUST 对降级路径给出可见提示（如"搜索为内存过滤"）

#### Scenario: 行数分档提示

- **WHEN** 单源行数超过警告线（默认 5 万）
- **THEN** 系统 MUST 记录告警并在分页响应中标记 `warn`

#### Scenario: 向后兼容

- **WHEN** 请求未携带分页参数
- **THEN** 行为 MUST 与旧版一致（全量读取并按 limit 截断，与聚合共享行缓存）

#### Scenario: 明细卡分页交互

- **WHEN** 前端明细卡渲染
- **THEN** MUST 提供搜索（防抖）、排序字段与升降序、上一页/下一页与页码/总行数展示
- **AND** 单页数据量 MUST 受控（默认 200 行，避免全量 DOM 渲染）
- **AND** 投放态自动轮播 MUST 保持既有行为（恒速上滚、到底回顶、悬停暂停）
