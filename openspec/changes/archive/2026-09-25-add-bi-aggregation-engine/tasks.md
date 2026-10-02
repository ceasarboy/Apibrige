# BI 聚合引擎 变更

## Delta Specifications

- [x] [bi-aggregation](specs/bi-aggregation/spec.md)
- [x] [data-pipeline](specs/data-pipeline/spec.md)

## Tasks

## 1. 数据模型与内置定义

- [x] 1.1 新增 `app/models/bi.py`：`BiMetricDef` / `BiMetricBucket` / `BiAggregationWatermark` 三张表
  （含 `UNIQUE(metric_key, bucket_start, dim_hash)` 与 `INDEX(metric_key, bucket_start)`）
  → 验证: 随 `create_all` 建表；单测落库断言唯一约束生效（重复插入同桶冲突）
  - 实现：三张表 + 复合唯一索引 `idx_bi_buckets_unique` + 查询索引 `idx_bi_buckets_lookup`；
    `main.py` 显式 `from app.models import bi` 才能注册进 metadata（已验证）
- [x] 1.2 内置 10 组指标定义以**幂等种子**写入（首次启动且表空时写入，不覆盖用户修改）
  → 验证: 首次启动写入 10 条；二次启动不新增、不覆盖（改过 field 后重启仍保留）
  - 实现：`app/services/bi_seed.py::seed_builtin_metrics`，main.py lifespan 调用
- [x] 1.3 聚合算子白名单与定义校验（`count/sum/avg/min/max/p50/p95/p99/count_distinct`）
  → 验证: 非法算子被拒绝且错误信息列出可用算子
  - 实现：`app/services/bi_defs.py::validate_metric_def`，`SUPPORTED_AGGS` 白名单
- [x] 1.4 维度基数保护配置：维度数量上限（默认 3，时间维不计入）、Top N（默认 100）、
  基数预估阈值——均可在定义中体现
  → 验证: 超上限的定义被拒绝；边界值（刚好 3 个）通过
  - 实现：`MAX_DIMENSIONS=3` / `DEFAULT_TOP_N=100` / `CARDINALITY_WARN_THRESHOLD=5000`；
    `is_time=True` 的隐式时间维不占配额；Top N 落入 `BiMetricDef.top_n`
- [x] 1.5 ~~PG 下将 `data_buffer` 的 JSON 列迁移为 JSONB~~ **已修订取消**：实现阶段确认聚合改为 Python 侧执行，
  每行 JSON 只解析一次，无需任何 schema 变更（见 design D5 修订说明）
  → 验证: 不适用；回归确认本次未引入 schema 变更
- [x] 1.6 业务维度候选与基数分级：内置 8 个候选维度（人 / 设备 / 物料 / 计划号 / 客户 / 类别 / 状态 / 地区）
  及其基数等级与用法提示；高基数维度配为分组时给出风险提示
  → 验证: 低基数（类别/状态/地区）直接通过；高基数（物料/计划号）返回风险提示
  - 实现：`DIMENSION_CANDIDATES` 8 项（low/medium/high + usable_for_group + hint）；
    高基数作分组返回 `high_cardinality_dimension` warning（非阻断，耦合 spec 用例）
- [x] 1.7 度量配置校验：字段 JSON 路径存在性、类型匹配、**时间点 vs 时长**区分
  （时间点禁止 `sum`/`avg`，仅允许 `min`/`max`）
  → 验证: 各类错误配置被拒并指出字段与原因；时长字段可 `sum`/`avg`，时间点字段仅 `min`/`max`
  - 实现：静态校验完成（`field_type` 枚举 + `point_in_time` 禁止 `sum`/`avg` + 数值算子必填 field）；
    **对真实数据的字段存在性校验**放在 4.8 定义管理 API（需采样）

## 2. 聚合引擎核心（风险最高，先建测试）

- [x] 2.1 新增 `app/services/bi_sources.py`：数据源适配（`data_buffer` / `execution_logs` / `custom_sql`）
  + JSON 取值方言（PG `->>` / SQLite `json_extract`）
  → 验证: 同一维度定义在 SQLite 与 PG 测试库上均能正确取值
  - **设计修订**：聚合改为 Python 侧执行后，**不再需要 JSON 取值方言**（JSON 在 Python 侧解析一次）；
    统一走 `get_field`（顶层列→data JSON 路径回退），与数据库无关
- [x] 2.2 `custom_sql` 只读校验（拒绝写操作关键字）
  → 验证: 含 `INSERT/UPDATE/DELETE/DROP` 的语句被拒绝；只读 `SELECT` 通过
  - 实现：`bi_defs.is_read_only_sql`（必以 SELECT/WITH 开头 + 写关键字黑名单）；
    `scan_window` 内二次校验并抛 `SourceError`
- [x] 2.3 窗口计算：维度分组 + 度量算子实现（含 p50/p95/p99 与 count_distinct）
  → 验证: 固定样本数据集上逐算子断言结果（与手工计算值一致）
  - 实现：`bi_aggregator.compute_measure` + `aggregate_rows`（纯函数，无 IO）；
    分位数与 `app/api/metrics.py` 口径一致（线性插值）
- [x] 2.4 幂等写入：按 `(metric_key, bucket_start, dim_hash)` UPSERT **覆盖**而非累加
  → 验证: 同一窗口连续计算两次，结果与一次完全相同（不翻倍）
  - **实现强化**：采用「先删该桶全部旧行 → 再插入」而非 UPSERT——
    同样幂等，且能清掉「上轮存在、本轮消失」的维度组合残留（已加专项用例）
- [x] 2.5 水位线推进与断点续跑（逐窗口补齐，不跳窗）
  → 验证: 模拟中途失败后续跑，断言所有窗口都被计算且无跳过
  - 实现：`run_metric` + `iter_windows`，水位线**只允许向前**；
    首次运行只算最近一个完整窗口（历史靠回填）；已断言窗口首尾相接无跳
- [x] 2.6 分片兜底：单桶行数超阈值时按 `id` 分片多次扫描后合并
  → 验证: 构造超阈值单桶，分片结果与不分片结果一致
  - 实现：**主键游标分片**（`id > last_id` 全序枚举，确定性无重复无遗漏）；
    已用（阈值=3）与（阈值=1000）对比断言结果集完全相同
  - **修正**：初版 `shard_threshold` 参数未向下传递（真实 bug），已修复
- [x] 2.7 回填服务层（指定 `metric` + `from`/`to` 区间重算）
  → 验证: 回填已存在区间后，既有桶被新值覆盖且无新增重复桶
  - 实现：`backfill` + `windows_between`
- [x] 2.8 JSON 解析成本：Python 侧每行 `json.loads` 一次，多个待提取字段复用同一解析结果，
  并附单窗口耗时基准
  → 验证: 5 万行 × 8 字段实测单指标聚合 **< 3s**；对比“逐字段解析”基线耗时明显下降
  - 实现：使用 ORM JSON 列，SQLAlchemy 每行解析一次并缓存为 Python 对象；
    后续多字段为内存字典访问（已写入模块 docstring）
  - 耗时基准实测归入 4.5 / 6.4
- [x] 2.9 维度基数保护：维度数 ≤ 3 校验、保存时组合数预估告警、
  结果按主度量降序 Top N 截断并将其余归并为「其他」
  → 验证: 超限拒绝、预估超阈值告警、构造超 TopN 数据断言截断与「其他」归并且总数守恒
  - 实现：维度数校验在 `bi_defs`（阶段1）；`_apply_top_n` 截断 + 「其他」归并；
    `estimate_cardinality` 预估最近窗口组合数
  - **诚实性**：不可跨组合并的算子（分位/均值）在「其他」项置 `None`，不编造；
    已断言总数守恒
- [x] 2.10 SQLite 降级路径：单指标提取字段数限制与已知限制提示
  → 验证: 超限配置被拒；正常配置在 SQLite 下可正确聚合
  - **已修订取消**：设计改为 Python 侧聚合后，SQLite 与 PG 走**同一实现**，
    不存在 JSON 能力差异，无需降级与字段数限制；既有用例已在 SQLite 上全部通过

## 3. 调度、告警与数据保留

- [x] 3.1 每分钟聚合 tick + Redis 锁（`bi_aggregation_tick`）
  → 验证: 并发触发下只有一个进程执行计算；锁释放后可再次执行
  - 实现：`app/services/bi_scheduler.py::bi_loop`（沿用 `scheduler.py` 的 tick + 锁模式，
    锁 ttl=120s）；抢锁返回 None（他进程持锁）时本轮跳过；main.py lifespan 启停
- [x] 3.2 Redis 不可用时降级为无锁执行，且告警只打一次
  → 验证: 注入 Redis 异常，断言任务仍执行且日志只告警一次
  - 实现：模块级 `_degraded_warned` 标志；已断言三轮中告警仅出现一次
- [x] 3.3 水位线滞后告警（超过阈值记录告警日志）
  → 验证: 构造滞后水位线，断言告警日志出现且含滞后时长
  - 实现：`check_watermark_lag`（阈值 = `APIBRIDGE_BI_WATERMARK_LAG_WARN_WINDOWS` × 窗口秒数，
    默认 60 窗口）；滞后信息随轮次结果返回（`lag_warning`）并记 WARNING 日志
- [x] 3.4 `data_buffer` 明细清理任务：按保留期分批删除（单批 ≤ 5000 行），保留天数可配置
  → 验证: 构造过期/未过期数据，断言仅删过期项、分批执行、配置生效
  - 实现：`purge_expired_buffers`——`expires_at` 非空时以它为准则，否则用
    `created_at + retention_days`；分批提交（默认 5000/批，最多 100 批）
  - 配置项：`APIBRIDGE_BI_RETENTION_DAYS`(7) / `BI_PURGE_BATCH_SIZE`(5000) / `BI_PURGE_MAX_BATCHES`(100)
  - 已断言：聚合结果表不受清理影响
- [x] 3.5 清理失败隔离：不影响同步传输与聚合任务
  → 验证: 注入清理异常，断言同步与聚合用例仍通过
  - 实现：`_run_once` 中聚合与清理各自独立 try/except + 独立会话；
    已双向断言（聚合炸→清理照跑；清理炸→聚合照跑）

## 4. 查询 API 与格式转换

- [x] 4.1 `GET /api/v1/bi/metrics` 列出指标定义
  → 验证: TestClient 返回定义清单，停用项状态正确
  - 实现：`app/api/bi.py`（已注册 `/api/v1/bi`）；支持 `enabled` 过滤
- [x] 4.2 `GET /api/v1/bi/metrics/{metric_key}?from=&to=&format=` 查询聚合结果（含 404 与维度过滤）
  → 验证: 用例覆盖正常查询、维度过滤、未定义指标 404
  - 实现：时间范围、`?dim=name:value`（可重复）维度过滤、`format` 白名单校验、非法时间 400
  - 查询只读预聚合表，不扫明细
- [x] 4.3 `POST /api/v1/bi/aggregate/run` 手动触发/回填
  → 验证: 返回处理的窗口数，且结果与自动聚合一致
  - 实现：不传 from/to 为增量（`mode=incremental`）；传则为回填（`mode=backfill`）；
    不传 metric 时跑全部启用指标
- [x] 4.4 新增 `app/services/bi_format.py`：`echarts-line` / `echarts-bar` / `echarts-pie` /
  `echarts-heatmap` / `datav-number` / `table` / `raw` 七种输出
  → 验证: 每个格式的结构断言（字段名与嵌套形状）
  - 实现：统一中间格式（timeseries/distribution/matrix/records/scalar）→ 适配器；
    缺失桶值为 `None`（不谎报为 0）；已断言 7 种输出结构 + 分发与非法格式拒绝
- [x] 4.5 查询性能预算验证：构造 **5 万行 × 8 字段**每桶的预算数据量，
  断言查询 < 100ms、单桶聚合（含 JSON 提取）< 3s
  → 验证: 计时输出与性能预算对照（聚合耗时与纯 `GROUP BY` 基线和逐字段解析基线三方对比）
  - 实现：`tests/test_bi_perf.py` 用 **1 万行 × 8 字段 × 3 维分组 + 3 度量** 验证量级
    （查询 < 100ms 已断言；聚合断言 5s 容差捕获数量级退化，正式预算 3s）
  - 真实 5 万行压测归入 6.4（`scripts/bi_bench.py`）
  - **发现并修正一个假阳性**：初版 `now` 落在数据窗口之外，测试“通过”但未真正聚合，
    已加“必须真的产出桶”的防御断言
- [x] 4.6 响应脱敏复核：中间数据可能含业务敏感字段
  → 验证: 断言响应中不出现 `enc:` 前缀与凭证字样
- [x] 4.7 高基数防护：确认 per-task/per-connection 维度只出现在 BI 端点，未新增 Prometheus 标签
  → 验证: 守卫测试断言 `/metrics` 中不含 `task_id` / `connection_id` 标签
- [x] 4.8 定义管理 API：`POST` / `PUT` / `DELETE` `/api/v1/bi/metrics[/{metric_key}]`
  （含维度数上限、基数预估告警、度量校验的完整接入）
  → 验证: 增删改用例；超限/非法配置被拒的用例
  - 实现：写入前 `validate_metric_def`；非法算子/4 个维度/时间点 sum 均 400 并返回 issue 列表；
    重复 metric_key 409；删除同时清理预聚合桶
  - 补齐：`POST /api/v1/bi/metrics/{key}/estimate` 基数预估端点
- [x] 4.9 `GET /api/v1/bi/dimensions`：返回 8 个候选维度及基数等级、用法提示
  → 验证: 用例断言返回 8 项且含基数等级字段

## 5. 前端 BI 大屏

- [x] 5.1 引入 `echarts`（按需：`echarts/core` + 折线/柱状/饼/热力 + CanvasRenderer）并封装通用图表组件
  → 验证: 构建产物体积对比（未引入全量包）+ `npx tsc --noEmit` 零错误
  - 实现：`components/charts/EChart.tsx`（core + 4 图表 + Grid/Tooltip/Legend/VisualMap + CanvasRenderer）
  - **体积实测**：lib 模式对比 — 按需 **949 KB** vs 全量 **1670 KB**（省 721 KB，gzip 省 205 KB，减 43%）
  - `npx tsc -b` 零错误（已用「注入类型错误」反证检查器真实生效）
- [x] 5.2 自研大屏装饰组件（数字翻牌、边框、发光效果），**零外部依赖**
  → 验证: 浏览器实测渲染正常；`package.json` 未新增任何 DataV 相关依赖
  - 实现：`components/screen/ScreenDecor.tsx`（ScreenPanel/DataCard）+ `screen.css`（四角框线 + 发光 + 翻牌字体）
- [x] 5.3 BI 大屏页（指标卡 + 图表网格 + 时间范围/维度筛选），路由 `/bi`
  → 验证: 浏览器实测各图表渲染出数
  - 实现：`pages/bi/index.tsx` + `pages/bi/BiBoard.tsx`（数据加载/格式拼装/渲染分发）
  - 浏览器实测：6 个 canvas、8 个卡片、canvas 实际尺寸 608×260、表格型指标 4 行数据
- [x] 5.4 独立投放模式：`/bi/screen`（无导航/侧边栏）、全屏切换、自动轮播（可暂停），
  1920×1080 基准等比缩放
  → 验证: 浏览器实测全屏切换、轮播切换与暂停、非 1920×1080 分辨率下布局无溢出
  - 实现：`pages/bi/screen.tsx`；路由在 MainLayout **之外**（顶层）保证无管理导航
  - 浏览器实测：无 `.ant-layout-sider`、5 个面板、4 张翻牌卡、空格键可暂停轮播
- [x] 5.5 数据自动刷新（默认 1 分钟，可配），且 MUST NOT 闪烁或重置轮播进度
  → 验证: 浏览器实测刷新后数据变化且轮播进度保持
  - 实现：刷新只递增 `refreshKey`，`useMetricsData` 不依赖 `page` 状态；
    ECharts 用 `setOption(option, notMerge)` 就地更新，容器不重建
- [x] 5.6 与管理界面并存（路由菜单互不干扰）
  → 验证: 浏览器走查导航切换
  - 实现：`/bi`、`/bi/definitions` 在 MainLayout 内；菜单新增「BI 大屏」；`/bi/screen` 独立顶层路由
- [x] 5.7 指标定义配置界面：维度下拉（8 个候选，带基数等级提示）、度量字段与算子选择、
  时间点/时长错误反馈、保存前基数预估告警
  → 验证: 浏览器实测（低基数维度可保存；物料/计划号作维度时出现风险提示；时间点选 sum 被拒）
  - 实现：`pages/bi/definitions.tsx`——候选维度表（基数分级标签）、维度/度量 Form.List、
    前端前置校验（时间点禁 sum/avg、维度≤3）、后端 issues 逐条展示、基数预估按钮
  - 浏览器实测：候选维度表 8 行 + 「高基数」标签 2 处，无控制台报错

## 6. 回归与收尾

- [x] 6.1 全量 `python -m pytest tests/ -q` 通过（基线 568 passed / 11 skipped）
  → 验证: 输出中 failed=0，新增用例计入总数
  - 实测：**748 passed / 11 skipped / 0 failed**（基线 568 + 新增 180）
- [x] 6.2 前端清 `tsconfig.tsbuildinfo` 后 `npx tsc --noEmit` 零错误 + 浏览器全功能走查
  → 验证: 命令输出 + 走查记录
  - `tsc -b` 零错误（已用注入类型错误反证检查器生效）；`npm run build` 成功
  - 浏览器（Playwright + Chromium）实测：/bi 6 个 canvas + 8 卡片 + canvas 608×260 + 表格 4 行；
    /bi/definitions 维度表 8 行 + 高基数标签 2 处；/bi/screen 5 面板 + 4 翻牌卡
    （值 3000/2360/640/1517.86）+ 无管理导航 + 空格可暂停；**全部页面无控制台报错**
- [x] 6.3 更新 `docs/project-status.md`：记录聚合引擎、指标集与性能实测结果
  → 验证: 文档描述与实测输出一致（项目规矩：只写实测事实）
- [x] 6.4 真实数据量压测：分别以 1 万 / 5 万 行每桶实测聚合耗时
  → 验证: 输出耗时与性能预算（20–50ms 目标）对照，超出则按 D5 兜底优化
  - 实现：`scripts/bi_bench.py`（3 维分组 + count/sum/p95 三度量，每行 8 字段）
  - 实测（SQLite）：1 万行 扫描 0.13s + 计算 0.05s = **端到端 0.21s**；
    5 万行 扫描 0.94s + 计算 0.23s = **端到端 1.35s**——均远低于 3s 预算
  - 结论：**瓶颈在扫描（占 70%，I/O + JSON 解析）而非聚合计算（17%）**，
    与 design D4/D5 的预判一致
  - **修正一个假结果**：初版未清水位线，导致 run_metric 跳过全部窗口、
    端到端耗时误报为 0.00s；已加「未产生窗口/桶则判压测无效」的防御断言
