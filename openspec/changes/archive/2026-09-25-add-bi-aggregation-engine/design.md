# BI 聚合引擎 设计

## Context

| 输入 | 事实 |
|---|---|
| 数据量 | 每分钟 **1 万–5 万组**数据，**每组 5–10 个字段**（→ 167–833 行/秒；日 1,440 万–7,200 万行；每分钟需提取 **5 万–50 万个字段值**） |
| 中间数据 | `data_buffer.source_data` / `transformed_data`（JSON），**无清理逻辑**，无限增长 |
| 执行元数据 | `execution_logs`（action/status/duration_ms/error_message，有 task_id 无 connection_id） |
| 关联路径 | `execution_logs.task_id → sync_tasks.mapping_id → field_mappings.{source,target}_connection_id` |
| JSON 能力 | 列类型为通用 `JSON`（**非 JSONB**），PG 下无 GIN 索引；SQLite 用 `json_extract` |
| 前端现状 | 仅 `@ant-design/charts`（G2）；无 ECharts / DataV |
| 部署约束 | 内网离线、单 worker、SQLite 与 PG 双兼容 |

## Goals / Non-Goals

**Goals**

1. 建立可复用的**聚合引擎**：定义驱动、增量计算、幂等可回填。
2. 交付 **3–10 组**开箱可用的聚合指标（运维 + 业务 + 大屏）。
3. 建立**格式转换层**，把聚合结果转成 ECharts / DataV / 表格可直接渲染的结构。
4. 在每分钟 1 万–5 万组（每组 5–10 字段）的量级下，**查询延迟 < 100ms**、**单指标后台聚合 < 3s**。
5. 防止多维组合导致的**结果基数爆炸**（每组 5–10 字段意味着维度候选多）。

**Non-Goals**：见 `proposal.md`「非目标」。

## 性能预算（先算清楚，再选方案）

| 环节 | 量级 | 预算 | 结论 |
|---|---|---|---|
| 明细写入 | 833 行/秒（每行 5–10 字段） | 已有链路 | 不改 |
| **禁止**：实时全表聚合 | 日 7200 万行 | 秒级恶化 | 明细表**不做**实时聚合 |
| 单窗口 `GROUP BY`（不含 JSON） | 5 万行 | **20–50ms**（`created_at` 有索引） | 可接受 |
| **⚠️ JSON 字段提取（原瓶颈）** | 5 万行 × 8 字段 = **40 万次取值** | Python 侧每行 `json.loads` **一次** ≈ **0.1–0.2s**（原 SQL 侧逐字段解析约 2–4s） | 已消解（见 D5） |
| 单指标聚合总预算 | 一个窗口 | **< 3s**（后台任务，不在请求路径） | 可接受 |
| 一轮 10 指标 | 每分钟 | 并发执行下 **< 10s** | 周期内可完成 |
| 聚合结果增长 | 1440 桶/天 × 维度组合数 | **必须受控**（见 D11），否则十万级/桶 | 需硬保护 |
| 查询 | 命中预聚合表 | **< 10ms** | 满足大屏 1 分钟刷新 |

**核心结论**：

1. 以“每分钟一个窗口”为计算单元，聚合成本恒定、不随历史增长。
2. **聚合在 Python 侧执行**（详见 D4 修订）：分位数与过滤度量跨库统一，JSON 每行只解析一次。
3. 叠加**维度基数硬限制**（D11）保护结果规模。

## 数据流

```
同步链路（不改动）
   │
   ├─→ data_buffer   (中间数据 JSON, 1万–5万/分钟)  ─┐
   └─→ execution_logs(执行元数据)                    │
                                                     ▼
                                    ┌────────────────────────────────┐
                                    │  bi_aggregator（每分钟 tick）   │
                                    │  1. 取水位线 → 确定待算窗口     │
                                    │  2. 窗口内扫描 + 提取 JSON 维度 │
                                    │  3. GROUP BY 维度计算度量       │
                                    │  4. 按 (指标,桶,维度) 覆盖写入  │
                                    │  5. 推进水位线                 │
                                    └────────────────────────────────┘
                                                     │
                                                     ▼
                                       bi_metric_buckets（预聚合结果）
                                                     │
                                        GET /api/v1/bi/metrics/{key}
                                                     │
                                                     ▼
                                    bi_format（格式转换层）
                                       ├─ echarts-line/bar/pie/heatmap
                                       ├─ datav-number（翻牌）
                                       └─ table
                                                     │
                                                     ▼
                                         React BI 大屏（ECharts + DataV）
```

## Decisions

### D1：聚合模式 —— 每分钟增量批聚合

- **触发**：定时 tick（60s），计算**上一个完整分钟桶** `[T-1min, T)`。
- **理由**：与"指标每分钟更新"需求天然匹配；解耦同步链路（聚合失败不影响搬运）；窗口固定使成本恒定。
- **否决**：写入时聚合（耦合传输链路，一次聚合异常会影响数据搬运）；物化视图（全量重算随时间变慢，且 SQLite 不支持）；流式（本机/离线无 MQ）。

### D2：幂等语义 —— 窗口整体覆盖，绝不累加

```sql
INSERT INTO bi_metric_buckets (metric_key, bucket_start, dim_hash, dimensions, values, computed_at)
VALUES (...)
ON CONFLICT (metric_key, bucket_start, dim_hash)
DO UPDATE SET values = EXCLUDED.values, computed_at = EXCLUDED.computed_at;
```

- **理由**：累加型 UPSERT 在重跑/回填时会**重复计数**；覆盖型天然幂等，重跑多少次结果一致。
- **代价**：每窗口需完整重算（但成本已被性能预算证明可接受）。

### D3：水位线 —— 支持断点续跑与回填

`bi_aggregation_watermark(metric_key PK, last_bucket, updated_at)`

- 正常：`last_bucket` 推进到 `T-1min`。
- 重启/失败：从 `last_bucket` 续跑，逐桶补齐（**逐桶**，不合并成大窗口，保持成本恒定）。
- 回填：`POST /api/v1/bi/aggregate/run?metric=&from=&to=` 指定区间重算（覆盖语义保证安全）。
- 滞后保护：若 `last_bucket` 落后超过阈值（如 1 小时），逐桶追赶并在日志告警。

### D4：数据源适配与**执行位置** —— 聚合在 Python 侧完成

`app/services/bi_sources.py` 提供 `scan_window(source, start, end) -> rows`，只负责**取窗口内原始行**（不写聚合 SQL）：

| source_type | 说明 |
|---|---|
| `data_buffer` | 扫窗口内行，取 `transformed_data`（缺省回退 `source_data`）等列 |
| `execution_logs` | 扫窗口内执行元数据 |
| `custom_sql` | 管理员提供的只读 SQL，带 `:start`/`:end` 参数（需只读校验） |

**分组、度量、分位数、Top N 全部在 Python 侧完成**。理由：

1. **分位数无法跨库用 SQL 统一表达**（PG 有 `percentile_cont`、SQLite 没有）——而项目既有的 `app/api/metrics.py` 已经是“取原始行 + Python 侧分位”模式，本引擎沿用同一模式，保持一致；
2. **过滤度量**（`count` filter `status=completed`）在 SQLite 无 `FILTER` 语法，Python 侧无方言差异；
3. **JSON 只在 Python 侧解析一次/行**，天然规避 D5 的重复解析问题；
4. **维度解析器**（task → 连接）只需内存字典查找，无需 SQL join；
5. **SQLite 与 PG 结果一致性**：同一实现，无方言分叉。

**代价**：单窗口需将行拉取到内存。按 5 万行 × 约 1KB 估算峰值约 50MB，可接受；超阈值时按主键分片（D5）。

### D5：JSON 解析成本 —— 由“Python 侧一次解析”消解（含修订说明）

**修订说明**：初版设计假设聚合在 SQL 侧执行，因而担心 `json` 列逐字段 `->>` 会重复解析（5 万行 × 8 字段 ≈ 2–4s），并提出“迁 JSONB + 一次性转换”。实现阶段确认**聚合改为 Python 侧执行**后，该问题自然消解：

| 方案 | 成本（5 万行 × 8 字段） |
|---|---|
| SQL 侧 `json` 列逐字段 `->>` | 每字段各解析一次 ≈ 2–4s ❌ |
| SQL 侧 `jsonb` 列 | ≈ 0.2s |
| **Python 侧 `json.loads` 一次/行** | **≈ 0.1–0.2s** ✅ |

**因此修订决策**：

1. **不迁移 JSONB**——省去 schema 变更，也避开 SQLite 无等价物的问题；
2. JSON 在 Python 侧**每行只解析一次**，后续取多个字段是内存字典访问；
3. **保留分片兜底**：单窗口行数超阈值（默认 20 万）时按主键分片扫描后合并，分片结果 MUST 与不分片一致；
4. **不采用**“为每个业务维度加物理列”：维度由配置驱动，加列会让 schema 随配置漂移。

**教训记录**：“窗口化”解决的是**成本随时间增长**；“每行只解析一次”解决的是**单次成本**。两者是不同维度的问题，设计时不应互相代替。

### D6：存储模型（3 张表）

```
bi_metric_defs          # 聚合定义（配置驱动，3–10 组）
  metric_key UNIQUE, name, source_type, source_ref, time_field,
  granularity, dimensions JSON, measures JSON, filters JSON,
  chart_type, format_config JSON, enabled

bi_metric_buckets       # 预聚合结果
  metric_key, bucket_start, dim_hash, dimensions JSON, values JSON, computed_at
  UNIQUE(metric_key, bucket_start, dim_hash)
  INDEX(metric_key, bucket_start)

bi_aggregation_watermark
  metric_key PK, last_bucket, updated_at
```

- `dim_hash`：维度组合的稳定哈希，作为 UPSERT 冲突键（避免长 JSON 比较）。
- `values`：度量结果 JSON（如 `{"success": 120, "failed": 3}`），避免为每个度量加列。

### D7：调度与并发 —— 复用既有模式

- 沿用 `scheduler.py` 的 tick + Redis 锁模式，锁名 `bi_aggregation_tick`（ttl=120s）；
- Redis 不可用时降级为无锁执行（单 worker 默认部署安全），告警只打一次；
- 聚合任务与 `scheduler_loop` 独立，**互不阻塞**（同步调度优先）。

### D8：格式转换层 —— 后端转换，前端只渲染

**中间格式**（引擎内部统一结构）：

```json
{
  "metric_key": "sync_throughput",
  "chart_type": "line",
  "type": "timeseries",
  "dimensions": [{"name": "bucket", "label": "时间"}],
  "x": ["2026-09-25T10:01:00Z", "2026-09-25T10:02:00Z"],
  "series": [
    {"key": "success", "label": "成功", "data": [120, 135]},
    {"key": "failed",  "label": "失败", "data": [2, 0]}
  ]
}
```

**适配器输出**：

| chart_type | 输出结构 | 适用 |
|---|---|---|
| `line` / `bar` | `{xAxis:{data}, series:[{name,type,data}]}` | 趋势、对比 |
| `pie` | `[{name, value}]` | 占比 |
| `heatmap` | `{xAxis, yAxis, data:[[x,y,v]]}` | 时间×任务 成功率 |
| `table` | `{columns:[{title,dataIndex}], rows:[]}` | 明细排行 |
| `number` | `{value, unit, trend}` | 大屏翻牌 |

- **决策理由**：格式契约集中在后端，前端只负责渲染；新增图表类型无需改前端数据结构。
- 前端仍可用 `/api/v1/bi/metrics/{key}?format=raw` 拿中间格式自行处理。

### D9：前端选型 —— ECharts + 自研装饰组件（已确认）

- **ECharts**：按需引入（`echarts/core` + `LineChart/BarChart/PieChart/HeatmapChart` + `CanvasRenderer`），避免全量包体积；
- **DataV**（用户已确认）：**不引入**任何 DataV 实现（主流实现面向 Vue，React 无官方版本）。大屏装饰（数字翻牌、边框、发光效果）**自研轻量 CSS 组件**，零外部依赖；
- 与既有 Ant Design 管理界面并存，互不影响。

### D10：数据保留 —— 明细 7 天，聚合永久（已确认）

- `data_buffer` 明细按 `expires_at` 清理，**保留 7 天**（用户确认，可配），补上当前完全缺失的清理能力；
- `bi_metric_buckets` 永久保留（聚合后明细可删）；
- 清理任务与聚合 tick 同源调度，批量删除（分批 5000 行，避免长事务）。

### D12：大屏投放模式 —— 独立投放页（已确认）

用户确认**需要独立投放模式**（适合作战室/车间大屏）。

- **路由**：`/bi`（管理界面内的普通视图）+ `/bi/screen`（独立投放，无导航栏、无侧边栏、深色底）；
- **全屏**：提供一键全屏（Fullscreen API）与退出入口；
- **轮播**：支持自动轮播（按配置周期切换指标分组/页面），可用空格键暂停；
- **自适应**：以 1920×1080 为基准做等比缩放（`transform: scale`），适配不同大屏分辨率；
- **刷新**：数据按 1 分钟周期刷新，刷新时不得闪烁或重置轮播进度。

### D11：维度基数控制 —— 5–10 字段的组合爆炸防护

每行有 5–10 个字段，**绝不能全部作为分组维度**：若 8 个字段各 50 个不同值，**双字段**组合即 2,500 种、**三字段**达 12.5 万种，单桶就能产出十万级行。

**防护措施**：

1. **硬限制**：单个指标的 `dimensions` 数量 ≤ **3**（配置校验直接拒绝，而非仅告警）；
2. **基数预估**：保存定义时对最近一个窗口执行 `count_distinct` 预估组合数，超过阈值（如 5,000）时**警告并要求显式确认**；
3. **Top N 截断**：分组结果按主度量降序取 Top N（默认 **100**），其余归入单项 `其他`——保证单桶行数有界；
4. **时间桶不占配额**：时间维度是隐式必有维度，不计入 3 个限制；
5. **基数监控**：单桶维度组合数超阈值时记录告警，提示降低维度数量。

**结果规模上界**（Top N = 100 时）：单桶 ≤ 100 行；即便 10 个指标全部按小时桶算，日增也在**万级**（±
仅业务类指标可能触及上界，运维类指标维度基数天然很低）。

## 3–10 组聚合指标设计

| # | metric_key | 名称 | 数据源 | 粒度 | 维度 | 度量 | 图表 |
|---|---|---|---|---|---|---|---|
| 1 | `sync_throughput` | 同步吞吐趋势 | `data_buffer` | 1min | 时间桶 | `count`、`count(status=completed)`、`count(status=failed)` | line（多系列面积） |
| 2 | `latency_percentile` | 延迟分位趋势 | `execution_logs.duration_ms` | 5min | 时间桶 | `p50`、`p95`、`p99`、`avg` | line（多线） |
| 3 | `error_top` | 错误类型 Top N | `execution_logs.error_message` | 1h | 归一化错误类型 | `count` | bar（横向） |
| 4 | `task_success_heatmap` | 任务成功率热力 | `execution_logs` | 1h | 时间桶 × task | `success_rate` | heatmap |
| 5 | `task_health` | 任务健康明细 | `execution_logs` × `sync_tasks` | 1h | task | `success`、`failed`、`avg_duration_ms`、`max_duration_ms` | table |
| 6 | `connection_health` | 连接健康总览 | `execution_logs` × `field_mappings` | 1h | connection × side | `success_rate`、`avg_latency_ms`、`failed` | table |
| 7 | `flow_volume` | 数据流向流量 | `execution_logs` × `field_mappings` | 1h | 源连接 → 目标连接 | `rows`、`runs` | bar（分组）/ sankey |
| 8 | `biz_dimension_summary` | 业务维度汇总 | `data_buffer.transformed_data` | 1h | 配置的 JSON path（如 `category`） | `count`、`sum(配置的数值字段)` | pie / bar |
| 9 | `biz_topn` | 业务 Top N 排名 | `data_buffer.transformed_data` | 1d | 配置的 JSON path | `sum` / `count` | bar（横向，Top 10） |
| 10 | `kpi_cards` | 大屏 KPI 卡组 | 复用 #1/#2/#5 结果 | 实时读 | — | 今日总量、成功率、平均耗时、活跃任务 | number（翻牌） |

**分组说明**：

- **#1–#7 运维组**：数据源都在平台库内，**开箱即用，无需业务配置**。
- **#8–#9 业务组**：需要管理员指定 JSON path（聚合哪个业务字段），属于配置驱动；未配置时不启用。
- **#10 大屏组**：不独立计算，直接复用前序聚合结果，保证口径一致。
- **维度纪律（D11）**：每个指标的显式分组维度数 ≤ 3（时间维不计入），结果按主度量 Top 100 截断；#8/#9 业务组尤其需要遵守（字段组合最易爆炸）。

**度量算子**（`measures JSON` 支持）：

```json
[
  {"key": "success", "agg": "count", "filter": {"status": "completed"}},
  {"key": "p95", "agg": "p95", "field": "duration_ms"},
  {"key": "amount_sum", "agg": "sum", "field": "transformed_data.amount"}
]
```

支持的算子：`count` / `sum` / `avg` / `min` / `max` / `p50` / `p95` / `p99` / `count_distinct`。

### 业务维度候选与基数分级（用户已确认）

分组维度候选共 8 个：**人、设备、物料、计划号、客户、类别、状态、地区**；求和度量（数量/金额/时长等）**由用户配置，不开箱预设**。

按典型基数分级——这直接决定哪些能直接分组、哪些只能做排名：

| 维度 | 典型基数 | 直接分组 | 建议用法 |
|---|---|---|---|
| 类别 | 低（10–50） | ✅ | 首选分组维度 |
| 状态 | 低（5–20） | ✅ | 首选分组维度 |
| 地区 | 低（10–100） | ✅ | 适合分组 |
| 客户 | 中（100–5,000） | ⚠️ | 分组需 Top N 截断 |
| 人 | 中（50–1,000） | ⚠️ | 分组需 Top N 截断 |
| 设备 | 中（50–1,000） | ⚠️ | 分组需 Top N 截断 |
| **物料** | 高（1,000–50,000） | ❌ | **仅作 Top N 排行**，不作分组维度 |
| **计划号** | 高（1,000–100,000+） | ❌ | 近乎唯一，仅作筛选/明细，不作分组 |

**推荐维度组合**（≤ 3 个，D11）：

- 安全组合：`类别 × 状态 × 地区`（全低基数）
- 常用组合：`客户 × 类别`、`设备 × 状态`
- 排行场景：单维度 `物料` / `计划号` + Top 10

**度量配置（用户设置，不预设）**：

- 字段经 JSON path 指定，算子支持 `sum/avg/min/max/count/count_distinct`；
- **时间型字段必须区分**：`时间点`（时间戳，求和无意义，仅允许 `min`/`max`）与 `时长`（可 `sum`/`avg`）——这是用户提到"时间"时最大的语义陷阱；
- 度量配置错误（字段不存在 / 类型不匹配 / 时间点误用 sum）在建定义时即校验拒绝。

## Risks

| 风险 | 缓解 |
|---|---|
| 单桶行数远超预期（> 20 万） | D5 兜底：按 `id` 分片扫描后合并 |
| **JSON 提取拖慢单指标聚合**（5 万行 × 8 字段 = 40 万次取值） | D5：PG 迁 JSONB + 聚合前一次性 `::jsonb` 转换；SQLite 限制字段数 |
| **维度基数爆炸**（5–10 字段组合） | D11：维度数 ≤ 3 硬限制 + Top N=100 截断 + 基数预估告警 |
| 聚合任务失败导致数据空洞 | D3 水位线续跑 + 手动回填端点 |
| 多 worker 重复聚合 | D7 Redis 锁；锁失效时覆盖语义仍保证结果正确（幂等） |
| 业务维度 JSON path 配错 | 配置校验 + 空结果告警；不删除旧桶（可回填修正） |
| DataV 无合适 React 实现 | D9 自研装饰组件兜底，不阻塞主体 |
| 明细清理误删排障数据 | D10 保留天数可配；聚合结果永久保留作为兜底 |
| SQLite 与 PG 行为差异 | D4 方言集中处理 + 双库测试用例 |

## Migration

- 新增 3 张表，随 `create_all` 自动创建，无数据迁移。
- 内置 10 组指标定义以**初始化数据**写入（首次启动 seed，已存在则跳过）。
- `data_buffer` 存量数据不回填聚合（历史桶可经回填端点按需生成）。

## Open Questions

**已全部确认**：

1. **业务维度候选**：人、设备、物料、计划号、客户、类别、状态、地区（度量由用户配置，不开箱预设）
2. **明细保留天数**：7 天（聚合结果永久）
3. **DataV 实现**：自研轻量装饰组件（不引入社区 React 移植版）
4. **大屏投放模式**：需要独立投放模式（全屏、隐藏导航、支持轮播）

**剩余待校准**（实现期按真实数据调整，不阻塞开工）：

1. 各维度的**实际取值基数**——用于校准 Top N 与基数阈值（当前按典型值估：类别/状态/地区低、客户/人/设备中、物料/计划号高）。
2. 单桶**真实行数**——用于确认 D5 的分片阈值与 D11 的 Top N 取值。
