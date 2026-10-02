# bi-aggregation Specification

## Purpose
TBD - created by archiving change add-bi-aggregation-engine. Update Purpose after archive.
## Requirements
### Requirement: 聚合定义配置化

系统 SHALL 以数据配置（而非代码）描述每一组聚合指标，并支持增删改与启用/停用。

#### Scenario: 定义包含必要要素

- **WHEN** 管理员创建一条聚合定义
- **THEN** MUST 指定 `metric_key`（唯一）、数据源、时间字段、时间粒度、维度、度量、图表类型
- **AND** 使用未支持的聚合算子时 MUST 被拒绝，并在错误中列出可用算子
- **AND** 度量算子 SHALL 含 `ratio`（聚合比：`numerator`/`denominator` 各含 `{field, agg}`，可选 `scale`）；
  ratio 缺少分子/分母 MUST 被拒绝

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

系统 SHALL 支持 `form`、`data_buffer`、`execution_logs`、自定义 SQL 四类数据源，
并 MUST 正确处理 PostgreSQL 与 SQLite 的 JSON 取值差异。

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

#### Scenario: 展示层修饰

- **WHEN** 定义声明 `display.percent`（占比）
- **THEN** series 结构数值 MUST 按各系列总和换算为百分比（保留 1 位）
- **AND** 定义声明 `display.cumulative`（累计）时 MUST 按 x 顺序累加；
  快照源（form）声明累计 MUST 在保存时被拒绝（每轮同一状态，累计无意义）

### Requirement: 派生计算字段（行级）

系统 SHALL 支持在指标定义中声明行级派生列（computed），对被拉取的每一行求值生成新列，
供维度、度量与筛选引用。

#### Scenario: 表达式引用与求值

- **WHEN** 用户定义派生列（如 `[合格数] / [检验数] * 100`）
- **THEN** 系统 MUST 以白名单 AST 方式解析并逐行求值（禁止直接执行任意代码）
- **AND** 表达式引用 SHALL 以 `[字段名]` 书写，保存时 MUST 按表单字段元数据
  （中文显示名优先、字段键回落）规范化；引用不存在 MUST 报 400
- **AND** 可用函数白名单 SHALL 含数值（round/abs/ceil/floor/min/max/coalesce）、
  字符串（substring/upper/lower/trim/concat）与日期（date_part）三类

#### Scenario: 循环引用防护

- **WHEN** 派生列之间形成循环引用（A→B→A）
- **THEN** 定义保存 MUST 拒绝并给出环路径

#### Scenario: 行级容错

- **WHEN** 某行引用字段缺失或不可转数
- **THEN** 该行派生值 MUST 为 null（不参与计数/求和，不编造 0）

### Requirement: 维度值自定义分组

系统 SHALL 支持维度的值→组映射（groups + other）在聚合前归一。

#### Scenario: 分组归一

- **WHEN** 维度声明分组映射（如 A线/B线→一车间、C线→二车间，other=其他）
- **THEN** 聚合 MUST 按组名分组；未命中的值 MUST 归入 other（未声明 other 时保留原值）

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

系统 SHALL 提供独立路由的 BI 大屏页面，支持多看板、时间范围筛选与自由画布布局。

#### Scenario: 大屏加载指标

- **WHEN** 用户打开 BI 大屏（指定或默认看板）
- **THEN** MUST 按该看板 cells 摆放渲染对应卡片
- **AND** 数据 SHALL 按配置周期自动刷新（默认 1 分钟）

#### Scenario: 与管理界面并存

- **WHEN** 用户在 BI 大屏与管理界面之间切换
- **THEN** 路由 MUST 互不干扰，管理界面功能不受影响

#### Scenario: 独立投放模式

- **WHEN** 用户访问独立投放路由（`/bi/screen?board=<key>`）
- **THEN** MUST 以无导航栏、无侧边栏的全屏布局展示
- **AND** MUST 提供全屏切换入口

#### Scenario: 自由画布

- **WHEN** 用户在管理页布局编辑（拖动/缩放/卡片库添加/卡片移出）
- **THEN** 格位（cells）MUST 即改即存服务端，布局与图表定义分离存储
- **AND** 投放页 MUST 采用同坐标解释（画布双解释）

#### Scenario: 大屏分辨率自适应

- **WHEN** 在非基准分辨率的大屏上打开投放页
- **THEN** 布局 MUST 按基准分辨率与适配模式缩放适配，不出现溢出或错位
- **AND** 播放设备比例与画布一致时各适配模式视觉等价

#### Scenario: 刷新不打断投放

- **WHEN** 定时刷新数据
- **THEN** MUST NOT 造成页面闪烁或重置画布

### Requirement: BI 多看板

系统 SHALL 支持多块相互独立的 BI 画布（看板），看板归属语义为「摆放即归属」。

#### Scenario: 看板增删改查

- **WHEN** 用户新建看板（可指定名称）
- **THEN** MUST 创建独立画布设置（空 cells），并出现在看板列表
- **AND** 看板 key SHALL 仅允许字母/数字/下划线/中划线（1-50 字符）；
  重复 key MUST 返回 409；保留名 `default` MUST 拒绝创建与删除
- **AND** 删除看板 SHALL 仅删除画布摆放，指标定义不受影响

#### Scenario: 看板隔离

- **WHEN** 在不同看板间切换
- **THEN** 各看板的 cells 布局、筛选元素、显示设置 MUST 相互隔离
- **AND** `default` 为缺省看板：读写不带 board 参数时 MUST 与其等价（兼容旧行为）

#### Scenario: 摆放即归属

- **WHEN** 渲染某看板
- **THEN** MUST 只渲染该看板 cells 中摆放了的指标（未摆放不上屏）
- **AND** 同一指标定义 MAY 同时摆放在多个看板，定义只存一份

#### Scenario: 投放独立地址

- **WHEN** 访问投放路由并携带 `board` 参数
- **THEN** MUST 渲染该看板的画布，大屏标题 SHALL 为看板名

### Requirement: 投放显示设置

系统 SHALL 支持看板级投放显示设置：基准分辨率、适配模式与主题底色（DataEase screenAdaptor 范式）。

#### Scenario: 基准分辨率

- **WHEN** 看板配置基准分辨率（base_w × base_h，预设 16:9/带状屏/4K 或自定义）
- **THEN** 投放页 MUST 以该分辨率为坐标基准渲染画布（72×36 格位按其等比解释），
  并整体适配播放设备视口

#### Scenario: 适配模式

- **WHEN** 播放设备视口比例与画布比例不一致
- **THEN** 适配模式 MUST 生效：`keep` 等比缩放居中（默认）、`widthFirst` 宽度撑满
  （高度可滚动）、`heightFirst` 高度撑满（宽度可滚动）、`full` 拉伸填满（不保持比例）
- **AND** 播放设备与画布同比例时各模式视觉等价（数学必然，UI MUST 给出说明）

#### Scenario: 主题底色

- **WHEN** 看板主题设为深色或浅色
- **THEN** 投放页底色、面板与图表配色 MUST 整体跟随（白底用浅色表格实现）
- **AND** 非全屏与全屏底色 MUST 一致

#### Scenario: 格位映射内容区

- **WHEN** 投放页渲染标题栏与筛选元素
- **THEN** 72×36 格位 MUST 映射到内容区（标题/筛选之下）实测尺寸，
  摆放在画布底部（y+h 触底）的卡片 MUST 完整可见、不被裁切

#### Scenario: 设置免刷新生效

- **WHEN** 管理页修改看板显示设置（模式/主题/基准/筛选/名称）
- **THEN** 已打开的投放页 MUST 在轮询周期（≤30s）内自动跟随生效，
  无需人工刷新

### Requirement: 明细表滚动

系统 SHALL 支持明细表在投放态自动轮播滚动。

#### Scenario: 自动轮播

- **WHEN** 投放页明细表内容溢出卡片高度
- **THEN** MUST 以恒定速度自动向上滚动，到底部停留后回到顶部循环
- **AND** 鼠标悬停 MUST 暂停滚动，移开后恢复

#### Scenario: 深色与浅色主题一致

- **WHEN** 明细表以任一主题渲染
- **THEN** MUST 使用同一滚动行为（卡内滚动、表头吸顶），不因主题差异丢失行或行为不同

#### Scenario: 管理页不滚动

- **WHEN** 在管理页查看明细卡
- **THEN** MUST NOT 自动滚动（便于编辑与查看）

### Requirement: 表单数据源

系统 SHALL 支持 `form` 数据源：以「连接 + 表单」为聚合单位，经平台同源读行路径
（HAP 工作表 / DB 表）拉取真实业务数据行。**HAP 源在表规模较大时 SHALL 走增量拉取**，
以「增量 + 定期全量对账」代替每轮整表重拉。

#### Scenario: 定义指向连接表单

- **WHEN** 管理员创建 `source_type=form` 的聚合定义
- **THEN** `source_ref` MUST 为含非空 `connection_id` 与 `form_key` 的 JSON
- **AND** 结构不合法时 MUST 被校验拒绝

#### Scenario: 拉取行数据走同源路径

- **WHEN** 聚合或明细查询需要表单行数据
- **THEN** HAP 连接 MUST 经 `ProtocolAdapterFactory` 构造适配器读取（与运行时同一条传输路径）
- **AND** DB 连接 MUST 经数据库连接器读取（凭证在其解密路径内流转，不进日志与响应）

#### Scenario: 行键与字段端点一致

- **WHEN** 维度或度量引用某字段
- **THEN** 字段标识 MUST 与字段端点 `path`（HAP 为 alias||controlId）一致

#### Scenario: 拉取行数有硬上限

- **WHEN** 单次拉取行数超过上限（10 万行）
- **THEN** 系统 MUST 截断并记录告警日志

#### Scenario: HAP 增量拉取

- **WHEN** HAP 工作表行数超过启用阈值（默认 2 万行）且已有水位线
- **THEN** 系统 MUST 只拉取「最近变化」的行（服务端按 `utime` 严格大于
  `水位线 − 重叠窗口` 过滤并降序返回），按行标识**幂等合并**进快照
- **AND** 合并后的快照 MUST 与全量拉取结果逐行一致
- **AND** 增量行数超过单轮上限或服务端 `total` 异常增大时 MUST 回退全量拉取

#### Scenario: 定期全量对账

- **WHEN** 距上次全量拉取超过对账间隔（默认 600 秒）
- **THEN** 系统 MUST 执行一次全量拉取以消除被删除/漏采的行
- **AND** 系统 SHALL 在读取响应中暴露当前模式、最近增量行数、水位线与上次全量时间，
  使「删除最迟何时消失」可被外部观测

#### Scenario: 增量模式的开关与降级

- **WHEN** 环境变量关闭增量、或工作表规模低于阈值、或行缺少行标识/更新时间字段
- **THEN** 系统 MUST 走全量拉取路径（行为与本变更前一致）

### Requirement: 快照聚合语义

`form` 数据源 SHALL 采用快照语义：每次计算取当前全量行，结果代表计算时刻的表单状态。

#### Scenario: 只计算最新完整窗口

- **WHEN** 聚合 tick 执行 form 源指标
- **THEN** MUST 只计算最新一个已完整结束的窗口，并把水位线直接前跳到该窗口
- **AND** 水位线已追平时 SHALL NOT 重复拉取与计算

#### Scenario: 不支持回填

- **WHEN** 对 form 源定义执行区间回填
- **THEN** 系统 MUST 拒绝并说明快照语义不支持回填

#### Scenario: 行缓存共享

- **WHEN** 同一表单在缓存有效期内被多个指标或明细查询使用
- **THEN** 系统 MUST 只向源拉取一次行数据

#### Scenario: 快照窗口（内存路径）

- **WHEN** form 源指标聚合（HAP 源或不可下推的 DB 定义）
- **THEN** MUST 取表单当前全量行、只算最新一个完整窗口，忽略 start/end 时间窗

#### Scenario: DB 源聚合下推

- **WHEN** DB 源定义满足可下推判定（维度/数值度量/结构化过滤均为可下推子集）
- **THEN** 系统 MUST 将「定义级筛选 → WHERE + 维度 → GROUP BY + 度量 → 聚合函数」
  下推为 SQL 执行，**MUST NOT** 拉取全量行
- **AND** 下推结果 MUST 与内存路径逐项一致（维度值 NULL→"未知"、数值 float 化、count 计数）

#### Scenario: 下推回退

- **WHEN** 定义含不可下推能力（分位算子/计算字段/自定义分组/resolver/文本模糊过滤/
  custom_sql/HAP/标识符非 ASCII/Oracle 默认）
- **THEN** 系统 MUST 回退内存路径并保持行为与现状一致
- **AND** 下推执行出现任何异常时 MUST 回退内存路径并缓存该定义的不可下推标记

#### Scenario: 下推执行点收敛且可关闭

- **WHEN** 平台执行聚合下推或内存回退
- **THEN** 源库 SQL MUST 只经单一执行入口 `app/connectors/grouped_pushdown.py`
  构造与执行（标识符白名单 + 全值绑定 + 表达式编译，不做字符串拼接）
- **AND** 连接器能力标志 `supports_group_pushdown=False`（Oracle 默认）或 db_type
  未知时 MUST 回退内存路径
- **AND** 可下推判定与失败标记 SHALL 进程内缓存（到达 TTL 后自愈），
  避免逐窗口重复判定与重复重试

#### Scenario: 下推数值与源库 SQL 逐项一致

- **WHEN** 对同一 DB 源定义分别取"下推聚合结果"与"源库 SQL 直查结果"
- **THEN** 分组维度值集合与各度量值 MUST 逐项一致（相对误差 ≤1e-9）
- **AND** 对照 MUST 覆盖分组 / 无维度汇总 / 定义级过滤三类形态

#### Scenario: 单源行数上限语义

- **WHEN** DB 源定义走聚合下推路径
- **THEN** MUST 不受 `MAX_FORM_ROWS`（10 万）约束（不下拉全量行）
- **AND** HAP 源与回退路径 MUST 维持 10 万硬上限与 5 万分档提示

#### Scenario: 并发隔离

- **WHEN** 聚合（下推或内存）与投放页查询同时进行
- **THEN** 内存聚合 MUST 在事件循环外执行（线程池）
- **AND** 在飞重查询 MUST 受信号量约束（默认 ≤4）；投放读桶查询 MUST NOT 受限

### Requirement: 即席分析查询

系统 SHALL 提供即席聚合查询接口 `POST /api/v1/bi/adhoc/query`：由「连接 + 表单 + 维度集 +
度量集 + 筛选」即时算出一份图表数据，**不落库**（仅"另存为卡片"才写定义）。

#### Scenario: 查询契约

- **WHEN** 管理员提交 `{connection_id, form_key, chart_type, dimensions[], measures[],
  filters[], computed[], top_n, sort, format}`
- **THEN** 系统 MUST 返回 `{engine, fallback_reason, row_count, elapsed_ms, defn, format, data}`
- **AND** `data` MUST 与 `GET /bi/metrics/{key}` 的输出同构（同一格式转换层）
- **AND** `defn` MUST 是可直接用于保存卡片定义的标准化定义

#### Scenario: 与看板同一套校验口径

- **WHEN** 即席请求含维度 >3、未知算子、按类型非法的筛选算子、ratio 结构不合法等
- **THEN** 系统 MUST 以 400 拒绝并返回与定义保存端点同结构的问题列表
- **AND** 合法即席定义 MUST 无需修改即可保存为卡片（不出现"能出图不能保存"）

#### Scenario: 引擎透出（不谎报）

- **WHEN** 即席查询取值
- **THEN** DB 源且满足可下推判定时 MUST 走聚合下推并标 `engine=pushdown`
- **AND** HAP 源 / 含 `computed` / 分位算子等不可下推情形 MUST 走内存路径并标
  `engine=memory` 与 `fallback_reason`
- **AND** 内存路径因 10 万行上限截断时 MUST 标 `truncated=true`（不得呈现为全表结果）

#### Scenario: 排序与 Top N

- **WHEN** 请求带 `sort={by, order}`（by 为度量 key 或维度名）
- **THEN** 结果 MUST 按该字段排序（稳定排序；缺值恒排末尾）
- **AND** `top_n` MUST 独立生效（截断 + 其余归并为「其他」），与排序互不覆盖

### Requirement: BI 访问授权与数据范围

系统 SHALL 支持**只读访问授权档案**：一份档案 = 一个只读令牌 + 一块可见范围
（看板白名单 / 行过滤 / 列隐藏）。BI 读端点 SHALL 接受管理令牌（全权）或档案令牌
（受限只读），且范围限制 MUST 在**服务端**生效——不得依赖前端隐藏。

#### Scenario: 档案令牌只读且默认拒绝

- **WHEN** 请求携带有效档案令牌访问 `/api/v1/bi/*`
- **THEN** 仅只读白名单端点 MUST 放行（`GET` 维度/字段/明细/指标/看板/投放设置、
  `POST /bi/adhoc/query`）
- **AND** 白名单之外的端点（含全部写操作）MUST 以 403 拒绝
- **AND** 令牌无效或档案已停用时 MUST 以 401 拒绝，且停用 MUST 立即生效（无缓存延迟）

#### Scenario: 看板范围

- **WHEN** 档案配置了看板白名单
- **THEN** 看板列表与投放设置 MUST 只返回白名单内的看板，其余看板 MUST 不可见（404）
- **AND** 白名单为空数组时 MUST 视为"不限看板"（管理员显式选择，接口与界面均如此表述）

#### Scenario: 行过滤（服务端强制）

- **WHEN** 档案配置了行过滤 `{字段: 值}`
- **THEN** 预聚合桶查询 MUST 只返回该字段等于授权值的桶
- **AND** 明细查询 MUST 在**分页之前**按行过滤，且响应 MUST 标注过滤降级
- **AND** 即席查询 MUST 把授权行过滤并入查询条件（同名字段以授权值为准，客户端无法放宽）
- **AND** 行过滤字段不是某卡片维度之一时，该卡片对档案 MUST 不可见（不得以未过滤结果返回）

#### Scenario: 列可见性

- **WHEN** 档案配置了隐藏字段
- **THEN** 响应（指标定义与取值、桶读、明细行、字段树）MUST 在服务端剥离这些字段
- **AND** 即席查询引用隐藏字段时 MUST 以 403 拒绝并说明字段名

#### Scenario: 不泄露存在性

- **WHEN** 档案请求范围外的看板或卡片
- **THEN** 系统 MUST 以 404 `not_visible` 拒绝，不得区分"不存在"与"无权限"
- **AND** 令牌明文 MUST 只在创建/轮换响应中出现一次，库内只存哈希

### Requirement: 表单字段元数据与逻辑类型

系统 SHALL 提供表单字段元数据查询，并给出统一的 BI 逻辑类型分类。

#### Scenario: 字段元数据复用统一口径

- **WHEN** 查询某连接某表单的字段元数据
- **THEN** 返回的 `path` MUST 与字段端点一致（可直接用作维度/度量字段标识）
- **AND** 字段中文名 MUST 合并字段字典的用户命名

#### Scenario: 逻辑类型归一

- **WHEN** 返回字段元数据
- **THEN** 每个字段 MUST 附带 `bi_type`（dimension/number/time），由字段类型归一计算

### Requirement: 表单明细查询

系统 SHALL 支持明细表图表（`chart_type=detail`，仅限 form 源），直接展示表单原始行，
并提供分页、排序与关键字过滤（明细大表逐行分析的基础）。

#### Scenario: 明细查询实时取行

- **WHEN** 查询 detail 类型指标
- **THEN** 系统 MUST 走表单行读取（与聚合共享行缓存）而非预聚合桶
- **AND** 返回行数 MUST 受上限约束（≤500）

#### Scenario: detail 仅限 form 源

- **WHEN** 非 form 源的定义声明 `chart_type=detail`
- **THEN** 校验 MUST 拒绝

#### Scenario: 分页下推（DB 源）

- **WHEN** 请求 `GET /bi/form-rows` 携带 `page/page_size`（DB 源）
- **THEN** 系统 MUST 以参数化分页查询而非下推全量（复用驱动层白名单标识符 +
  LIMIT/OFFSET 参数绑定），每页只取 `page_size+1` 行判断 `has_more`
- **AND** 单页 MUST 受上限约束（≤200）

#### Scenario: HAP 源分页与降级标记

- **WHEN** 表单源为 HAP，或 DB 源请求了无法安全下推的能力（关键字过滤 / 降序排序）
- **THEN** 系统 MUST 在 60s 行缓存内完成过滤/排序/切片，并在响应中标记
  `fallback`（`memory_paging` / `keyword_memory` / `sort_memory`）
- **AND** 关键字过滤 MUST 大小写不敏感地匹配任一字段值
- **AND** 前端 MUST 对降级路径给出可见提示（如"搜索为内存过滤"）

#### Scenario: 行数分档提示

- **WHEN** 单源行数超过警告线（默认 5 万）
- **THEN** 系统 MUST 记录告警并在分页响应中标记 `warn`

#### Scenario: 向后兼容

- **WHEN** 请求未携带分页参数
- **THEN** 行为 MUST 与旧版一致（全量读取并按 limit 截断，与聚合共享行缓存）

#### Scenario: 明细卡分页交互

- **WHEN** 前端明细卡渲染
- **THEN** MUST 提供搜索（防抖）、排序字段与升降序、上一页/下一页与页码/总行数展示
- **AND** 单页数据量 MUST 受控（默认 200 行，避免全量 DOM 渲染）
- **AND** 投放态自动轮播 MUST 保持既有行为（恒速上滚、到底回顶、悬停暂停）

### Requirement: 字段显示别名

系统 SHALL 支持指标定义级的字段显示别名（`style.column_labels`），
显示名优先级为「卡级别名 > 字段字典 name > 原始字段名」。

#### Scenario: 明细表头显示别名

- **WHEN** 明细指标定义声明 `style.column_labels = {"work_date": "生产时间"}`
- **THEN** 明细表头与排序字段下拉 MUST 显示"生产时间"（原始值仍以 `work_date` 传输）
- **AND** 未声明别名的字段 MUST 回落字段字典 name，字典未配置时回落原始字段名

#### Scenario: 定义页编辑入口

- **WHEN** 用户在定义页编辑明细图表（form 源）
- **THEN** MUST 提供「列显示名」编辑区（逐字段输入别名，留空即回落；位于
  detail 专属区域，维度/度量整块在 detail 下隐藏不影响本区）
- **AND** 保存后 MUST 经 `style.column_labels` 随定义持久化，投放页与放大弹层一并生效

#### Scenario: 与全局字段字典的关系

- **WHEN** 同一字段在字段字典（连接级）与卡片别名同时配置
- **THEN** 卡片别名 MUST 优先（局部覆盖全局），且两者互不影响

### Requirement: 内置指标默认停用

平台运营类内置指标 SHALL 默认停用（撤屏），引擎保留全部数据源类型，用户可手动启用。

#### Scenario: 新环境内置指标不上屏

- **WHEN** 全新环境完成内置指标种子写入
- **THEN** 所有内置指标 MUST 为停用状态

#### Scenario: 存量环境一次性撤屏

- **WHEN** 部署方执行内置指标收敛脚本
- **THEN** 存量库中 builtin 且仍启用的指标 MUST 被停用
- **AND** 脚本 MUST 幂等（重复执行无副作用）


### Requirement: 图表类型与数据形状校验

系统 SHALL 支持 17 种图表类型（柱状/折线/时间坐标系折线/饼图/散点/雷达/热力图/3D 矩阵/桑基/关系图/途径图/树图/漏斗/仪表盘/表格/指标卡/明细表），并 MUST 按图表类型校验数据形状。

#### Scenario: 形状不满足时拒绝保存

- **WHEN** 定义声明的图表类型与维度/度量数量不匹配（如散点图度量数 ≠2、关系族维度 <2、仪表盘维度 >1）
- **THEN** 校验 MUST 拒绝并说明该类型的形状要求

#### Scenario: 图表级样式可配置

- **WHEN** 定义配置 style（指标卡 bg_color/font_color、仪表盘 gauge_min/gauge_max）
- **THEN** 渲染时 MUST 应用该样式；未配置时使用深浅色默认

### Requirement: 结构化筛选规则

聚合定义 SHALL 支持结构化筛选规则（field + op + value + field_type 的列表，规则间 AND），筛选在聚合前过滤行；操作符 MUST 按字段逻辑类型区分（数值=区间/比较，时间=起止/早晚，文本=包含/等于/属于）。

#### Scenario: 类型感知的操作符

- **WHEN** 筛选规则的操作符与其 field_type 不匹配（如文本字段用区间）
- **THEN** 校验 MUST 拒绝并列出该类型可用操作符

#### Scenario: 旧等值形态兼容

- **WHEN** filters 为旧版等值字典（{字段: 值}）
- **THEN** 系统 MUST 继续按等值匹配处理

### Requirement: 看板筛选元素与拖拽布局

系统 SHALL 提供看板级设置（服务端持久化）：筛选元素（可添加/移除的维度下拉，选中值运行时注入全部含该维度的启用图表）与图表布局（顺序 + 全宽/半宽跨度，编辑模式拖拽调整）。

#### Scenario: 筛选元素多图联动

- **WHEN** 看板筛选元素选中某维度值
- **THEN** 所有含该维度的启用图表 MUST 按值过滤
- **AND** 无维度图表（指标卡/仪表盘）SHALL NOT 受影响

#### Scenario: 布局编辑持久化

- **WHEN** 布局编辑模式下拖拽换位或调整宽度
- **THEN** 变更 MUST 即时保存到服务端
- **AND** 管理页刷新与投放端 MUST 呈现同一布局
