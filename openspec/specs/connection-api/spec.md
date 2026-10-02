# connection-api Specification

## Purpose
提供连接管理、字段映射、同步任务、执行日志、监控统计的后端 RESTful API，供管理界面和外部系统调用。
## Requirements
### Requirement: 连接管理 CRUD API

系统 SHALL 提供 `/api/v1/connections` 端点，支持 API/DB 连接的创建、查询、更新、删除操作。

#### Scenario: 创建连接

- **WHEN** 客户端 POST /api/v1/connections，传入 name、connection_type、protocol、base_url（或 db_type+db_config）、auth_type、auth_config
- **THEN** 系统创建连接记录，密码等敏感信息加密存储，返回 201 和连接 ID

#### Scenario: 查询连接列表

- **WHEN** 客户端 GET /api/v1/connections，可选传入 connection_type 过滤参数
- **THEN** 系统返回连接列表，敏感字段（密码、密钥）脱敏显示

#### Scenario: 更新连接

- **WHEN** 客户端 PUT /api/v1/connections/{id}，传入更新字段
- **THEN** 系统更新连接记录，返回 200

#### Scenario: 删除连接

- **WHEN** 客户端 DELETE /api/v1/connections/{id}
- **THEN** 系统检查该连接是否被映射引用，若被引用返回 409 冲突，否则删除并返回 204

### Requirement: 连接测试 API

系统 SHALL 提供 `/api/v1/connections/{id}/test` 端点，测试连接可用性。

#### Scenario: 测试连接成功

- **WHEN** 客户端 POST /api/v1/connections/{id}/test
- **THEN** 系统用该连接配置调用对应适配器的 test_connection，返回 {"status": "ok", "latency_ms": 120}

#### Scenario: 测试连接失败

- **WHEN** 目标系统不可达或认证失败
- **THEN** 系统返回 {"status": "failed", "error": "具体错误信息"}

### Requirement: 数据库表结构查询 API

系统 SHALL 提供 `/api/v1/connections/{id}/tables` 和 `/api/v1/connections/{id}/schema/{table}` 端点。

#### Scenario: 获取表列表

- **WHEN** 客户端 GET /api/v1/connections/{id}/tables，且连接为数据库类型
- **THEN** 系统返回该数据库的所有表名列表

#### Scenario: 获取表结构

- **WHEN** 客户端 GET /api/v1/connections/{id}/schema/{table}
- **THEN** 系统返回该表的字段定义列表（字段名、类型、是否可空、主键）

### Requirement: 字段映射 CRUD API

系统 SHALL 提供 `/api/v1/mappings` 端点，支持字段映射的 CRUD 和转换预览。

#### Scenario: 创建字段映射

- **WHEN** 客户端 POST /api/v1/mappings，传入 name、source_connection_id、target_connection_id、source_endpoint/table、target_endpoint/table、mapping_config
- **THEN** 系统创建映射记录，返回 201 和映射 ID

#### Scenario: 预览转换结果

- **WHEN** 客户端 POST /api/v1/mappings/{id}/preview，传入测试数据
- **THEN** 系统按映射规则转换数据，返回转换后的结果或转换错误

### Requirement: 同步任务 CRUD API

系统 SHALL 提供 `/api/v1/tasks` 端点，支持同步任务的 CRUD、手动执行、暂停/恢复。

#### Scenario: 创建任务

- **WHEN** 客户端 POST /api/v1/tasks，传入 mapping_id、sync_mode、schedule_cron
- **THEN** 系统创建任务记录，返回 201 和任务 ID

#### Scenario: 手动执行任务

- **WHEN** 客户端 POST /api/v1/tasks/{id}/run
- **THEN** 系统立即触发同步，返回执行结果（同步模式直接返回结果，异步模式返回 buffer_id）

#### Scenario: 暂停/恢复任务

- **WHEN** 客户端 POST /api/v1/tasks/{id}/pause
- **THEN** 任务状态变为 paused
- **WHEN** 客户端 POST /api/v1/tasks/{id}/resume
- **THEN** 任务状态变为 active

### Requirement: 执行日志查询 API

系统 SHALL 提供 `/api/v1/logs` 端点，支持按时间、任务、状态过滤的分页查询。

#### Scenario: 分页查询日志

- **WHEN** 客户端 GET /api/v1/logs?page=1&page_size=20&task_id={id}&status=failed&start_time={iso}&end_time={iso}
- **THEN** 系统返回符合条件的日志列表及分页信息

### Requirement: 监控统计 API

系统 SHALL 提供 `/api/v1/metrics` 端点，返回监控看板所需的统计数据。

#### Scenario: 获取看板概览

- **WHEN** 客户端 GET /api/v1/metrics/overview?period=24h
- **THEN** 系统返回 {sync_count, success_rate, p50_latency, p95_latency, p99_latency, queue_depth, task_status_distribution}

#### Scenario: 获取趋势数据

- **WHEN** 客户端 GET /api/v1/metrics/trend?period=24h&interval=1h
- **THEN** 系统返回按时间间隔聚合的同步量趋势数据（时间戳、成功数、失败数）

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

### Requirement: 表单注册表查询

系统 SHALL 提供表单注册表查询接口 `GET /api/v1/forms`，支持按连接筛选（connection_id 可空返回全部），每行返回本地名称、原始名、form_key、备注、来源（auto/manual）、所属连接信息，以及该表单被映射引用的统计（bridge_count 与引用明细）。

#### Scenario: 全量查询含引用统计

- **WHEN** 客户端 GET /api/v1/forms 不带参数
- **THEN** 系统返回全部连接的全部已登记表单
- **AND** 每行含 `bridge_count` 与 `bridges` 引用明细（映射名称、本表单方向 源/目标、对端连接名与对端表单标识）

#### Scenario: 按连接筛选

- **WHEN** 客户端 GET /api/v1/forms?connection_id={id}
- **THEN** 系统只返回该连接下的表单

#### Scenario: 引用匹配大小写互认

- **WHEN** 表单 form_key 为大写表名（如 `MTL_MATERIAL`）而映射 source_table 为小写（`mtl_material`）
- **THEN** 系统 MUST 将该映射计入 bridge_count（大小写不敏感比对）

### Requirement: 表单注册表从源端拉取

系统 SHALL 提供 `POST /api/v1/forms/{connection_id}/sync` 端点，从源端拉取表单清单并合并入注册表：数据库连接列出全部表；HAP 连接通过 V3 worksheets/list 接口列出全部工作表；无结构协议（REST 等）返回 400 并提示手动登记。

#### Scenario: DB 连接拉取合并

- **WHEN** 客户端对 Oracle 连接 POST /api/v1/forms/{id}/sync，源端有表 A、B、C，注册表已有表 A（用户已改显示名）与表 D（源端已删）
- **THEN** 新表 B、C 以 source=auto 插入，display_name 默认为源端表名
- **AND** 表 A 仅刷新 original_name，display_name 保持用户设置
- **AND** 表 D 保留不删
- **AND** 响应含 `{added, refreshed, total}` 统计

#### Scenario: HAP 连接拉取走 V3

- **WHEN** 客户端对 HAP 连接 POST /api/v1/forms/{id}/sync
- **THEN** 系统通过 `POST {base_url}/v3/app/worksheets/list`（header HAP-Appkey/HAP-Sign 鉴权）拉取工作表清单，按 id 登记 form_key、按 name 登记 original_name
- **AND** 凭证从连接配置解密注入，任何日志/响应输出不泄漏密钥

#### Scenario: V3 不可用时明确降级提示

- **WHEN** HAP 私有部署不支持 V3 接口或鉴权失败
- **THEN** sync 返回明确错误信息（含原因），系统其余功能不受影响，前端提示改用手动登记

#### Scenario: 无结构协议不支持自动拉取

- **WHEN** 客户端对 REST 连接 POST /api/v1/forms/{id}/sync
- **THEN** 系统返回 400 并说明该协议不支持自动拉取，应手动登记

### Requirement: 表单注册表手动维护

系统 SHALL 提供表单的手动登记、编辑与删除接口：`POST /api/v1/forms`（登记，source=manual）、`PUT /api/v1/forms/{id}`（修改 display_name/remark）、`DELETE /api/v1/forms/{id}`（删除注册记录，不影响已建映射）。

#### Scenario: 手动登记

- **WHEN** 客户端 POST /api/v1/forms，body 含 connection_id、form_key、display_name、remark
- **THEN** 系统以 source=manual 落库，(connection_id, form_key) 重复时返回 409

#### Scenario: 编辑不碰来源属性

- **WHEN** 客户端 PUT /api/v1/forms/{id} 修改 display_name/remark
- **THEN** 仅更新传入字段，original_name/source/form_key 不变

#### Scenario: 删除注册不影响桥

- **WHEN** 客户端 DELETE /api/v1/forms/{id} 删除一个已被映射引用的表单
- **THEN** 注册记录删除，引用它的映射与同步任务保持不变且继续可运行

