# add-bi-adhoc-analysis 规格增量

## ADDED Requirements

### Requirement: 即席分析查询

系统 SHALL 提供即席聚合查询接口 `POST /api/v1/bi/adhoc/query`：由「连接 + 表单 + 维度集 +
度量集 + 筛选」即时算出一份图表数据，**不落库**（仅"另存为卡片"才写定义）。

#### Scenario: 查询契约

- **WHEN** 管理员提交 `{connection_id, form_key, chart_type, dimensions[], measures[],
  filters[], computed[], top_n, sort, format}`
- **THEN** 系统 MUST 返回 `{engine, fallback_reason, row_count, elapsed_ms, defn, format, data}`
- **AND** `data` MUST 与 `GET /bi/metrics/{key}` 的输出同构（同一格式转换层）
- **AND** `defn` MUST 是可直接用于保存卡片定义的标准化定义

#### Scenario: 与看板同一套校验口径

- **WHEN** 即席请求含维度 >3、未知算子、按类型非法的筛选算子、ratio 结构不合法等
- **THEN** 系统 MUST 以 400 拒绝并返回与定义保存端点同结构的问题列表
- **AND** 合法即席定义 MUST 无需修改即可保存为卡片（不出现"能出图不能保存"）

#### Scenario: 引擎透出（不谎报）

- **WHEN** 即席查询取值
- **THEN** DB 源且满足可下推判定时 MUST 走聚合下推并标 `engine=pushdown`
- **AND** HAP 源 / 含 `computed` / 分位算子等不可下推情形 MUST 走内存路径并标
  `engine=memory` 与 `fallback_reason`
- **AND** 内存路径因 10 万行上限截断时 MUST 标 `truncated=true`（不得呈现为全表结果）

#### Scenario: 排序与 Top N

- **WHEN** 请求带 `sort={by, order}`（by 为度量 key 或维度名）
- **THEN** 结果 MUST 按该字段排序（稳定排序；缺值恒排末尾）
- **AND** `top_n` MUST 独立生效（截断 + 其余归并为「其他」），与排序互不覆盖
