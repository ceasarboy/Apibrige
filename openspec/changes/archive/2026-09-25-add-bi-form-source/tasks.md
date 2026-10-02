# add-bi-form-source 变更

## Delta Specifications

- [x] [bi-aggregation](specs/bi-aggregation/spec.md)

## Tasks

## 1. form 数据源

- [x] 1.1 `app/services/bi_defs.py`：SOURCE_TYPES 加入 `form`；CHART_TYPES 加入 `detail`；
      `is_valid_form_ref` source_ref 结构校验；`classify_bi_type` 逻辑类型归一
- [x] 1.2 `app/services/bi_sources.py`：`parse_form_ref` / `fetch_form_rows`
      （HAP 与 DB 双路径、60s TTL 缓存、并发按 key 去重、10 万行硬上限）/ `_scan_form`
      行形状 `{"data", "fetched_at"}`
- [x] 1.3 取数失败带原因抛 `SourceError`（调度侧降级记日志，凭证不外泄）

## 2. 快照聚合语义

- [x] 2.1 `app/services/bi_aggregator.py`：form 源 `run_metric` 只算最新完整窗口，
      水位线直接前跳，已追平零工作
- [x] 2.2 `backfill` 对 form 源拒绝（SourceError）
- [x] 2.3 buffer/logs/custom_sql 三种源行为零变化（既有测试全绿）

## 3. API

- [x] 3.1 `GET /bi/form-fields`：字段元数据 + `bi_type` 归一（复用字段端点口径与字段字典中文名）
- [x] 3.2 `GET /bi/form-rows`：原始行明细（limit ≤500，共享行缓存）
- [x] 3.3 `query_metric` detail 分支：短路桶读取直出 `{rows}`；格式 `bi-detail` 透传注册
- [x] 3.4 响应不含凭证与内部敏感信息（行数据为业务表单内容）

## 4. 内置指标撤屏

- [x] 4.1 `BUILTIN_METRICS` 全部 `enabled=False`
- [x] 4.2 一次性收敛脚本 `scripts/bi_retire_builtin_metrics.py`（存量库 builtin 项停用，幂等）

## 5. 前端

- [x] 5.1 定义页：数据源默认「连接表单」；连接→表单→字段级联（fields 候选按 bi_type 分类）
- [x] 5.2 图表类型加「明细表」（detail 隐藏维度/度量配置）
- [x] 5.3 大屏支持 detail 图表渲染；标题改「业务数据大屏」
- [x] 5.4 `npx tsc --noEmit` 零错误 + UI 验证脚本 `scripts/bi_form_ui_check.py`

## 6. 测试与验证

- [x] 6.1 `tests/test_bi_form_source.py`：类型归一 / source_ref 校验 / 取数与缓存 /
      快照语义 / 端点（24 用例）
- [x] 6.2 全量 `pytest tests/ -q` 绿（818 passed / 13 skipped）
- [x] 6.3 真实端点冒烟 `scripts/bi_form_smoke.py`（HAP 真实表单 + SQLite 测试表）
