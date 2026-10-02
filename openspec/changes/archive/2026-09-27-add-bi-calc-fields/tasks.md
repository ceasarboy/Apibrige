# add-bi-calc-fields 任务清单（已归档）

## 0. 拍板（2026-09-27 已完成）

- [x] P1 表达式引用语法：**中文显示名优先，回落字段 path**
- [x] P2 函数集起点：**数值 7 件 + 字符串 + 日期**（round/abs/ceil/floor/min/max/coalesce
      + substring/upper/lower/trim/concat + date_part）
- [x] P3 聚合比语法：**numerator/denominator 对象**（除零 null，scale 可选）
- [x] P4 展示层：**占比 + 累计本轮一起做**（累计仅滚动源，快照源拒绝）

## 1. 后端：表达式引擎（拍板后）

- [x] `app/services/bi_expr.py` 新模块：AST 白名单解析 + 引用解析（中文名/path）+ 求值 + 循环检测
- [x] `bi_defs.validate_metric_def`：computed/ratio/groups 结构与引用校验（422 文案友好）
- [x] `bi_aggregator`：行级派生列求值（在 scan/聚合前置）→ 聚合照旧
- [x] `bi_aggregator`：`ratio` 算子（除零 null）
- [x] `bi_aggregator`：维度值分组归一（groups/other）
- [x] 单测：解析/求值/循环引用/除零/分组命中

## 2. 前端：定义编辑器

- [x] 定义页「计算字段」区：表达式输入（字段与函数插入辅助）+ 名称/类型
- [x] 维度分组配置弹层（值列表 → 组映射 + 其他归组）
- [x] 度量聚合方式下拉增加「比率（分子/分母）」配置
- [x] tsc + UI 实测（含控制台门禁）

## 3. 展示层与交付

- [x] bi_format：`show_percent`（柱状/表格占比）与 `cumulative`（仅滚动源）
- [x] 生产测试库示例：整体达成率（ratio）+ 合格率派生列 + 车间分组维度
- [x] 数值对照（SQL 直查基准）+ Playwright 看板验收 + `scripts/bi_calc_fields_smoke.py`
- [x] 文档：`bi-dashboard-guide.md` / `skills/bi-dashboard/SKILL.md` 增计算字段章节
- [x] OpenSpec 归档合并基线
