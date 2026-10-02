## Purpose

提供统一的认证管理能力，支持 API Key、OAuth2.0、JWT、Basic Auth、自定义签名等多种认证方式的配置、加密存储和自动注入，使协议适配器无需关心认证细节。

## ADDED Requirements

### Requirement: 认证配置存储

系统 SHALL 将认证配置（API Key、密钥、密码等敏感信息）加密存储在数据库中，查询时脱敏返回。

#### Scenario: 存储认证配置

- **WHEN** 用户创建或更新连接时填写了认证类型和凭证
- **THEN** 系统使用 AES-256 加密敏感字段后存储，数据库中不出现明文

#### Scenario: 查询时脱敏

- **WHEN** 用户通过 API 或界面查询连接列表
- **THEN** 密码、API Key、密钥等字段返回脱敏值（如 `***`），不泄露完整凭证

### Requirement: API Key 认证

系统 SHALL 支持 API Key 认证方式，支持通过 Header 或 Query 参数传递。

#### Scenario: Header 方式 API Key

- **WHEN** 连接配置 auth_type=api_key，auth_config 含 {key_name: "X-API-Key", key_value: "xxx", location: "header"}
- **THEN** 适配器在请求时自动在 Header 中添加 `X-API-Key: xxx`

#### Scenario: Query 参数方式 API Key

- **WHEN** 连接配置 auth_type=api_key，auth_config 含 {key_name: "api_key", key_value: "xxx", location: "query"}
- **THEN** 适配器在请求 URL 中自动添加 `?api_key=xxx`

### Requirement: Bearer Token / JWT 认证

系统 SHALL 支持 Bearer Token（JWT）认证方式。

#### Scenario: Bearer Token 注入

- **WHEN** 连接配置 auth_type=bearer，auth_config 含 {token: "eyJhbG..."}
- **THEN** 适配器在请求时自动在 Header 中添加 `Authorization: Bearer eyJhbG...`

### Requirement: Basic Auth 认证

系统 SHALL 支持 HTTP Basic Authentication。

#### Scenario: Basic Auth 注入

- **WHEN** 连接配置 auth_type=basic，auth_config 含 {username: "admin", password: "pass"}
- **THEN** 适配器在请求时自动在 Header 中添加 `Authorization: Basic <base64(user:pass)>`

### Requirement: OAuth2.0 认证

系统 SHALL 支持 OAuth2.0 Client Credentials 模式，自动获取和刷新 Access Token。

#### Scenario: 获取 OAuth2 Token

- **WHEN** 连接配置 auth_type=oauth2，auth_config 含 {token_url, client_id, client_secret, scopes}
- **THEN** 适配器首次请求前自动向 token_url 获取 Access Token，并缓存 Token 直到过期

#### Scenario: Token 自动刷新

- **WHEN** 缓存的 OAuth2 Token 已过期
- **THEN** 适配器自动重新获取新 Token，用新 Token 重试请求

### Requirement: 自定义签名认证

系统 SHALL 支持自定义签名（Sign）认证，支持 MD5 和 HMAC-SHA256 算法。

#### Scenario: MD5 签名

- **WHEN** 连接配置 auth_type=sign，auth_config 含 {algorithm: "md5", secret_key: "xxx", sign_fields: ["timestamp", "nonce", "data"]}
- **THEN** 适配器按配置的字段和密钥计算 MD5 签名，将签名和参与签名的字段加入请求

#### Scenario: HMAC-SHA256 签名

- **WHEN** 连接配置 auth_type=sign，auth_config 含 {algorithm: "hmac-sha256", secret_key: "xxx", sign_fields: ["timestamp", "method", "path"]}
- **THEN** 适配器按配置计算 HMAC-SHA256 签名，将签名加入请求 Header
