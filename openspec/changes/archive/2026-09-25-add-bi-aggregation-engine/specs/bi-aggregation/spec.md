## ADDED Requirements

### Requirement: 聚合定义配置化

系统 SHALL 以数据配置（而非代码）描述每一组聚合指标，并支持增删改与启用/停用。

#### Scenario: 定义包含必要要素

- **WHEN** 管理员创建一条聚合定义
- **THEN** MUST 指定 `metric_key`（唯一）、数据源、时间字段、时间粒度、维度、度量、图表类型
- **AND** 使用未支持的聚合算子时 MUST 被拒绝，并在错误中列出可用算子

#### Scenario: 停用定义后不再计算

- **WHEN** 某条定义 `enabled=false`
- **THEN** 聚合任务 SHALL NOT 为其产生新的时间桶

### Requirement: 每分钟增量窗口聚合

系统 SHALL 以固定时间窗口（默认 1 分钟）增量计算聚合结果，且单次计算成本 SHALL NOT 随历史数据量增长。

#### Scenario: 只计算待处理窗口

- **WHEN** 聚合 tick 执行
- **THEN** MUST 只扫描水位线之后、且已完整结束的窗口
- **AND** SHALL NOT 重新扫描已计算过的窗口（回填场景除外）

#### Scenario: 结果写入预聚合表

- **WHEN** 某窗口计算完成
- **THEN** MUST 按 `(metric_key, bucket_start, dim_hash)` 将结果写入 `bi_metric_buckets`

### Requirement: 聚合幂等（窗口覆盖而非累加）

系统 SHALL 保证同一窗口被重复计算时产生相同结果。

#### Scenario: 重复计算不翻倍

- **WHEN** 同一窗口被连续计算两次
- **THEN** 结果 MUST 与仅计算一次相同
- **AND** MUST NOT 出现计数或求和值翻倍

#### Scenario: 回填覆盖既有桶

- **WHEN** 对已存在时间桶的窗口执行回填
- **THEN** 既有桶 MUST 被新计算结果覆盖

### Requirement: 水位线与断点续跑

系统 SHALL 为每个指标维护聚合水位线，支持失败后断点续跑与指定区间回填。

#### Scenario: 失败后续跑

- **WHEN** 聚合任务中断后重新启动
- **THEN** MUST 从水位线继续，逐窗口补齐
- **AND** 补齐过程中 SHALL NOT 跳过任何窗口

#### Scenario: 指定区间回填

- **WHEN** 客户端 POST `/api/v1/bi/aggregate/run` 并指定 `metric`、`from`、`to`
- **THEN** MUST 重算该区间，并返回处理的窗口数

#### Scenario: 水位线滞后告警

- **WHEN** 水位线落后当前时间超过配置阈值
- **THEN** 系统 MUST 记录告警日志，并在有前端入口时可见

### Requirement: 数据源与 JSON 方言适配

系统 SHALL 支持 `data_buffer`、`execution_logs`、自定义 SQL 三类数据源，并 MUST 正确处理 PostgreSQL 与 SQLite 的 JSON 取值差异。

#### Scenario: 两种数据库都能提取 JSON 维度

- **WHEN** 同一聚合定义分别在 PostgreSQL 与 SQLite 上运行
- **THEN** MUST 均能正确提取配置的 JSON 路径值
- **AND** SHALL NOT 因 SQL 方言差异报错

#### Scenario: 自定义 SQL 只读校验

- **WHEN** 数据源为 `custom_sql`
- **THEN** 系统 MUST 拒绝非只读语句（含写操作关键字）

### Requirement: 业务维度与度量由用户配置

系统 SHALL 将业务类指标的维度与度量交由用户配置，MUST NOT 预设业务字段作为开箱度量。

#### Scenario: 度量字段由用户指定

- **WHEN** 管理员配置业务指标的度量
- **THEN** MUST 由用户指定字段（JSON 路径）与算子
- **AND** 系统 SHALL NOT 预设任何业务字段作为开箱可用度量

#### Scenario: 时间型字段区分时间点与时长

- **WHEN** 用户为某度量选择时间型字段
- **THEN** 系统 MUST 区分"时间点"与"时长"
- **AND** 时间点字段 SHALL NOT 允许 `sum`/`avg` 算子，仅允许 `min`/`max`

#### Scenario: 度量配置在建定义时校验

- **WHEN** 用户保存含不存在字段、类型不匹配或时间点误用 `sum` 的度量配置
- **THEN** MUST 拒绝保存，并指出问题字段与原因

#### Scenario: 高基数维度给出用法提示

- **WHEN** 用户将高基数维度（如物料、计划号）配置为分组维度
- **THEN** 系统 MUST 提示基数风险，并建议改用 Top N 排行

#### Scenario: 低基数维度直接可用

- **WHEN** 用户以类别/状态/地区等低基数维度配置分组
- **THEN** MUST 保存通过，无额外确认负担

### Requirement: 聚合维度基数保护

系统 SHALL 限制单个指标的维度组合规模，防止多维分组导致结果基数爆炸。

#### Scenario: 维度数量硬限制

- **WHEN** 管理员定义的分组维度数量超过上限（时间维不计入）
- **THEN** MUST 拒绝保存，并在错误中说明上限值

#### Scenario: 高基数维度需显式确认

- **WHEN** 保存定义时预估的维度组合数超过阈值
- **THEN** 系统 MUST 给出警告，并要求显式确认后才能保存

#### Scenario: Top N 截断保证单桶行数有界

- **WHEN** 某窗口的分组结果项数超过 Top N（默认 100）
- **THEN** MUST 仅保留主度量降序的前 N 项
- **AND** 其余项 MUST 合并为单项"其他"，并在结果中标记

### Requirement: 聚合执行位置与 JSON 解析成本

系统 SHALL 在应用侧（Python）完成分组与度量计算，并 SHALL 保证每行 JSON 只解析一次。

#### Scenario: 每行 JSON 只解析一次

- **WHEN** 聚合数据源为含 JSON 的 `data_buffer`
- **THEN** 系统 MUST 对每行 JSON 只做一次解析
- **AND** 多个待提取字段 MUST 复用同一解析结果，SHALL NOT 逐字段重复解析

#### Scenario: 分位数与过滤度量跨库一致

- **WHEN** 同一数据分别在 PostgreSQL 与 SQLite 上聚合，且指标含分位数与过滤度量
- **THEN** 两种数据库的结果 MUST 一致（同一实现，无方言分叉）

#### Scenario: 单窗口聚合耗时预算

- **WHEN** 单窗口含 5 万行、每行 8 个待提取字段
- **THEN** 单指标聚合耗时 SHALL 小于 3 秒

#### Scenario: 超阈值自动分片

- **WHEN** 单窗口行数超过配置阈值
- **THEN** MUST 按主键分片多次扫描后合并结果
- **AND** 分片结果 MUST 与不分片时一致

### Requirement: 预聚合结果查询

系统 SHALL 提供按指标与时间范围查询预聚合结果的接口，查询 MUST 命中预聚合表而非扫描明细。

#### Scenario: 查询时间范围

- **WHEN** 客户端 GET `/api/v1/bi/metrics/{metric_key}?from=&to=`
- **THEN** 返回该区间内的时间桶与度量值
- **AND** 在性能预算数据量下响应时间 SHALL 小于 100ms

#### Scenario: 未定义指标

- **WHEN** 请求的 `metric_key` 不存在
- **THEN** MUST 返回 404

#### Scenario: 按维度过滤

- **WHEN** 请求携带维度过滤参数
- **THEN** 返回结果 MUST 仅包含匹配的维度组合

### Requirement: 图表格式转换契约

系统 SHALL 提供格式转换层，把统一中间格式转换为前端图表可直接渲染的结构。

#### Scenario: ECharts 折线/柱状格式

- **WHEN** 请求 `format=echarts-line` 或 `echarts-bar`
- **THEN** MUST 返回含 `xAxis.data` 与 `series[{name,type,data}]` 的结构

#### Scenario: 饼图格式

- **WHEN** 请求 `format=echarts-pie`
- **THEN** MUST 返回 `[{name, value}]` 数组

#### Scenario: 大屏数字格式

- **WHEN** 请求 `format=datav-number`
- **THEN** MUST 返回含 `value` 与 `unit` 的结构

#### Scenario: 原始中间格式

- **WHEN** 请求 `format=raw`
- **THEN** MUST 返回统一中间格式（`dimensions` / `x` / `series`）

### Requirement: 内置指标集

系统 SHALL 在首次启动时写入内置聚合指标定义，覆盖同步吞吐、延迟分位、错误聚合、任务与连接健康、数据流向等运维维度。

#### Scenario: 首次启动写入种子定义

- **WHEN** 系统首次启动且聚合定义表为空
- **THEN** MUST 写入内置定义
- **AND** 重复启动 SHALL NOT 重复写入或覆盖用户修改

#### Scenario: 业务维度指标默认不启用

- **WHEN** 业务类指标（依赖 JSON 路径配置）缺少配置
- **THEN** MUST 保持停用状态，且 SHALL NOT 产生空桶

### Requirement: BI 大屏展示

系统 SHALL 提供独立路由的 BI 大屏页面，支持时间范围筛选与指标卡/图表网格布局。

#### Scenario: 大屏加载指标

- **WHEN** 用户打开 BI 大屏
- **THEN** MUST 按指标定义渲染对应图表
- **AND** 数据 SHALL 按配置周期自动刷新（默认 1 分钟）

#### Scenario: 与管理界面并存

- **WHEN** 用户在 BI 大屏与管理界面之间切换
- **THEN** 路由 MUST 互不干扰，管理界面功能不受影响

#### Scenario: 独立投放模式

- **WHEN** 用户访问独立投放路由（`/bi/screen`）
- **THEN** MUST 以无导航栏、无侧边栏的全屏布局展示
- **AND** MUST 提供全屏切换入口

#### Scenario: 自动轮播

- **WHEN** 独立投放模式开启自动轮播
- **THEN** MUST 按配置周期切换指标分组或页面
- **AND** 用户 MUST 能暂停/继续轮播

#### Scenario: 大屏分辨率自适应

- **WHEN** 在非基准分辨率（非 1920×1080）的大屏上打开投放页
- **THEN** 布局 MUST 等比缩放适配，不出现溢出或错位

#### Scenario: 刷新不打断投放

- **WHEN** 定时刷新数据
- **THEN** MUST NOT 造成页面闪烁或重置轮播进度
