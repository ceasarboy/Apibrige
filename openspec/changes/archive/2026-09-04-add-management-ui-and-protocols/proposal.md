## Why

当前 APIBridge 仅有后端 API 和一个 `/tasks/{id}/run` 触发接口，**没有管理界面**，用户无法通过界面配置字段映射、API 连接（地址、认证信息等），也无法直观查看同步状态和日志。同时，当前 API 客户端只支持 HTTP/REST，无法对接 SOAP、GraphQL、WebSocket、gRPC 等协议的外部系统，严重限制了数据桥接的适用范围。

## What Changes

### 新增：管理界面（React + Ant Design Pro）
- **连接管理页面**：API/DB 连接的 CRUD + 连接测试 + 数据库表结构预览
- **字段映射页面**：可视化配置源→目标字段映射，支持类型转换预览
- **任务管理页面**：创建/编辑/运行/暂停/删除同步任务，支持 Cron 配置
- **执行日志页面**：查看同步日志、错误详情、重试记录
- **监控看板页面**：同步量趋势、成功率、延迟 P99、队列深度图表

### 新增：后端 CRUD API
- `/api/v1/connections` — 连接管理 CRUD + 测试连接 + 获取表结构
- `/api/v1/mappings` — 字段映射 CRUD + 转换预览
- `/api/v1/tasks` — 任务管理 CRUD + 手动执行 + 暂停/恢复
- `/api/v1/logs` — 执行日志查询（分页 + 过滤）
- `/api/v1/metrics` — 监控统计数据

### 新增：协议适配器架构
- **统一协议接口**：`ProtocolAdapter` 抽象基类，定义 `fetch_data()` / `push_data()` / `test_connection()` 统一接口
- **HTTP 系适配器**：
  - REST（已有，重构为适配器模式）
  - SOAP（zeep 库，支持 WSDL 解析）
  - GraphQL（gql 库，支持查询/变更）
  - WebSocket（websockets 库，支持实时推送）
- **gRPC 适配器**（grpcio + grpcio-tools，支持动态 proto 加载）

### 增强认证管理
- API Key（Header / Query 参数）
- OAuth2.0（Client Credentials / Authorization Code）
- JWT（Bearer Token）
- Basic Auth
- 自定义签名（Sign，支持 MD5/HMAC-SHA256）

### 技术选型权衡分析

| 决策点 | 选项 | 选择 | 理由 |
|--------|------|------|------|
| 前端框架 | React+AntD / Vue+Element / Jinja2 | React+AntD | 设计文档原定方案，Ant Design Pro 提供完整后台模板，生态成熟 |
| 状态管理 | Redux / Zustand / React Context | Zustand | 轻量、API 简洁、无 boilerplate，中型后台够用 |
| 图表库 | ECharts / @ant-design/charts / Recharts | @ant-design/charts | 与 AntD 风格统一，开箱即用 |
| SOAP 库 | zeep / suds / requests手工 | zeep | 异步支持好、WSDL 自动解析、社区活跃 |
| gRPC 方式 | grpcio+proto / HTTP/2直接 | grpcio+proto | 官方方案，支持流式，可通过反射动态调用 |
| GraphQL 库 | gql / sgqlc / requests手工 | gql | 异步支持好、支持订阅、活跃维护 |
| 前端构建 | Vite / Webpack / CRA | Vite | 启动快、HMR 快、配置简单 |
| 前后端部署 | 独立部署 / Nginx合并 / FastAPI静态 | Nginx合并 | 内网部署简单，单端口访问 |

## 非目标

- **Dubbo 协议**：需要 Java 生态（dubbo-python 维护停滞），本变更不实现，后续如需可通过 sidecar 模式对接
- **SaaS 多租户**：初期为内部团队使用，不做租户隔离和计费
- **API 调试台**：类似 Apifox 的单 API 调试功能，不是本平台核心价值
- **数据回溯**：历史数据快照查询，后续版本再做
- **脚本引擎**：自定义 JS/Python 转换脚本，先用配置化映射

## Capabilities

### New Capabilities

- `management-ui`: 管理界面 — 连接管理、字段映射配置、任务管理、执行日志查看的可视化前端
- `protocol-adapters`: 协议适配器 — 统一接口抽象，支持 REST/SOAP/GraphQL/WebSocket/gRPC 协议的数据拉取与推送
- `monitoring-dashboard`: 监控看板 — 同步量、成功率、延迟、队列深度的实时图表展示
- `connection-api`: 连接管理 API — API/DB 连接的 CRUD、测试、表结构查询后端接口
- `auth-management`: 认证管理 — API Key/OAuth2/JWT/Basic Auth/自定义签名的配置与存储

### Modified Capabilities

（无 — 前一个变更 `refine-tech-stack-selection` 的 data-pipeline 规范不变，本变更在其上扩展）

## Impact

### 代码影响
- **新增** `frontend/` — React 前端项目（Vite + Ant Design Pro）
- **新增** `app/adapters/` — 协议适配器层（base + rest + soap + graphql + websocket + grpc）
- **新增** `app/api/connections.py` — 连接管理路由
- **新增** `app/api/mappings.py` — 字段映射路由
- **新增** `app/api/metrics.py` — 监控统计路由
- **修改** `app/api/tasks.py` — 扩展任务 CRUD
- **修改** `app/services/api_client.py` — 重构为适配器工厂模式
- **修改** `app/main.py` — 注册新路由 + 静态文件服务
- **修改** `app/models/sync.py` — 扩展 ApiConnection 模型（认证字段细化）

### 依赖变更
- 后端新增：`zeep`（SOAP）、`gql[all]`（GraphQL）、`websockets`（WebSocket）、`grpcio` + `grpcio-tools`（gRPC）
- 前端新增：React 18、Ant Design 5、Zustand、@ant-design/charts、Vite

### 部署影响
- Docker Compose 新增 `web` 服务（Nginx + 前端构建产物）
- 内网离线部署包需包含前端 node_modules 离线包
