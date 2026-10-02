## Purpose

提供连接管理、字段映射、同步任务、执行日志、监控统计的后端 RESTful API，供管理界面和外部系统调用。

## ADDED Requirements

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
