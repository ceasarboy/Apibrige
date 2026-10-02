# add-bi-form-source 设计

设计依据：`docs/bi-dataease-study.md`（DataEase v3 源码研究，只借鉴设计不复制代码）。

## D1 form 数据源与行形状

`scan_window` 分发新增 `form` 分支。`source_ref` = `{"connection_id","form_key"}` JSON
（复用 `source_ref` 列，custom_sql 已用同列存 SQL）。

取数走平台同源读行路径（无映射适配器构造，字段端点同款）：
- HAP：`build_adapter_config(conn)` + `HapV3Adapter.get_worksheet_rows(form_key)`
  （工厂注册的 "hap" 即它，1000/页自动翻页，自身封顶 10 万行）
- DB：`fetch_from_db(db_type, decrypt_db_config(...), table)`（同步引擎同款入口）

行统一为 `{"data": 原始行, "fetched_at": 拉取时刻}`：
聚合侧 `get_field()` 顶层未命中即进 `data` 取值——HAP 行键（alias||controlId）
与字段端点 `path` 一致，维度/度量直接用 `path` 配置，无需行键翻译。

## D2 查询防护三件套（借鉴 DataEase）

- 拉行硬上限 10 万行（超出截断 + warning 日志）；HAP 适配器另有自身翻页上限
- 明细端点 `limit ≤ 500`
- 聚合结果沿用既有 TopN 截断与维度 ≤3 校验

## D3 快照聚合语义

- 每轮 tick 只计算**最新一个完整窗口**（`fetched_at` 即行时间；窗口内不过滤行）；
- 水位线直接前跳到该窗，已追平则零工作；
- `backfill` 对 form 源拒绝（SourceError）——历史窗口回溯只会重复同一份当前快照；
- 60s TTL 进程缓存（key = connection_id:form_key，并发按 key 加锁去重），
  同一表单的多个指标与明细查询每分钟只拉一次源。

与既有源的关系：buffer/logs/custom_sql 三种源的增量窗口/回填/水位线行为零变化。

## D4 字段逻辑类型归一

`GET /bi/form-fields` 复用字段端点口径（path/name/type + 字段字典中文名合并），
并叠加 `classify_bi_type`：HAP 中文名（数值/金额/自动编号→number，日期→time）
与 DB 原始列类型关键字两级匹配，归一为 `dimension|number|time`。
定义页据此自动分类维度/度量候选、自动设置 field_type，不让用户猜类型。

## D5 明细查询

图表类型 `detail`（仅 form 源，校验器把关）。`query_metric` 对 detail 短路桶读取：
实时拉行（共享行缓存）→ `{rows: [...]}` 直出（格式 `bi-detail` 透传，不经桶转换）；
列标题由前端用 `/bi/form-fields` 元数据按 `path` 拼接。

## D6 内置指标撤屏

10 组内置指标 `enabled=False`（BUILTIN_METRICS 默认值，新库 seed 即停用）。
存量库不做启动时强制收敛——那会覆盖用户手动启用的修改（违背 seed「不覆盖用户修改」原则）；
改为一次性脚本 `scripts/bi_retire_builtin_metrics.py`：把 `builtin=True` 且仍在启用的
指标停用（幂等，可重复执行），由部署方按需运行一次。
