## ADDED Requirements

### Requirement: 字段字典保存 API

系统 SHALL 提供 `PUT /api/v1/connections/{id}/fields` 端点，按连接 + 范围（DB 表名 / HAP 工作表 ID）批量保存字段字典（可修改显示名与读写属性）。

#### Scenario: 保存字段字典（DB 源按表）

- **WHEN** 客户端 PUT /api/v1/connections/{id}/fields?table=MTL_MATERIAL，body 为 `{"fields": [{"path": "MATERIAL_CODE", "name": "物料编码", "readable": true, "writable": false}, ...]}`
- **THEN** 系统按 `(connection_id, scope=MTL_MATERIAL, path)` upsert 字段记录
- **AND** 新增字段默认 `readable=true, writable=true`（用户"默认既可读又可写"）
- **AND** 已存在字段仅更新传入属性，未传入属性保持不变

#### Scenario: 保存字段字典（HAP 按工作表）

- **WHEN** 客户端 PUT /api/v1/connections/{id}/fields?worksheet_id=6a85006cda2a332216c6f288，body 列出控件 path
- **THEN** 系统以 `scope=worksheet_id` 保存控件级字段字典

#### Scenario: 读写属性缺省默认双可读写

- **WHEN** 客户端保存某 path 时未传 readable/writable 或整体未传
- **THEN** 系统按 `readable=true, writable=true` 落库

### Requirement: 字段定义查询合并字典

系统 SHALL 在 `GET /api/v1/connections/{id}/fields` 返回字段时合并已保存的字段字典，未保存字段给出默认读写属性。

#### Scenario: 已保存字段按字典回显

- **WHEN** 客户端 GET /api/v1/connections/{id}/fields?table=MTL_MATERIAL，且该表已有字典记录
- **THEN** 返回的字段使用字典中的 name/readable/writable，并带 `saved: true`

#### Scenario: 未保存字段默认双可读写

- **WHEN** 字段无对应字典记录
- **THEN** 返回 name 用拉取值（HAP 用 controlName/alias/controlId），readable/writable 均 true，`saved: false`