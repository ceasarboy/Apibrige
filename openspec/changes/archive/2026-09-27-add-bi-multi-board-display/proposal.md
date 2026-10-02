# add-bi-multi-board-display 提案

## Why

生产运行管理看板验收后（2026-09-27），用户提出三个演进诉求，均来自真实投放场景：

1. **BI 可以建多个、用于不同地方演示**——现有 `bi_screen_settings` 为单例（id='default'），
   所有启用指标挤同一块画布（人员技能看板与生产看板互相挤压、此前被迫撤屏）。
2. **播放设备不一样**——需要布局长宽比例可设、便于缩放；且基本样式要有黑底/白底两种
   （原实现非全屏白底、全屏深色不一致）。
3. **明细表要自动轮动显示**——大屏无人值守时表格内容自动向上滚动循环。

设计参照 DataEase v3 源码（`DePreview.vue` / `CanvasAttr.vue` / `custom-component/`）：
`screenAdaptor` 四模式（keep/widthFirst/heightFirst/full，缩放比=容器/基准分辨率）、
主题亮暗两键、标题/筛选为画布内组件或悬浮（**无固定占位**）、明细表自动滚动；
**只借鉴设计，不复制代码**。

## What Changes

1. **多看板（多画布）**：`bi_screen_settings` 每行一块画布（id 即 board key，零迁移）；
   新增 `GET/POST /bi/screens`、`PUT/DELETE /bi/screens/{key}`（default 保留名不可删，
   缺失时列表兜底虚拟项）；`screen-settings` 读写带 `board` 参数（缺省 default 兼容旧行为）。
   **归属语义「摆放即归属」**：cells 里摆了的指标才上屏，同一指标可摆进多个看板，
   定义只存一份；管理页看板切换/新建（自动进布局编辑）/重命名/删除 + 卡片库点选上屏 +
   编辑模式卡片移出；投放页 `?board=key` 独立地址、标题随看板名。
2. **投放显示设置（看板级）**：`base_w/base_h` 基准分辨率（预设 1920×1080/1280×720/
   2560×720/4K + 自定义）、`screen_adaptor` 四适配模式、`theme` 深/浅两色；
   管理页「看板设置」双 Tab（投放显示/筛选元素）；投放页 30s 轮询设置免刷新生效；
   格位坐标映射**内容区**（实测尺寸，标题/筛选之下），摆到底部的卡片不被裁。
3. **明细表自动轮播**：投放态内容溢出时恒速（22px/s）上滚、到底停留 1.6s 回顶循环、
   悬停暂停；深色/白底两种表格统一滚动容器与行为；深色表格去掉 12 行截断；
   表头 sticky 吸顶；管理页不滚动（编辑友好）。
4. **回归门禁**：`scripts/production_bi_ui_check.py`（明细可见/自动轮播/悬停暂停/KPI 完整）、
   `scripts/multi_board_ui_check.py`（多看板全流程 14 项）、
   `scripts/display_settings_ui_check.py`（显示设置 15 项）随仓库保留；
   自审脚本运行时临时设深色主题、结束还原用户设置。

## Impact

- **specs**：`bi-aggregation` 新增 3 组需求（多看板、投放显示设置、明细表滚动），
  修改 1 组（BI 大屏展示——分辨率自适应细化为基准分辨率+适配模式，明细表滚动）
- **代码**：`app/api/bi.py`（screens CRUD + board 参数）、`app/services/bi_seed.py`
  （默认看板幂等）、`app/models/bi.py`（to_def_dict 带 style）
- **前端**：`pages/bi/{index,screen,BiBoard}.tsx`、`components/screen/{ScreenDecor,screen.css}`
- **兼容**：default 看板与既有 cells 布局零迁移；指标定义、聚合引擎零变化
