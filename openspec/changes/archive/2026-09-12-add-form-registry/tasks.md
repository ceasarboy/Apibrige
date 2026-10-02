# Form Registry 变更

## Delta Specifications

- [x] [connection-api](specs/connection-api/spec.md)
- [x] [management-ui](specs/management-ui/spec.md)

## Tasks

## 1. HAP V3 list_worksheets 实测与适配器（最大风险项最先做）

- [x] 1.1 用真实凭证实测工作表清单接口：确认私有部署版本可用性与返回结构（id/name/remark）→ 验证: 冒烟脚本输出工作表清单（凭证走脚本参数/环境变量，不入库不入文档）
  - 实测结论：按项目规矩（AGENTS.md §3.8）改用 **V2** `POST /v1/open/app/get`（query 参数鉴权），返回 `sections[].items[]`，可用；未走 V3。
- [x] 1.2 `HapV3Adapter.list_worksheets()`：凭证复用连接现有 appKey/sign（enc: 解密注入，异常与响应脱敏）→ 验证: mock 单测断言请求头/路径/解析；真实端点复测一次
  - 实现为 V2 `/v1/open/app/get`，扁平化 sections→items；真实端点复测通过（「明道云2」5 张工作表）。
- [x] 1.3 若实测不可用：记录失败原因，sync 端点对该连接返回明确降级提示（手动登记路径保持可用）→ 验证: 手动登记 + 列表用例不受影响
  - 实测可用，本项不适用；降级提示与手动登记路径仍按设计保留并有单测覆盖。

## 2. 模型与注册表服务

- [x] 2.1 `ConnectionForm` 模型（connection_id + form_key 唯一，original_name/display_name/remark/source）→ 验证: 建表随 create_all；tests/test_form_registry.py 落库断言
- [x] 2.2 `app/services/form_registry.py` 拉取合并：新 key 插入（source=auto）、已存在仅刷 original_name、消失项保留，返回 {added, refreshed, total}；DB 走现有 list_tables、HAP 走 list_worksheets、REST 报不支持 → 验证: 合并规则单测（含 display_name 不被覆盖）
- [x] 2.3 桥引用统计：HAP 按 mapping_config.protocol.worksheet_id/endpoint、DB 按 source_table/target_table 匹配 form_key，大小写互认，产出 bridge_count + bridges 明细（桥名/方向/对端连接+对端表单）→ 验证: 单测覆盖 Oracle 大写表名 × 小写映射互认、源/目标双向、无引用为 0

## 3. 注册表 API

- [x] 3.1 `GET /api/v1/forms`（connection_id 可空筛选；响应含引用统计与所属连接信息；脱敏复核）→ 验证: TestClient 用例（全量/筛选/引用明细形状）
- [x] 3.2 `POST /api/v1/forms/{connection_id}/sync`（按协议分发；失败降级提示；REST 400）→ 验证: 用例覆盖 DB/HAP/REST 三分支 + 失败提示文案
- [x] 3.3 `POST /api/v1/forms`（手动登记 source=manual；重复 409）/ `PUT /api/v1/forms/{id}`（仅 display_name/remark）/ `DELETE /api/v1/forms/{id}`（删注册不碰映射）→ 验证: CRUD + 409 + 删除后映射仍可运行的回归用例
- [x] 3.4 router 注册进 main.py → 验证: 启动后 OpenAPI 出现 /api/v1/forms 路径

## 4. 前端表单管理页

- [x] 4.1 新页面 frontend/src/pages/forms/：平铺表格（本地名称/原始名/form_key/所属连接/备注/来源标签/已建桥数+Tooltip 引用明细/操作）+ 顶部连接筛选与搜索 → 验证: npx tsc --noEmit 零错误（清 tsbuildinfo 后跑）
- [x] 4.2 「从源端拉取」（按筛选连接触发，提示 added/refreshed，失败展示原因）与「手动添加」弹窗（连接/form_key/名称/备注，409 提示冲突）→ 验证: API 契约由后端用例覆盖；浏览器实测（项目规矩：tsc 绿 ≠ 能用）
- [x] 4.3 行内"编辑"（改显示名/备注）与"删除"（确认弹窗）→ 验证: 浏览器实测
- [x] 4.4 路由与菜单（与连接/映射/任务平级）→ 验证: 浏览器走查导航可用

## 5. 映射页表单选择联动

- [x] 5.1 源/目标连接选中后，表单字段渲染 Select（选项 = GET /forms?connection_id=，显示 `display_name（form_key）`），附"手动输入"切换开关回退 Input → 验证: tsc 零错误 + 浏览器实测切换
- [x] 5.2 选中表单自动回填：HAP 写 mapping_config.protocol.worksheet_id + endpoint；DB 写 source_table/target_table；编辑已有映射时按现行值反显 → 验证: 浏览器实测（新建 HAP→HAP 与 DB→HAP 各一例，回填后"获取字段"能拉到字段）
- [x] 5.3 手动输入路径回归：未登记表单手输 ID 建映射，拉字段/保存照常 → 验证: 用户浏览器走查确认（2026-09-12，"功能已经满足我的要求"）

## 6. 测试与收尾

- [x] 6.1 新增 tests/test_form_registry.py 全套（模型/合并/引用/API）；全量 `pytest tests/ -q` 通过 → 验证: 全量输出 546 passed / 11 skipped / 1 failed
  - 唯一失败 `test_group7_islands.py::TestConflictSkip::test_sync_all_conflict_skips_push` 为本机 **Redis 未安装**（localhost:6379 拒连）导致的环境性失败，与本变更无关。
- [x] 6.2 前端全量 `npx tsc --noEmit`（清缓存）已通过；两页浏览器走查（表单管理页全操作 + 映射页联动）用户已确认 → 验证: 用户浏览器走查 2026-09-12（后端 8000 + 前端 3000 实测环境）；确认语与走查记录入 docs/project-status.md

## 7. 追加项（用户反馈驱动）

- [x] 7.1 表单注册表行内新增「字段」入口：抽出共用组件 `frontend/src/components/FieldDictDrawer.tsx`（连接管理页与注册表页共用），注册表按 form_key 直接定位表单（DB=表名 / HAP=worksheet_id），复用既有字段字典（`GET/PUT /connections/{id}/fields`），无新增后端契约 → 验证: tsc 零错误 + `GET /connections/{id}/fields?worksheet_id=...` 实测 200（产品目录 23 控件）
- [x] 7.2 修复 vite dev proxy 端口错配（8001 → 基线 8000），此前导致前端全页面无数据 → 验证: 经 3000 代理访问 connections/forms/mappings 均 200
- [x] 7.3 修复 HAP 适配器 base_url 尾斜杠拼接出 `//v2/...` 被明道云拒绝（405 → 前端 500）：`HapV3Adapter.__init__` 统一 `rstrip("/")`；`GET /connections/{id}/fields` 的 HAP 分支加 try/except 转 502 带原因 → 验证: 相关单测 133 passed；真实端点复测 200

## 8. 追加项二：明道云开放 API V2/V3 双实现（用户要求：两代接口各做一个）

- [x] 8.1 真实端点双实测：V2 `GET /v1/open/app/get`（query 鉴权）与 V3 `POST /v3/app/worksheets/list`
  （header `HAP-Appkey`/`HAP-Sign` 鉴权）均可用，返回清单一致（「明道云2」5 张工作表）→ 验证: scripts/_probe_v3_list.py 真实打点（用完即删）
- [x] 8.2 `list_worksheets` 拆为 `list_worksheets_v2` / `list_worksheets_v3` 双实现，统一入口
  先 V3（凭证不进 URL）后 V2 自动回退，成功链路记 `last_list_api`，双失败抛带双端原因异常
  → 验证: tests/test_hap_list_worksheets.py Part A/B；真实端点复测输出 "OK via v3: 5 个工作表"
- [x] 8.3 控件定义同构双实现：新增 `get_worksheet_detail_v3`（`GET /v3/app/worksheets/{id}`，
  fields[].type 字符串名 → V2 数字码映射 `_V3_FIELD_TYPE_CODES`，系统字段 `_SYSTEM_FIELD_IDS`
  剔除对齐 V2 口径）；`get_worksheet_controls` 先 V3 后 V2 回退，记 `last_controls_api`
  → 验证: tests/test_hap_list_worksheets.py Part C（13 用例）
  - **根因发现**：V2 getWorksheetInfo 对"无视图的空工作表"（v2 addWorksheet 刚建、views=[]，
    如采购订单表）返回服务端"请求异常"（error_code=0，加参变体同样失败）；V3 详情正常返回空
    fields。控件链路接 V3 后该表从 502 报错变为正常空列表（真实端点复测：5 张表全部 via v3 成功）
- [x] 8.4 全量回归 `pytest tests/ -q` = **558 passed / 11 skipped** → 验证: 本机实测输出（2026-09-12）
- [x] 8.5 类型显示失真修复（用户确认"大问题"后）：`GET /connections/{id}/fields` HAP 分支的
  类型映射 {2,6,15,32}→string 替换为实证中文名表 `HAP_CONTROL_TYPE_NAMES`
  （文本/数值/金额/单选/多选/选项/附件/日期/分段/成员/部门/关联记录/自动编号），
  未收录码显示"未知类型(N)"可诊断 → 验证: tests/test_field_dictionary.py
  TestHapFieldsTypeNames 3 用例；真实控件复测产品目录 23 控件盲区 13 → 0；全量 561 passed
- [x] 8.6 字段标识语义根因修复：**getFilterRows 行数据键 = alias（有别名时），非 controlId**
  （真实行数据逐键归类实证：产品目录首行 40 键中 24 个业务键全为 alias，仅无别名字段用 controlId）；
  addRow 写入实测 alias/controlId 双认（写入+物理删除验证）→ 字段端点 `path` 从 controlId
  改为 `alias || controlId`（读写两侧统一运行时行键；无别名表零回归）
  → 验证: TestHapFieldsTypeNames::test_path_prefers_alias_as_runtime_row_key
- [x] 8.7 段落控件剔除（用户拍板）：字段端点 HAP 分支跳过 type=22（分段是表单设计器的
  分组标题/布局元素，真实行数据 40 键中无分段键、不可映射同步，剔除防误选为映射字段/业务键）
  → 验证: TestHapFieldsTypeNames::test_divider_sections_excluded；全量 562 passed

