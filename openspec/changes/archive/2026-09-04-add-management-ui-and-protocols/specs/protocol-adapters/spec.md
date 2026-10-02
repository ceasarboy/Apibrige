## Purpose

提供统一的协议适配器层，将 REST、SOAP、GraphQL、WebSocket、gRPC 等异构协议抽象为一致的 fetch_data / push_data 接口，使数据同步核心逻辑与具体协议解耦。

## ADDED Requirements

### Requirement: 统一协议适配器接口

系统 SHALL 定义统一的协议适配器接口，所有协议适配器 MUST 实现相同的 fetch_data、push_data、test_connection 方法，核心同步逻辑通过该接口调用而非直接依赖具体协议。

#### Scenario: 协议适配器工厂创建

- **WHEN** 系统需要与外部系统通信，根据连接配置的协议类型（rest/soap/graphql/websocket/grpc）从工厂获取对应适配器
- **THEN** 系统返回正确的适配器实例，调用方无需感知具体协议

#### Scenario: 统一数据拉取

- **WHEN** 系统通过适配器调用 fetch_data，传入端点、参数、认证信息
- **THEN** 适配器将返回统一格式的数据（Python dict 或 list），无论底层协议是 REST JSON 还是 GraphQL Response 还是 gRPC Message

#### Scenario: 统一数据推送

- **WHEN** 系统通过适配器调用 push_data，传入端点、数据、认证信息
- **THEN** 适配器将数据转换为目标协议格式并发送，返回统一格式的响应（status_code + body）

### Requirement: REST 适配器

系统 SHALL 提供 REST 协议适配器，支持 GET/POST/PUT/PATCH/DELETE 方法，支持 JSON 请求体和查询参数。

#### Scenario: REST GET 拉取数据

- **WHEN** 系统通过 REST 适配器 fetch_data，传入 endpoint 和 query params
- **THEN** 适配器发起 HTTP GET 请求，返回解析后的 JSON 数据

#### Scenario: REST POST 推送数据

- **WHEN** 系统通过 REST 适配器 push_data，传入 endpoint 和 JSON 数据
- **THEN** 适配器发起 HTTP POST 请求，返回响应状态码和响应体

#### Scenario: REST 认证注入

- **WHEN** 连接配置了 API Key / Basic Auth / Bearer Token 认证
- **THEN** 适配器在请求中自动注入对应的认证 Header

### Requirement: SOAP 适配器

系统 SHALL 提供 SOAP 协议适配器，支持通过 WSDL 定义发现和调用 Web Service 操作。

#### Scenario: SOAP 调用拉取数据

- **WHEN** 系统通过 SOAP 适配器 fetch_data，传入 WSDL URL 和操作名
- **THEN** 适配器解析 WSDL，调用对应操作，返回 SOAP 响应转换为 dict

#### Scenario: SOAP 推送数据

- **WHEN** 系统通过 SOAP 适配器 push_data，传入操作名和数据
- **THEN** 适配器将数据构造为 SOAP 请求体，发送并返回响应

### Requirement: GraphQL 适配器

系统 SHALL 提供 GraphQL 协议适配器，支持 Query 和 Mutation 操作。

#### Scenario: GraphQL Query 拉取数据

- **WHEN** 系统通过 GraphQL 适配器 fetch_data，传入 GraphQL 查询语句和变量
- **THEN** 适配器发起 GraphQL Query 请求，返回 data 字段内容

#### Scenario: GraphQL Mutation 推送数据

- **WHEN** 系统通过 GraphQL 适配器 push_data，传入 Mutation 语句和变量
- **THEN** 适配器发起 GraphQL Mutation 请求，返回变更结果

### Requirement: WebSocket 适配器

系统 SHALL 提供 WebSocket 协议适配器，支持建立持久连接并接收实时推送数据。

#### Scenario: WebSocket 接收数据

- **WHEN** 系统通过 WebSocket 适配器 fetch_data，传入 WebSocket URL
- **THEN** 适配器建立 WebSocket 连接，接收消息并返回解析后的数据

#### Scenario: WebSocket 发送数据

- **WHEN** 系统通过 WebSocket 适配器 push_data，传入消息内容
- **THEN** 适配器通过已建立的 WebSocket 连接发送消息

### Requirement: gRPC 适配器

系统 SHALL 提供 gRPC 协议适配器，支持通过 proto 定义调用 gRPC 服务方法。

#### Scenario: gRPC 一元调用拉取数据

- **WHEN** 系统通过 gRPC 适配器 fetch_data，传入服务地址、proto 定义、方法名和请求消息
- **THEN** 适配器动态加载 proto，创建 gRPC channel，调用方法并返回响应消息转换为 dict

#### Scenario: gRPC 推送数据

- **WHEN** 系统通过 gRPC 适配器 push_data，传入方法名和请求消息
- **THEN** 适配器调用 gRPC 方法，返回响应

### Requirement: 适配器连接测试

系统 SHALL 通过适配器的 test_connection 方法提供统一的连接测试能力。

#### Scenario: 测试连接成功

- **WHEN** 系统调用适配器的 test_connection 方法
- **THEN** 适配器发起轻量级测试请求（如 REST 的 GET /health，GraphQL 的 introspection），返回连接状态和延迟

#### Scenario: 测试连接失败

- **WHEN** 目标系统不可达或认证失败
- **THEN** 适配器返回失败状态和错误信息
