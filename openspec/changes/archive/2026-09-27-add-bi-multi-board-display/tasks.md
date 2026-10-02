# add-bi-multi-board-display 任务清单（已归档）

## 1. 多看板（多画布）

- [x] `bi_screen_settings` 多行化：id 即 board key，default 兜底虚拟项（零迁移）
- [x] API：`GET/POST /bi/screens`、`PUT/DELETE /bi/screens/{key}`（default 不可删、key 格式校验、409 重复）
- [x] `screen-settings` 读写带 `board` 参数（缺省 default 兼容旧行为）
- [x] seed 幂等确保 default 行（`ensure_default_board`）
- [x] 归属语义：`resolveCells` 去 defaultCells 全量兜底；cells 即上屏
- [x] 管理页：看板切换器/新建（自动进布局编辑）/重命名/删除 + 卡片库点选上屏 + 编辑模式卡片 × 移出
- [x] 投放页 `?board=key` 独立地址 + 标题随看板名
- [x] 测试：TestBoards 5 用例（列表兜底/增删改查/隔离/校验/默认保护）

## 2. 投放显示设置

- [x] 调研 DataEase：screenAdaptor 四模式语义与缩放比口径（DePreview.resetLayout）
- [x] `ScreenSettings` 扩展：base_w/base_h/screen_adaptor/theme
- [x] 投放页：基准分辨率解释格位（cellW=base_w/72、rowH=base_h/36）+ 四适配模式 + 深浅主题
- [x] 格位坐标映射内容区（ResizeObserver 实测）：摆到底部的卡片不被裁
- [x] 投放页 30s 轮询设置：管理页改配置大屏免刷新生效
- [x] keep 模式垂直+水平精确居中（余量 margin，块级 margin auto 垂直无效已踩坑）
- [x] 管理页「看板设置」双 Tab（投放显示 / 筛选元素）；模式文案含适用场景说明
- [x] `scr-root--light` CSS 变量覆写（零组件改动）+ ScreenPanel light 变体
- [x] 自审：`scripts/display_settings_ui_check.py` 15 项（含 keep 居中/轮询跟随）

## 3. 明细表自动轮播

- [x] DetailTable：rAF 恒速上滚（22px/s）、到底停留 1.6s 回顶循环、悬停暂停
- [x] scrollTop 本地浮点累积（直接 += 小数被取整吞掉——实测踩坑）
- [x] 深色/白底统一滚动容器；深色表格去 12 行截断；表头 sticky 吸顶
- [x] antd 表格去分页改滚动（大屏无人翻页）
- [x] 管理页不滚动（编辑友好），投放页启用（autoScroll 透传）
- [x] 自审断言：多次采样极差判定（兼容回顶）+ 悬停暂停

## 4. 修复与门禁

- [x] KPI 数值被裁：CSS 紧凑化（标题栏/内边距/数字 24px）+ 默认看板重排（KPI 5 行/明细 7 行）
- [x] 自审脚本主题安全：运行时设深色、结束还原用户主题（保护白底设置）
- [x] 三个 UI 自审脚本随仓库保留（生产看板 17 项 / 多看板 14 项 / 显示设置 15 项）
- [x] 全量 pytest 850 passed / 13 skipped；tsc 零错误
