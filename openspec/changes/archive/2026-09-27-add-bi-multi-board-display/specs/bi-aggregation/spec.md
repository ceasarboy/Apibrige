# add-bi-multi-board-display 规格增量

## ADDED Requirements

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

## MODIFIED Requirements

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
