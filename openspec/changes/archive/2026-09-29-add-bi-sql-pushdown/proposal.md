# add-bi-sql-pushdown 提案（P3：聚合下推 + 单源上限解除 + 并发硬化）

## Why

对 DataEase 的对比结论（`docs/bi-comparison-finebi-dataease.md`）与用户拍板：
**提升单源上限、支撑 TB 级、并发加固**。现状：form 源聚合把全表拉到 Python 内存
（`MAX_FORM_ROWS=10 万` 硬闸），百万行表不可用，且聚合耗时随行数线性增长
（P1 实测 9.5 万行 1.2s，百万行预计 12s+，期间挤占事件循环）。

前期调研（Explore 子代理，源码级）已产出驱动现状清单、方言差异表与回退策略，
现固化为本变更设计（见 design.md）。

**关键语义澄清**：form 源是快照语义（`_scan_form` 忽略 start/end 时间窗）——
下推**不需要把时间窗进 WHERE**，只需把「定义级 filters → WHERE」+「GROUP BY 聚合」
下发，其余（Top N、比率、分组归一）保持 Python 侧不变。语义冲突不存在。

## What Changes

1. **驱动层 `fetch_grouped`**（新方法，缺省 NotImplementedError → 调用方回退内存）：
   `SELECT 维度列, 聚合函数 … FROM 表 [WHERE filters] GROUP BY 维度 [ORDER BY ...][LIMIT ...]`，
   标识符白名单 + 全值绑定；先实现 Sqlite/PostgreSQL/MySQL；**Oracle 默认回退**
   （11g ROWNUM + 聚合 + 排序组合行为需单独验证，能力标志可开）。
2. **可下推判定 `can_pushdown(defn, conn)`**（纯函数）：不满足即回退内存并缓存判定；
   回退清单：p50/p95/p99、computed 派生列、resolver 维度、groups 分组归一、
   contains/empty 类过滤、custom_sql、HAP、标识符含非 ASCII、Oracle（默认）、
   维度组合数超限。
3. **聚合下推接入** `compute_window`：可下推 → `fetch_grouped` → entries（结果值与
   内存路径归一：维度值 `dim_value` 尾部语义、数值 `float()`、NULL→"未知"）；
   任何异常回退内存并标记（不中断聚合）。
4. **单源上限解除（DB 源）**：下推路径不受 `MAX_FORM_ROWS` 约束（不再全量拉行）；
   HAP 与回退路径维持 10 万闸 + 5 万分档提示。
5. **并发硬化**：内存聚合路径（含回退）改 `asyncio.to_thread`（不占事件循环）；
   新增"在飞重查询"信号量（默认 ≤4，超限 429/降级提示）；红利用 SQL 级 COUNT(*)
   或行数探测（大表不拉全量）。
6. **存量修复**：Oracle DB form 源大小写（fetch 返回小写键 vs 元数据大写列名，
   `get_field` 精确匹配）→ 统一"逻辑键映射"（大小写互认，对齐 AGENTS 陷阱 9 约定），
   内存与下推两条路径共用。

## Impact

- **specs**：`bi-aggregation` 新增 1 组需求（聚合下推与回退）+ 修改「快照聚合语义」
  （DB 源下推路径的行数上限语义）
- **代码**：`app/connectors/database.py`（fetch_grouped + 三驱动）、
  `app/services/bi_sources.py`（can_pushdown + 下推扫描）、
  `app/services/bi_aggregator.py`（compute_window 分支 + to_thread + 信号量 + 键映射）
- **前端/其它**：无需改（查询接口契约不变）
- **验收**：100 万行基准（`scripts/bi_pushdown_bench.py`，子代理产出中）——
  下推聚合 <3s、聚合期间投放查询无 >500ms 抖动、数值与 SQL 直查逐项对照、
  既有 914 passed 零回归
- **不做**（明确）：跨库 join、条件聚合（度量级 filter 的 CASE WHEN 版）、
  同环比（快照语义）；Oracle 下推验证后置（默认回退不影响正确性）
