# protocol-adapters Specification

## Purpose
提供统一的协议适配器层，将 REST、SOAP、GraphQL、WebSocket、gRPC 等异构协议抽象为一致的 fetch_data / push_data 接口，使数据同步核心逻辑与具体协议解耦。
## Requirements
### Requirement: 统一协议适配器接口

系统 SHALL 定义统一的协议适配器接口，所有协议适配器 MUST 实现相同的 `fetch_data`、`push_data`、`test_connection` 方法。**核心同步逻辑与探针 MUST 通过该接口调用外部系统，系统 MUST NOT 存在绕过适配器的平行传输路径。**

#### Scenario: 协议适配器工厂创建

- **WHEN** 系统需要与外部系统通信，根据连接配置的协议类型从工厂获取对应适配器
- **THEN** 未注册的协议 MUST 在创建时报错，SHALL NOT 静默退化为 REST

#### Scenario: 统一数据拉取

- **WHEN** 任一协议适配器需要从外部系统拉取数据
- **THEN** 系统 MUST 统一调用该适配器的 `fetch_data` 方法完成

#### Scenario: 统一数据推送

- **WHEN** 任一协议适配器需要向外部系统推送数据
- **THEN** 系统 MUST 统一调用该适配器的 `push_data` 方法完成

#### Scenario: 同步链路经适配器拉取

- **WHEN** 同步模式拉取源数据且源连接 `protocol=graphql`
- **THEN** `route_transfer` MUST 经 `GraphqlAdapter.fetch_data` 完成
- **AND** SHALL NOT 调用任何直连 HTTP 的兼容函数

#### Scenario: 异步链路经适配器推送

- **WHEN** 异步 Worker 推送数据到 `protocol=soap` 的目标
- **THEN** `process_async` MUST 经 `SoapAdapter.push_data` 完成

#### Scenario: 不存在平行传输路径

- **WHEN** 检索应用代码中的对外传输调用
- **THEN** 除 `app/adapters/` 内部实现外，MUST NOT 存在直接构造 HTTP 客户端完成源拉取或目标推送的代码

#### Scenario: 统一数据拉取与推送

- **WHEN** 系统通过适配器调用 `fetch_data` 或 `push_data`
- **THEN** 返回结构 MUST 保持既有约定（拉取为数据本体，推送为 `{status_code, body}`）
- **AND** 认证注入 SHALL 由适配器内部的 `AuthInjector` 完成

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

### Requirement: 适配器配置构造单一入口

系统 MUST 提供唯一的"连接配置 → 适配器配置"构造函数，供运行时（同步/异步）与探针共同调用，保证两者构造出的请求头、解密凭据、变量解析结果一致。

#### Scenario: 探针与运行时同构

- **WHEN** 探针与运行时分别对同一连接执行配置构造
- **THEN** 两者 MUST 产出完全一致的 adapter_config（含解密后的凭据与解析后的端点）

#### Scenario: 映射级覆盖生效

- **WHEN** 字段映射声明了映射级请求头覆盖
- **THEN** 构造函数 MUST 按"映射级 > 连接级 > 认证生成"优先级合并
- **AND** 该优先级在同步、异步、探针三条路径上 SHALL 一致

### Requirement: 协议特有参数承载

系统 SHALL 在 `mapping_config` 中以 `protocol` 子对象承载协议特有参数，SHALL NOT 为此新增数据库列。

#### Scenario: gRPC 方法名与消息参数

- **WHEN** 目标连接 `protocol=grpc`
- **THEN** 适配器所需的 `service_name`、`proto_content` 取自连接配置，方法名取自端点字段，消息参数取自 `mapping_config["protocol"]["args"]`

#### Scenario: GraphQL 语句与变量

- **WHEN** 源连接 `protocol=graphql`
- **THEN** query/mutation 文本与变量 MUST 能从 `mapping_config["protocol"]` 取得

#### Scenario: 参数缺失明确报错

- **WHEN** 协议所需参数在 `mapping_config["protocol"]` 中缺失
- **THEN** 探针 MUST 返回指明缺失键名的错误
- **AND** 运行时 SHALL 记录失败原因而非抛出未处理异常

#### Scenario: REST 与数据库路径不受影响

- **WHEN** `mapping_config` 中不存在 `protocol` 键
- **THEN** 既有 REST 与数据库同步行为 MUST 保持不变

