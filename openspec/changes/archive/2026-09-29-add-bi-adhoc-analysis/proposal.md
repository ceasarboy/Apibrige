# add-bi-adhoc-analysis 提案（P2：分析工作台——自助拖拽 + 即席任意维度）

## Why

用户 2026-09-27 拍板"对标 DataEase 并尽可能超越，增加自助拖拽分析"。前置已就绪：
P1（明细服务化）解决大表逐行分析、P3（聚合下推）把 DB 源聚合下推到源库执行
（100 万行首轮 590ms，不受 10 万行闸）——**即席任意维度查询的地基已经存在**，
缺的只是"把能力开放成分析入口"：现在要出图必须先在定义页建一个指标定义。

对标对象（DataEase v3 源码，`E:\dataease-study\`）：
- 前端 `views/chart/components/editor/`：**dimension/quota 双槽** + `vuedraggable` 拖拽
  （`drag-item/DimensionItem.vue`、`QuotaItem.vue`）+ 图表类型面板 + 字段树
  （`visualized/data/dataset/form/`）；配置变更即重算图表数据。
- 后端 `chart/server/ChartDataServer` + `chart/charts/ChartHandlerManager`：视图配置
  （dimension/quota/filter）→ 统一取数服务 → 各图表处理器。
- 其"另存"是把视图存进仪表板；本平台的对应物是**写入指标定义 + 可选摆放**。

## What Changes

1. **后端即席查询** `POST /api/v1/bi/adhoc/query`：请求携带
   `{connection_id, form_key, chart_type, dimensions[], measures[], filters[], computed[],
   top_n, sort, format}` → 即时聚合返回
   `{engine, fallback_reason, row_count, elapsed_ms, defn, data}`：
   - 复用 P3 判定/执行：可下推 → `fetch_grouped_rows`（源库 GROUP BY，不拉全量行）；
     否则（HAP / computed / p95 / 中文列名…）→ `scan_window` + 内存聚合，并**透出回退原因**；
   - 复用定义校验 `validate_metric_def`（维度 ≤3、算子白名单、结构化筛选按类型）——
     与看板卡片同一套口径，避免"分析页能出的图，另存成卡片后校验失败"；
   - 复用格式转换 `convert`（category/scalar/timeseries/…）→ 前端渲染与看板完全同源；
   - 不落库：即席查询不写任何表（除显式"另存为卡片"）。
2. **排序**：`sort: {by: <度量 key | 维度名>, order: asc|desc}`——类别图按度量降序是
   最常用形态（DataEase 有自定义排序项，我们先用最小可用形态）。
3. **前端分析工作台** `/bi/analysis`：
   - 左：连接 + 表单选择 → 字段树（复用 `/bi/form-fields` 的 BI 逻辑类型分组：维度/指标）；
   - 中：维度槽 + 指标槽（**原生 HTML5 拖拽** + **点击添加**双通路；指标可改聚合算子、可删、
     可调 Top N 与排序）；图表类型选择器；
   - 右：即时出图（复用 `MetricContent` + `buildOption`，18 种图表同一渲染器），
     显示**引擎标签**（下推/内存）+ 回退原因 + 耗时 + 分组行数；
   - 底：「另存为看板卡片」→ 填卡片名/看板（可选）→ 写标签定义 + 可选摆放，跳转看板可见。
4. **入口**：BI 管理页顶部加"分析工作台"入口按钮。

## Impact

- **specs**：`bi-aggregation` 新增 1 组需求（即席分析：查询契约 + 引擎透出 + 另存转化）
- **代码**：
  - `app/api/bi.py`（+ `POST /adhoc/query`，复用 `_load_form_field_meta` 口径）
  - `app/services/bi_adhoc.py`（新增：请求 → 定义归一 + 聚合 + 桶包装，纯逻辑便于单测）
  - `frontend/src/pages/bi/analysis.tsx`（新增页）+ `App.tsx` 路由 + `index.tsx` 入口
  - 复用：`bi_sources.can_pushdown/try_pushdown_rows`、`bi_aggregator.entries_from_grouped/
    aggregate_rows`、`bi_defs.validate_metric_def`、`bi_format.convert`、前端
    `BiBoard.MetricContent/buildOption/loadFormFields/nextFreeCell`
- **不做（非目标）**：
  - 多表单/跨源 join（分析单元仍是"连接 + 表单"；跨表维度继续用定义页的 resolver）
  - 计算字段的**可视化表达式编辑器**（分析页透传 `computed`，但表达式仍在定义页配）
  - 钻取/下钻、度量级 filter（条件聚合）、同环比、导出图片/Excel
  - 引入 DnD 三方库（原生 HTML5 DnD，零新增依赖）
  - 内嵌列存加速层（DuckDB 仍为评估项，不属本期）
- **验收**（口径经实测校正，见 design §7.1）：
  1. DB 源任意维度组合（1~3 维）× 数值度量：**下推命中**，且**引擎开销 ≤500ms**
     （引擎开销 = 接口耗时 − 源库裸 SQL 地板），数值与源库 SQL 直查逐项一致；
  2. HAP 源/含 computed 的定义：走内存路径且有明确 fallback 提示（不谎报下推）；
  3. 拖拽到出图全流程浏览器实测（含图表类型切换）；
  4. 「另存为看板卡片」后看板可见、数值与工作台一致；
  5. 全量 pytest 零回归 + tsc 零错误 + 既有 UI 自审脚本零回归 + 新增分析页自审脚本。

> 校正说明：原写『任意维度组合 <1s』——实测发现 SQLite 无索引 100 万行表的 2~3 维
> GROUP BY **裸 SQL 本身**即 1.2~1.7s（源库成本，与是否走平台无关），故达标线改为
> 『引擎开销 ≤500ms』，实测 5~70ms。
