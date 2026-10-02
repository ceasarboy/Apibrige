## ADDED Requirements

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
