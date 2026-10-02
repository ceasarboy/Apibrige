## Why

连接连通 API 后，系统只能展示自动拉取的字段（名称、类型、可空、主键），用户**无法对字段做任何自定义**：

1. 字段显示名不可改（HAP 的 controlName 可能为 null，映射页里看到的是丑陋的 controlId）；
2. 字段的读写属性无法表达——同一个字段在源侧能否被读取、在目标侧能否被写入，目前没有概念，同步时按映射全量执行；
3. 无持久化，字段定义每次拉取都是瞬时产物，无法作为映射配置的参考基线。

这导致明道云等目标端测试时（建表→读写验证）无法按用户意图控制"只读/只写/双向"字段。

## What Changes

### 新增：字段字典（ConnectionField）
- 新增 ORM 模型 `ConnectionField`（建表 `connection_fields`，随 `create_all` 自动创建）：
  - `connection_id` + `scope`（DB 表名 / HAP 工作表 ID）+ `path`（字段 ID/列名）三元唯一
  - `name`（可修改的显示名）、`readable`（可读勾选）、`writable`（可写勾选）
  - 创建记录时默认 `readable=True, writable=True`（既可读又可写）
- 字段定义以「连接 + 范围（表/工作表）」为单位保存，同一连接可保存多套。

### 新增：字段字典 API
- `PUT /api/v1/connections/{id}/fields?table=&worksheet_id=` 批量 upsert 字典：
  body 为 `{"fields": [{"path", "name", "readable", "writable"}, ...]}`
  - DB 连接按 `table` 定 scope，HAP 按 `worksheet_id` 定 scope
  - path 相同则覆盖 name/readable/writable，path 不存在则新增（默认双读写）
- `GET /api/v1/connections/{id}/fields`（已有端点增强）：返回字段时**合并已保存字典**——
  有字典记录的字段用字典的 name/readable/writable，无记录的用拉取值 + 默认双可读写。
- 响应中每个字段带 `saved: true/false` 与读写标记，供前端展示"已保存/未保存"。

### 修改：同步按读写过滤
- `sync_handler` 转换阶段（transform_payload 之后、推送之前）：
  - **源侧**：源连接字段字典中 `readable=false` 的字段在转换结果中剔除（不读）；
  - **目标侧**：目标连接字段字典中 `writable=false` 的字段在推送载荷中剔除（不写）。
- 无字典（未保存过）时行为与现状一致：全部字段参与同步，不产生行为回归。
- 过滤在 records_full 键集计算**之前**做源侧过滤（读过滤），删除同步键集基于读过滤后的记录；目标侧写过滤在推送前做，不影响键集。

### 修改：前端字段抽屉
- 「字段」抽屉表格每行变为可编辑：字段名称 Input（可改）、可读 Checkbox、可写 Checkbox（默认勾选）。
- 新增「保存字段定义」按钮 → PUT 批量保存；保存成功提示并刷新读取状态。
- 未保存的字段仍显示默认双勾选；已保存字段按字典值回显。

## 非目标

- 不做字段级权限（用户/角色级可读写控制）。
- 不做字段排序/分组/隐藏的可视化编排。
- 不在探针 CLI 增加读写过滤参数（探针 L2 仍按映射全量校验，字典只影响运行时传输）。

## Capabilities

### New Capabilities

- `connection-api`: 字段字典 API — 连接级字段定义的保存、合并查询（connection-api 域扩展）

### Modified Capabilities

- `data-pipeline`: 同步时按连接字段字典读写过滤（data-pipeline 域扩展）
- `management-ui`: 字段抽屉可编辑（名称 + 读写勾选 + 保存）

## Impact

### 代码影响
- **新增** `ConnectionField` 模型（app/models/sync.py）
- **修改** `app/api/connections.py` — GET fields 合并字典 + 新增 PUT fields
- **修改** `app/services/sync_handler.py` — 读写过滤
- **新增** `app/services/field_dict.py` — 字典查询/叠加工具（源/目标侧过滤共用）
- **修改** `frontend/src/pages/connections/index.tsx` — 字段抽屉编辑 + 保存
- **新增** `tests/test_field_dictionary.py` — API 保存/合并 + 同步读写过滤测试

### 依赖变更
无新依赖。

### 部署影响
无（建表随启动 create_all 自动完成）。