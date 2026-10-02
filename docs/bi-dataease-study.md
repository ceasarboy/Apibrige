# DataEase v3 源码研究——BI 重定位的设计参照

> 目的：BI 大屏重定位为「连接表单内容可视化」后，研究成熟开源 BI 的领域模型，
> **只借鉴设计，不复制代码**（约束见文末）。来源：`github.com/dataease/dataease`
> dev-v3 分支浅克隆（Gitee 镜像 `fit2cloud-feizhiyun/DataEase`，24.6k stars，
> Spring Boot + Vue3 + AntV），本地副本在仓库外 `E:\dataease-study\dataease`（勿提交）。
> 三份子系统分析由并行探查完成，本文为提炼与决策；行号引用为当时快照。

## 1. 数据源与数据集层（后端）

**三层抽象**：类型枚举（`DatasourceConfiguration.DatasourceType`，含 SQL 标识符引号前后缀/方言）
→ 每类型一个配置 POJO（`@Component`，只负责拼 JDBC URL）→ 统一执行器
`CalciteProvider`（连接池 + 方言改写 + 跨源联邦）。
新增内置数据源类型 = 枚举加一项 + 配置类 + 方言分支，不写新 Provider。

**存储**：`core_datasource.configuration` 一列存整份连接配置 JSON（JPA Converter 整列 AES 加密，
密钥首启随机生成独立存 `core_rsa` 表）；返回前端二次加密或直接置空（`getSimpleDs`），
编辑态 `hidePW` 打码；JDBC URL 有前缀/危险参数黑名单校验（`JdbcUrlSecurityPolicy`）。

**数据集**：`core_dataset_group`（mode=直连|抽取、type=sql|union、info=关联树 JSON）+
`core_dataset_table`（物理来源）+ `core_dataset_table_field`（字段元数据）。
字段三元组是核心：**`groupType`（d=维度 / q=指标）+ `deType`（0文本/1时间/2整型/3浮点/4布尔/
5地理位置/6二进制/7URL 的统一逻辑类型）+ `extField`（0原始/1复制/2计算/3分组）**。
计算字段 = 表达式引用字段 ID（`[f_id]`），查询期正则展开 + 深度 100 防循环。

**查询执行（图表请求 → SQL）**：聚合**全部下推到 SQL**
（`Dimension2SQLObj` 生成 GROUP BY 列、`Quota2SQLObj` 生成 `SUM(x) AS f_y_1`，
指标过滤进 HAVING，`SQLProvider.sqlLimit` 出 LIMIT/TopN），
Calcite 只做「SqlNode 方言改写器」（单源）/ 内存联邦（跨源）。

**查询防护三件套**：每数据源 `queryTimeout`（默认 30s，注入 Statement）；
明细表 `pageSize` 被 `resultCount` 硬上限截断；聚合图 resultMode=custom 直接 LIMIT。

**直连 vs 抽取**：`core_dataset_group.mode` 一列决定查询路由（直连源库 vs 内置引擎库），
上层 SQL 生成无感；抽取落库走「tmp 表 → RENAME 原子换表 → 失败即删」。

## 2. 图表配置模型

**「轴即配置」宽列 JSON**：`core_chart_view` 一行一图，
`x_axis / x_axis_ext(系列拆分) / y_axis / y_axis_ext / ext_stack / ext_color / ext_label / ext_tooltip`
各存一个 JSON 数组；**每个元素 = 字段快照（id/名称/deType/groupType）+ 图表内行为
（summary 聚合、sort、filter→HAVING、formatterCfg、compareCalc 同环比）**。
样式三段分离：`customAttr`（数据样式）/ `customStyle`（容器样式）/ `senior`（辅助线/阈值/缩略轴），
前端契约即 `chart-attr.d.ts / chart-style.d.ts / chart-senior.d.ts`。

**图表类型插件**：前端 `render+type` 二元组注册表（glob 自动扫描每个图的
`AbstractChartView` 子类：`properties/propertyInner/axis/axisConfig/drawChart/setupDefaultOptions`）；
后端 Handler 注册表未命中回落 `DefaultChartHandler`，每类图只重载
`formatAxis()`（哪些轴进 SQL）与 `buildResult()`（行列→series 形状）。共 49 种图表类型。

**请求契约**：`POST /chartData/getData` **直接 POST 完整图表配置**（无中间查询 id，
保存即所得）+ `chartExtRequest`（仪表板过滤/联动/下钻/分页运行时上下文）；
响应为行式数据 + `dimensionList/quotaList`（点击事件反查字段用）。

**联动/钻取/跳转统一为「运行时过滤器注入」**：配置态三张小表存字段对映射，
运行态全部折叠为 `{fieldId, operator, value, filterType}` 列表进 WHERE；
钻取 = 钻取值变过滤 + 下级字段追加进维度递归重查询。点击事件携带
`dimensionList:[{id,value}]` 是三种交互统一实现的关键。

## 3. 仪表板 / 大屏 / 嵌入

**布局与数据绑定分离**：`core_data_visualization_info` 一行一画布
（`canvasStyleData` 画布配置 + `componentData` 组件数组 JSON）；
图表配置独立存 `core_chart_view`，前端以 `canvasViewInfo: Map<viewId, config>` 关联。
组件树平铺数组 + canvasId 归属；仪表板=72×36 网格坐标（x/y/sizeX/sizeY），
大屏=绝对像素（style.left/top/w/h），**同一套组件 JSON 按 dvType 双解释**
（换算核心仅 30 行：`utils/style.ts getShapeItemStyle`）。

**大屏自适应**：基准 1920×1080，`screenAdaptor ∈ {keep,widthFirst,heightFirst,full}`，
实现 = 内层 `width/height: curScale%` + `transform: scale(curScale) translateZ(0)` + 居中偏移
（DePreview.vue:198-212）；多页大屏用 Tab 组件内嵌 iframe 隔离 + Tab 级轮播。

**自动刷新三级**：画布级（canvasStyleData.refreshViewEnable/refreshTime）→
浏览器级整页 reload → 单图表级（core_chart_view.refresh*）；全部前端 setInterval。

**公开分享 = 受限令牌闭环**：不可猜 uuid + RSA 密文密码 + ticket（携带过滤参数、
首次访问后计时过期）→ 通过后签发与资源绑定的短 JWT（X-DE-LINK-TOKEN）→
服务端拦截器把该 token 白名单化（**只能访问该资源的 API**）。
iframe 嵌入 = postMessage 握手（ready → embeddedToken）+ origin 白名单 +
outerParams（URL 参数→组件字段过滤映射表）。

## 4. 借鉴决策（v1 采纳 / 后续 / 不引入）

**v1 直接采纳**（已落入本轮实施方案）：

1. **统一逻辑类型**：借鉴 deType+groupType——BI 侧给表单字段算出
   `bi_type ∈ {number, time, dimension}`（`GET /bi/form-fields` 端点统一计算），
   定义页据此自动分类维度/度量候选、自动设 field_type，不让用户猜。
2. **查询防护三件套**：form 拉行设 10 万行硬上限（截断告警）；
   明细端点 limit ≤500；聚合结果沿用 TopN 截断；拉行经适配器自带超时。
3. **快照缓存**借鉴直连/抽取二相性：v1 只做「直连 + 60s TTL 进程缓存」，
   多指标共享同一次拉取；tmp 表原子换表模式留作日后做「加速快照表」的范式。
4. **大屏等比缩放**：本项目已有 1920×1080 + transform:scale，与 DataEase 同构，验证通过。
5. **布局/绑定分离**作为大屏演进方向的存储原则：v1 大屏仍是「启用指标自动网格」，
   但今后若做自由画布，布局 JSON 与图表定义分存（不塞进一张表）。

**后续演进（记入 backlog，不在本轮）**：联动/钻取的运行时过滤器注入模型；
公开投放受限令牌（uuid+密码+ticket+最小权限）；单图表级刷新间隔；
模板导出/导入（= 画布 JSON + 图表配置字典 + ID 重映射）。

**明确不引入**：Calcite/SQL 方言改写层（本项目聚合统一在 Python 侧——
数据源含 HAP 表单这类「行只存在于内存」的 API 源，Python 聚合是唯一跨源一致的选项；
custom_sql 源保留给想用 SQL 的用户）；多租户/行权限树/企业版插件机制；SeaTunnel。

## 5. License 与合规

- 仅研究（学习设计思路）、不复制/翻译/改编其代码进本仓库 → GPLv3+ 附加条款不触发。
- 本文档只描述架构事实与借鉴结论，不含 DataEase 源码片段。
- 若未来采用「嵌入 DataEase 实例」路线，注意其附加条款（不得替换 Logo/版权）。
