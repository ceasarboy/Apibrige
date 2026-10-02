# add-bi-adhoc-analysis 任务清单

## 1. 服务层（app/services/bi_adhoc.py）

- [x] `build_adhoc_defn(payload)`：请求 → 标准化定义（source_ref 合成、chart_type 默认 bar、
      top_n 默认 100、format 按图表类型映射）——验证：单测 `TestDefn::test_defaults_and_source_ref`
- [x] `query_adhoc_defn(session, defn, *, fmt, sort, ref_names)`：定义校验 → 取值 → 排序 →
      单桶包装 → `convert`——验证：单测三路径 + API 用例
- [x] 引擎标注：`engine` + `fallback_reason`（`can_pushdown` 原因透出）+
      `truncated`（内存路径达 MAX_FORM_ROWS）——验证：单测 pushdown / HAP 内存 / computed 回退
      / 截断（monkeypatch 上限）四态
- [x] 排序（`sort.by` 度量 key 或维度名；数值 → 文本 → 缺值三段，缺值恒末尾）
      ——验证：单测 desc/asc/维度名/未知字段 400

## 2. 接口（app/api/bi.py）

- [x] `POST /api/v1/bi/adhoc/query`（`BiAdhocQueryIn`；400 返回与保存端点同结构的 issues；
      连接不存在 502）——验证：`TestAdhocApi` 四例
- [x] computed 中文引用归一复用保存端点的 `_normalize_calc_refs`——验证：API 用例
      （patch 表结构，断言表达式引用被规范化）
- [x] 字段树沿用既有 `/bi/form-fields`（前端直接用，无新端点）

## 3. 前端分析工作台（pages/bi/analysis.tsx）

- [x] 数据源选择（连接/表单）+ 字段树（BI 类型分组、搜索、data-bitype 标记）
      ——验证：UI 自审字段数 ≥5
- [x] 维度槽 + 指标槽：**点击添加**与**原生 HTML5 拖拽**双通路；指标槽可改算子/删除；
      Top N 与排序控件——验证：UI 自审两条通路各过一项
- [x] 图表类型切换 + 即时出图（复用 `MetricContent`；300ms debounce + 竞态序号丢弃）
      ——验证：UI 自审切 number/bar 均渲染
- [x] 引擎标签（下推/内存 + 回退原因 + 耗时 + 行数/分组 + 截断提示）
      ——验证：UI 自审断言标签文案为"聚合下推 · 124ms · 分组 101"
- [x] 「另存为看板卡片」：弹层（卡片名/metric_key/目标看板）→ 创建定义 → 可选摆放 →
      跳转看板可见——验证：UI 自审另存后看板可见 + **卡片桶值与同参数即席查询逐项一致（101 组）**
- [x] 路由 `/bi/analysis` + 管理页入口按钮——验证：tsc + UI 自审入口点击

## 4. 验收

- [x] 单测 `tests/test_bi_adhoc.py` 16 项——验证：全绿；全量 **1000 passed / 13 skipped**
- [x] 100 万行实测（bench [3a'']）：1 维 / 2 维 / 3 维 / 无维度 接口 592/1256/1758/177ms，
      **引擎开销 5~70ms**（= 接口耗时 − 源库裸 SQL 地板 596/1242/1688/155ms）
      ——验证：脚本输出（**验收口径已校正**，见 design §7：原提案"<1s"在 SQLite
      无索引百万行上不成立，源库本身即 1.2~1.7s）
- [x] `scripts/analysis_ui_check.py`（13 项）全过——验证：脚本输出（含自清理）
- [x] 全量 pytest + tsc + **五个** UI 自审脚本零回归——验证：命令输出
- [x] 文档（design §7 落地记录与偏差 / 本 tasks / project-status / evolution-plan / skill）
      + OpenSpec 归档——验证：文档 diff

## 5. 事故与修复（本期内真实发生，已入 AGENTS 陷阱清单）

- [x] 「另存为卡片」写看板设置时把 board 写进了 body（实为 query 参数）→ 误覆盖**默认看板**
      （14 张卡片 + 看板名）；已从 SQLite 空闲页捞回原 JSON 原样还原，并把调用改为
      `{ params: { board } }`；AGENTS 陷阱 30 记处置手法——验证：五脚本复跑 + 看板 14 卡片在位
