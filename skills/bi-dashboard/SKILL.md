# BI 看板说明（agent 配置剧本）

面向 agent：把一个「连接表单」的表格数据配置成可投放的 BI 大屏。
BI 定义与看板**不属于建桥保护路径**，agent 可经管理 API 直接创建，无需人工批准
（建桥三件套 ApiConnection/FieldMapping/SyncTask 的清单约束不适用于 BI）。

- 服务地址与令牌：`$SERVER`（如 `http://127.0.0.1:8000`）、`$TOKEN`（管理令牌，`X-Admin-Token` 头）
- 操作员视角说明见 `docs/bi-dashboard-guide.md`；DataEase 范式研究见 `docs/bi-dataease-study.md`

## 0. 五步总览

```
① 观察表单与字段（CLI）→ ② 建指标定义（API）→ ③ 触发聚合并核对数值（API）
→ ④ 建看板并摆放（API）→ ⑤ 交付投放地址
```

## 1. 观察：表单与字段（CLI，只读）

```bash
export APIBRIDGE_SERVER="$SERVER" APIBRIDGE_ADMIN_TOKEN="$TOKEN"
python app/probe/cli.py forms list --search quality    # 找目标表单（form_key）
python app/probe/cli.py fields --connection-id <连接ID> --table <表名或工作表ID>
```

- `forms list --search` **匹配 form_key（英文标识），中文 display_name 不参与**——
  源是明道云时 form_key 是 26 位工作表 ID，用中文名搜不到，直接 `forms list` 找
  `display_name` 对应行拿 form_key
- `fields` 返回 `path`（**运行时行键**：HAP 为 `alias || controlId`；DB 为列名）与
  `type`（DB 原始类型如 TEXT/REAL，或 HAP 中文类型名）；**指标定义里的 field 一律用 path**
- 带 `bi_type`（dimension/number/time）的字段元数据在 `GET /bi/form-fields`
  （BI 专用端点，定义页用它做类型感知）；CLI fields 不返回 bi_type

表单未登记时：请管理员在管理界面「表单注册表」跑一次拉取（agent 不可写入注册表）；
DB 表可直达（注册非必需——`source_ref` 只需 connection_id + form_key）。

## 2. 建指标定义（API）

```bash
curl -s -X POST -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  "$SERVER/api/v1/bi/metrics" -d '{
  "metric_key": "prod_daily_attain",
  "name": "今日产量达成率",
  "source_type": "form",
  "source_ref": "{\"connection_id\":\"<连接ID>\",\"form_key\":\"<表名>\"}",
  "granularity": "1min",
  "chart_type": "number",
  "dimensions": [],
  "measures": [{"key": "达成率", "agg": "avg", "field": "attain_rate", "field_type": "number"}],
  "filters": [{"field": "date", "op": "eq", "value": "2026-09-27", "field_type": "string"}],
  "style": {"unit": "%"},
  "top_n": 100,
  "enabled": true
}'
```

字段契约（易错点逐条）：

| 字段 | 契约 |
|---|---|
| `source_ref` | **JSON 字符串**（不是对象）：`"{\"connection_id\":…,\"form_key\":…}"` |
| `dimensions` / `measures` | **必传数组**（无则 `[]`；传 null 报 422） |
| `dimensions[]` | `{name 显示名, field 行键}`；**最多 3 个** |
| `measures[]` | `{key 系列名, agg, field, field_type}`；agg ∈ sum/avg/count/count_distinct/min/max/p50/p95/p99；**count 可省略 field** |
| `filters[]` | 结构化规则数组（AND 关系）：`{field, op, value, field_type}`；op ∈ eq/ne/gt/ge/lt/le/in/not_in/contains/not_contains/between/not_between/empty/not_empty；between 的 value 为 `[下界, 上界]` |
| `chart_type` | 18 种：`number | table | detail | line | bar | time_line | pie | scatter | radar | heatmap | bar3d | sankey | graph | flow | tree | funnel | gauge` |
| 数据形状校验 | scatter 恰 2 度量；关系族（sankey/graph/flow）与 tree ≥2 维度；gauge ≤1 维度；detail 仅 form 源 |
| `computed` | 行级派生列：`[{key, name, expr, field_type}]`；expr 写 `[字段中文名]` 表达式（保存时自动解析为字段键）；函数白名单 round/abs/ceil/floor/min/max/coalesce/substring/upper/lower/trim/concat/date_part；派生列可互相引用（判环），可被维度/度量/筛选引用 |
| `measures[].agg="ratio"` | 聚合比：`{key, agg:"ratio", numerator:{field,agg}, denominator:{field,agg}, scale?}`（先聚合再相除，除零 null；scale=100 得百分数） |
| `dimensions[].groups/other` | 自定义分组：`groups:[{name, values[]}]` + `other`（未命中归组；缺省保留原值） |
| `display` | 展示修饰：`{percent: true}`（系列占比 %）/ `{cumulative: true}`（累计，仅滚动源——快照源 422） |
| `style` | 自由 JSON：`unit`（单位，如 `"%"`）、KPI 卡 `bg_color`/`font_color`、仪表盘 `gauge_min`/`gauge_max` |
| `granularity` | 快照聚合窗口，`1min` 或 `1d` 皆可（form 源只算最新完整窗口，历史不含业务时间） |

**图表语义速查**（选型时看这条，别踩语义坑）：

- `number` KPI 卡（配 `unit`）；`gauge` 仪表盘（配 `gauge_min/max`）
- **类别对比用 `bar`**：x = 维度值、series = 度量（DataEase 式）；**时间推移用 `line` / `time_line`**
- `pie` 分布占比例；`table` 聚合结果表；`detail` 表单原始行明细（不聚合，投放页自动轮播滚动；
  **大表逐行分析**：`GET /bi/form-rows` 支持 `page/page_size(≤200)/keyword/sort_field/order`
  ——DB 源分页+升序下推（`has_more` 探测，不拉全量），HAP 或 keyword/desc 在 60s 缓存内处理
  并在响应标 `fallback`（keyword_memory/sort_memory/memory_paging））
- 快照源（form）的分布/关系/树/类别图**只取最新桶**（跨桶求和会翻倍，引擎已按源类型处理，无需配置）

**大表规模能力（P3 聚合下推，2026-09-29 实测）**：DB 源（MySQL/PG/SQLite 连接）的聚合
**自动下推为源库 SQL GROUP BY**——不拉全量行、不受 10 万行上限；100 万行表聚合首轮
590ms（同表裸 sqlite3 同查询 598ms，即下推链路开销近似为零）。配置侧无需开关，满足下列条件即命中：

| 命中下推 | 回退内存路径（与 HAP 源同款：10 万行闸 + 5 万分档提示） |
|---|---|
| 维度 = 普通字段（最多 3 个，列名 ASCII） | 维度含 `resolver`（跨表）/ `groups`（自定义分组）/ `normalize` |
| 度量 = `count`/`count_distinct`/`sum`/`avg`/`min`/`max`/`ratio` | 度量含 `p50/p95/p99`；定义含 `computed` 派生列；度量带 `filter`（条件聚合） |
| 筛选 = `eq/ne/gt/ge/lt/le/in/not_in/between/not_between` | 筛选含 `contains/not_contains/empty/not_empty`；区间比较用在非数值字段 |
| 数值字段标 `field_type:"number"`（`duration` 亦可） | 字段/列名非 ASCII（中文列名）；Oracle 连接（能力标志默认关） |

**HAP（明道云）大表（P4 增量拉取，2026-09-29 实测）**：行数超过阈值（默认 2 万）的
HAP 表单自动改走**增量拉取**——按 `utime` 水位线只拉变化行 + 定期（默认 600s）全量对账。
读 `GET /bi/form-rows` 的 `snapshot` 字段判断当前模式：

| 字段 | 含义 |
|---|---|
| `mode=incremental` | 本轮只拉变化行（`delta_rows` 真增量 / `boundary_rows` 同秒边界刷新） |
| `mode=full` + `reason=bootstrap` | 首轮或对账轮的全量 |
| `reason=below_threshold` | 表太小，保持全量（默认行为） |
| `reason=degrade:*` | 行缺 rowid/utime，退化全量 |
| `full_at_ago_s` | 距上次全量对账多少秒 → **删除的行最迟此时长后消失** |

增量模式下**删除不会被立即感知**（靠对账清除）：对"删除很敏感"的看板，
把阈值调高（`APIBRIDGE_HAP_INCREMENTAL_MIN_ROWS`）或直接关掉增量
（`APIBRIDGE_HAP_INCREMENTAL=0`）。HAP 源仍受 10 万行硬闸。

**给大表做看板的建议**：优先用"源表已有列 + SQL 可表达的口径"（sum/avg/count/ratio 直接下推）；
若必须用 `computed`（行级派生）或 `p95`，聚合会落回内存路径——百万行级明显变慢，
此时改在源库侧建视图/预计算列，再把视图名当 `form_key` 接入。

## 2.1 计算字段与字段转换（聚合 / 派生 / 分组 / 展示）

> 真实业务表通常不存比率列——把"算出来"的口径配置化（add-bi-calc-fields，2026-09-27 实测）。

**场景速查**（要算什么 → 用哪个配置）：

| 需求 | 配置 | 示例 |
|---|---|---|
| 行级派生列（每行算比率/拼接/取分量） | `computed` + 度量引用其 key | 合格率 = `[合格数] / [检验数] * 100` |
| **平均比率**（各行比率的平均） | `computed` + `agg:"avg"` | 平均合格率 97.44% |
| **整体比率**（总量之比） | `agg:"ratio"`（分子/分母对象） | 整体达成率 = Σ实际 ÷ Σ计划 = 97.93% |
| 维度长尾值归类 | `dimensions[].groups` + `other` | A线/B线→一车间、其余→其他 |
| 系列占比 % | `display.percent` | 各产线产量占比 34.3/30.8/34.9 |
| 累计趋势 | `display.cumulative` | **仅滚动源**（快照源 422） |

**表达式语法**：`[字段中文名]` 引用（保存时自动解析为字段键；中文名优先、字段键回落，
不存在报 400）+ `+ - * / %`、括号、数值/字符串字面量 + 白名单函数：

```
数值 round(x[,d]) abs ceil floor min(..) max(..) coalesce(..)
字符串 substring(s,start[,len]) upper lower trim concat(..)     ← substring 为 SQL 风格 1-based
日期  date_part(s,'year'|'month'|'day')                          ← 对 ISO 日期字符串取分量
```

派生列可互相引用（自动判环，循环引用报错）；单行缺值/不可转数 → 该行 null（不编造 0）。

**可跑示例**（生产测试库实测；`$CID` 为「生产运行管理-测试库」连接 ID）：

```bash
# ① 计算字段：从原始列算合格率（computed + avg，行级比率口径）
curl -s -X POST -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  "$SERVER/api/v1/bi/metrics" -d "{
  \"metric_key\": \"calc_rate\", \"name\": \"合格率(计算字段)\", \"source_type\": \"form\",
  \"source_ref\": \"{\\\"connection_id\\\":\\\"$CID\\\",\\\"form_key\\\":\\\"quality_records\\\"}\",
  \"granularity\": \"1min\", \"chart_type\": \"number\", \"dimensions\": [],
  \"computed\": [{\"key\": \"rate\", \"name\": \"合格率计算\",
                 \"expr\": \"round([passed] / [inspected] * 100, 2)\", \"field_type\": \"number\"}],
  \"measures\": [{\"key\": \"合格率\", \"agg\": \"avg\", \"field\": \"rate\", \"field_type\": \"number\"}],
  \"style\": {\"unit\": \"%\"}, \"top_n\": 100, \"enabled\": true}"
# ② 聚合比：整体达成率 = Σ实际 ÷ Σ计划 × 100（ratio 口径）
curl -s -X POST -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  "$SERVER/api/v1/bi/metrics" -d "{
  \"metric_key\": \"calc_attain\", \"name\": \"整体达成率(ratio)\", \"source_type\": \"form\",
  \"source_ref\": \"{\\\"connection_id\\\":\\\"$CID\\\",\\\"form_key\\\":\\\"production_output\\\"}\",
  \"granularity\": \"1min\", \"chart_type\": \"number\", \"dimensions\": [],
  \"measures\": [{\"key\": \"整体达成率\", \"agg\": \"ratio\",
                 \"numerator\": {\"field\": \"actual_qty\", \"agg\": \"sum\"},
                 \"denominator\": {\"field\": \"plan_qty\", \"agg\": \"sum\"}, \"scale\": 100}],
  \"style\": {\"unit\": \"%\"}, \"top_n\": 100, \"enabled\": true}"
# ③ 自定义分组：车间维度（A/B线→一车间，C线→二车间）
curl -s -X POST -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  "$SERVER/api/v1/bi/metrics" -d "{
  \"metric_key\": \"calc_workshop\", \"name\": \"各车间产量(分组)\", \"source_type\": \"form\",
  \"source_ref\": \"{\\\"connection_id\\\":\\\"$CID\\\",\\\"form_key\\\":\\\"production_output\\\"}\",
  \"granularity\": \"1min\", \"chart_type\": \"bar\",
  \"dimensions\": [{\"name\": \"车间\", \"field\": \"line\",
                   \"groups\": [{\"name\": \"一车间\", \"values\": [\"A线\", \"B线\"]},
                               {\"name\": \"二车间\", \"values\": [\"C线\"]}], \"other\": \"其他\"}],
  \"measures\": [{\"key\": \"实际产量\", \"agg\": \"sum\", \"field\": \"actual_qty\", \"field_type\": \"number\"}],
  \"top_n\": 100, \"enabled\": true}"
# ④ 展示占比：各产线产量占比（percent 修饰）
curl -s -X POST -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  "$SERVER/api/v1/bi/metrics" -d "{
  \"metric_key\": \"calc_share\", \"name\": \"各产线产量占比\", \"source_type\": \"form\",
  \"source_ref\": \"{\\\"connection_id\\\":\\\"$CID\\\",\\\"form_key\\\":\\\"production_output\\\"}\",
  \"granularity\": \"1min\", \"chart_type\": \"bar\",
  \"dimensions\": [{\"name\": \"产线\", \"field\": \"line\"}],
  \"measures\": [{\"key\": \"实际产量\", \"agg\": \"sum\", \"field\": \"actual_qty\", \"field_type\": \"number\"}],
  \"display\": {\"percent\": true}, \"top_n\": 100, \"enabled\": true}"
```

实测期望值（与 SQL 直查对照，2026-09-27）：
① `avg(round(passed/inspected*100,2))` ≈ **97.44%**（行级先 round 与"先 avg 后 round"的基准
存在 0.01 级四舍五入时序差，属正常）；② `SUM(actual)/SUM(plan)*100` = **97.93%**；
③ 一车间 **20088** / 二车间 **10760**；④ **34.3 / 30.8 / 34.9**（%）。

**错误对照**（保存时校验，别硬试）：

| 报错 | 原因 |
|---|---|
| 400 `invalid_computed_expr: 表达式引用了不存在的字段：[xx]` | `[名]` 不在表单字段列表（用 `fields` 端点核对 path/中文名） |
| 400 `invalid_computed_expr: 不支持的函数` / `未知标识符` | 用了白名单外函数；字段引用必须写 `[方括号]` |
| 400 `computed_circular_ref` | 派生列互相引用成环（报错含环路径） |
| 400 `invalid_ratio` | ratio 缺 `numerator`/`denominator` 或组件缺 field（count 可省） |
| 400 `cumulative_requires_rolling_source` | 快照源（form）配了 `display.cumulative`——累计只对滚动源成立 |
| 改筛选/表达式后数值不变 | 快照水位线已追平跳过重算——**同时删桶与水位线**再跑聚合（见 §6） |

**变更索引**：定义编辑走 `PUT /bi/metrics/<key>`（整字段覆盖，computed/display 需一并回传，
否则被清空——回读用 `GET /bi/metrics` 列表带出原值再改）。

## 3. 触发聚合并核对数值

```bash
curl -s -X POST -H "X-Admin-Token: $TOKEN" "$SERVER/api/v1/bi/aggregate/run"        # 全量
curl -s -X POST -H "X-Admin-Token: $TOKEN" "$SERVER/api/v1/bi/aggregate/run?metric=<key>"
# 查询（format 与图表类型对应；from/to 缺省读全部桶）
curl -s -H "X-Admin-Token: $TOKEN" "$SERVER/api/v1/bi/metrics/<key>?format=datav-number"
```

format 对照：number/gauge→`datav-number`；line/bar/time_line→`echarts-line`/`echarts-bar`；
pie→`echarts-pie`；table→`table`；detail→`bi-detail`；其余图表见 `FORMATS`（另有 raw）。

**自审纪律**：新建或改筛选后，必须用 SQL 直查源表核对聚合值（数值对照基准），
不要只看"图能渲染"——快照翻倍/筛选失效这类问题只有数值对照能发现。

## 3.1 探索期：即席查询与分析工作台（不落库，先试再建卡）

探索数据形态时不要先建指标定义——用**即席查询**直接试维度组合，满意了再"另存为卡片"：

```bash
# 即席聚合（不写任何表；DB 源自动下推源库 GROUP BY，不拉全量行）
curl -s -X POST -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json"   "$SERVER/api/v1/bi/adhoc/query" -d '{
  "connection_id": "<连接ID>", "form_key": "<表名>",
  "chart_type": "bar", "format": "raw",
  "dimensions": [{"name": "产线", "field": "line"}],
  "measures": [{"key": "实际产量", "agg": "sum", "field": "actual_qty", "field_type": "number"}],
  "top_n": 100, "sort": {"by": "实际产量", "order": "desc"}
}'
# → {engine:"pushdown"|"memory", fallback_reason, row_count, group_count, truncated,
#     elapsed_ms, defn, format, data}
```

- `defn` 就是**可直接 POST /bi/metrics 的定义**（校验规则与保存端点同一套）；
  `sort` 不进定义（看板卡片没有排序字段）。
- **引擎字段要读**：`engine=pushdown` 表示 DB 源走了源库 GROUP BY；
  `memory` + `fallback_reason`（HAP 源/含 computed/分位算子/中文列名…）表示走了内存快照，
  `truncated=true` 表示内存路径被 10 万行闸截断——**截断结果不得当全表结论使用**。
- 界面路径：管理页「分析工作台」按钮 → `/bi/analysis`——左字段树（拖或点进槽位）、
  顶部图表类型/Top N/排序、右侧即时出图、底部「另存为看板卡片」（可顺手摆到看板）。

**规模预期**（100 万行 SQLite 实测）：下推路径接口耗时 ≈ 源库裸 SQL 成本 + 引擎 5~70ms
（1 维 592ms、2 维 1256ms、3 维 1758ms）；即席查询耗时里**源库自身扫描占绝大部分**，
想更快在源表加覆盖索引（DBA 侧动作），而不是换平台。

## 3.2 分级投放：只读访问授权（P6，2026-09-29 实测）

要把大屏投给车间/供应商（只看自己的行、看不到成本列）时，用**访问授权档案**：

```bash
# 建授权（返回体里的 token 只出现一次，务必转存）
curl -s -X POST -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json"   "$SERVER/api/v1/bi/access/profiles" -d '{
  "name": "一车间大屏",
  "boards": ["default"],            // 不填=全部看板
  "row_filters": {"line": "A线"},   // 行过滤：多字段 AND；字段必须是卡片维度
  "hidden_fields": ["defect_qty"]   // 列隐藏：响应里不出现该字段
}'
# 投放链接（令牌走 URL 参数，投放页进来自动入 localStorage 并抹掉地址栏）
#   $FRONTEND/bi/screen?board=default&token=bit_xxx
```

**必须知道的几条（都是实测口径）**：

| 规则 | 说明 |
|---|---|
| 只读 | 档案令牌只能读：写操作（改定义/触发聚合/建授权）一律 403 |
| 行过滤字段必须是卡片维度 | 否则该卡对这份授权**不可见**（404 `not_visible`）——桶按维度组合存，不按该维度分组无法隔离行；`detail` 卡与即席查询是例外（按行过滤） |
| 数值口径 | 桶查询的行过滤是"取对应维度桶"，值等于 `WHERE 字段=值 GROUP BY …` 的直查结果 |
| 明细 | 服务端先过滤后分页（响应标 `fallback="row_permission"`），被隐藏列不出现在行里 |
| 即席 | 授权行过滤是**前置条件**：同名字段以授权值为准（客户端传 `in [A,B]` 也只会拿到 A） |
| 撤销 | 停用/轮换**立即生效**（每请求实时查库）；令牌只存哈希，泄漏即轮换 |
| 管理入口 | 管理页「访问授权」→ `/bi/access`（列表/新建/轮换/停用/复制投放链接） |

## 4. 建看板与摆放（多看板）

```bash
curl -s -H "X-Admin-Token: $TOKEN" "$SERVER/api/v1/bi/screens"                     # 列表（default 恒在）
curl -s -X POST -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  "$SERVER/api/v1/bi/screens" -d '{"name": "生产运行管理"}'                          # 新建 → 返回 {key}
curl -s -H "X-Admin-Token: $TOKEN" "$SERVER/api/v1/bi/screen-settings?board=<key>" # 读设置
```

摆放卡片：PUT 看板设置（**整字段覆盖，body 传顶层结构**，勿包 `data`）：

```bash
curl -s -X PUT -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  "$SERVER/api/v1/bi/screen-settings?board=<key>" -d '{
  "name": "生产运行管理",
  "layout": {},
  "filter_dims": ["产线"],
  "cells": {
    "prod_daily_attain": {"x": 0,  "y": 0,  "w": 18, "h": 5},
    "line_output":       {"x": 0,  "y": 5,  "w": 36, "h": 8},
    "prod_detail":       {"x": 0,  "y": 29, "w": 72, "h": 7}
  },
  "base_w": 1920, "base_h": 1080,
  "screen_adaptor": "keep",
  "theme": "dark"
}'
```

- 网格：**72 列 × 36 行**；`x+w ≤ 72`、`y+h ≤ 36`；**摆放即归属**（cells 里没摆的不上屏）
- 同一指标可摆进多个看板（定义只存一份）；`filter_dims` 的维度名要与指标 `dimensions[].name` 一致
- 显示设置：`base_w/base_h` 基准分辨率、`screen_adaptor` ∈ `keep|widthFirst|heightFirst|full`、
  `theme` ∈ `dark|light`；投放页 30s 内自动跟随，改完无需通知
- 常用排布参考（三层布局）：KPI 条 y=0 h=5 → 趋势层 y=5 h=8 → 状态层 y=13 h=8 → 分析层 y=21 h=8 → 明细 y=29 h=7

## 5. 交付

- 投放地址：`$FRONTEND/bi/screen?board=<key>`（每看板独立 URL）；管理地址 `$FRONTEND/bi`
- 权限：看板 key 与投放地址不是鉴权凭证，按内部系统保护

## 6. 运维与排错

| 症状 | 处置 |
|---|---|
| 改了指标筛选但数据不变 | 快照水位线已追平会跳过重算——**同时删除该指标的桶与水位线**（缺一不可）后重跑聚合 |
| 查询返回空 rows | 该指标尚未聚合（先跑 `aggregate/run`）或 from/to 范围不含桶 |
| POST 422 `list_type` | `dimensions`/`measures` 传了 null，改传 `[]` |
| POST 422 形状校验 | 对照「数据形状校验」行（散点恰 2 度量等） |
| 投放页卡片空白 | 检查指标 `enabled=true`、连接可测通、看板 cells 里摆了该 key |
| 新建看板后看不到卡片 | 「摆放即归属」——去管理页布局编辑从卡片库点选上屏 |
| 删除看板 | `DELETE /bi/screens/<key>`（default 不可删；只删画布摆放，指标定义保留） |

## 7. 端到端示例（2026-09-27 实测跑通，可直接复跑）

```bash
# 场景：把「质量记录」做成看板——今日合格率 KPI + 各产线合格率 bar + 原始明细
CID="<连接ID>"   # 生产运行管理-测试库（SQLite，5 表）
# ① KPI：今日一次合格率（快照源 + 日期等值筛选）
curl -s -X POST -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  "$SERVER/api/v1/bi/metrics" -d "{
  \"metric_key\": \"demo_quality_rate\", \"name\": \"今日一次合格率\", \"source_type\": \"form\",
  \"source_ref\": \"{\\\"connection_id\\\":\\\"$CID\\\",\\\"form_key\\\":\\\"quality_records\\\"}\",
  \"granularity\": \"1min\", \"chart_type\": \"number\", \"dimensions\": [],
  \"measures\": [{\"key\": \"合格率\", \"agg\": \"avg\", \"field\": \"pass_rate\", \"field_type\": \"number\"}],
  \"filters\": [{\"field\": \"date\", \"op\": \"eq\", \"value\": \"2026-09-27\", \"field_type\": \"string\"}],
  \"style\": {\"unit\": \"%\"}, \"top_n\": 100, \"enabled\": true}"
# ② 类别对比：各产线合格率（bar 语义：x=维度值、series=度量）
curl -s -X POST -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  "$SERVER/api/v1/bi/metrics" -d "{
  \"metric_key\": \"demo_line_pass\", \"name\": \"各产线一次合格率\", \"source_type\": \"form\",
  \"source_ref\": \"{\\\"connection_id\\\":\\\"$CID\\\",\\\"form_key\\\":\\\"quality_records\\\"}\",
  \"granularity\": \"1min\", \"chart_type\": \"bar\",
  \"dimensions\": [{\"name\": \"产线\", \"field\": \"line\"}],
  \"measures\": [{\"key\": \"平均合格率\", \"agg\": \"avg\", \"field\": \"pass_rate\", \"field_type\": \"number\"}],
  \"style\": {\"unit\": \"%\"}, \"top_n\": 100, \"enabled\": true}"
# ③ 触发 + 查询
curl -s -X POST -H "X-Admin-Token: $TOKEN" "$SERVER/api/v1/bi/aggregate/run"
curl -s -H "X-Admin-Token: $TOKEN" "$SERVER/api/v1/bi/metrics/demo_quality_rate?format=datav-number"
#    期望 value≈96.37 unit=%（与 SELECT ROUND(AVG(pass_rate),2) FROM quality_records
#    WHERE date='2026-09-27' 对照）
curl -s -H "X-Admin-Token: $TOKEN" "$SERVER/api/v1/bi/metrics/demo_line_pass?format=echarts-bar"
#    期望 x=['A线','B线','C线'] data=[98.5, 95.33, 98.5]
# ④ 建看板 + 摆放（72×36 网格；摆放即归属）
KEY=$(curl -s -X POST -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  "$SERVER/api/v1/bi/screens" -d '{"name":"质量专题演示"}' | python -c "import json,sys;print(json.load(sys.stdin)['key'])")
curl -s -X PUT -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  "$SERVER/api/v1/bi/screen-settings?board=$KEY" -d '{
  "name": "质量专题演示", "layout": {}, "filter_dims": ["产线"],
  "cells": {
    "demo_quality_rate":   {"x": 0,  "y": 0, "w": 24, "h": 5},
    "demo_line_pass":      {"x": 0,  "y": 5, "w": 36, "h": 10},
    "demo_quality_detail": {"x": 36, "y": 5, "w": 36, "h": 10}},
  "base_w": 1920, "base_h": 1080, "screen_adaptor": "keep", "theme": "light"}'
# ⑤ 交付：$FRONTEND/bi/screen?board=$KEY（实测：标题/3 卡/浅色主题/筛选条齐全）
```
