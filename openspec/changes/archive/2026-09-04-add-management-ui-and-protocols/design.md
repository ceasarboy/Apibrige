## Context

当前 APIBridge 后端已完成数据同步核心链路（sync/async/auto 传输模式、Redis 缓存层、Celery Worker），但仅有一个 `/tasks/{id}/run` API 端点，缺少管理界面和 CRUD API，且 `api_client.py` 硬编码了 HTTP/REST 调用，无法支持 SOAP/GraphQL/WebSocket/gRPC 协议。本设计解决"用户无法通过界面配置"和"协议扩展性不足"两个核心问题。

约束：
- 内网部署，无外网依赖，所有依赖需离线打包
- 遵循 CLAUDE.md 准则：简单优先、外科手术式修改
- 前一个变更 `refine-tech-stack-selection` 的 data-pipeline 核心不变，在其上扩展

## Goals / Non-Goals

**Goals:**
- 提供完整的 Web 管理界面，覆盖连接/映射/任务/日志/监控全部核心操作
- 建立协议适配器架构，新增协议只需实现统一接口，不修改核心同步逻辑
- 敏感认证信息加密存储，查询时脱敏
- 前后端合并部署（Nginx 反向代理），内网单端口访问

**Non-Goals:**
- 不直接调用 Dubbo RPC 协议（需 Java 生态，通过 HTTP 网关或 Triple 协议桥接）
- 不做多租户隔离和权限体系（初期内部使用）
- 不做 API 调试台（非核心价值）
- 不重构已有 data-pipeline 核心逻辑（sync_handler/async_handler/router 保持不变）

## Decisions

### 决策1：协议适配器架构 — 抽象基类 + 工厂模式

**选择**：定义 `ProtocolAdapter` 抽象基类（`fetch_data` / `push_data` / `test_connection`），通过 `ProtocolAdapterFactory.create(protocol)` 按连接配置的协议类型创建适配器实例。

**备选方案**：
- 策略模式直接注入：需要修改调用方代码，侵入性大
- 函数式分发（dict 映射函数）：简单但无法管理适配器状态（如 WebSocket 连接、OAuth2 Token 缓存）

**理由**：适配器可能持有状态（WebSocket 长连接、OAuth2 Token），类比函数更适合。工厂模式隔离创建逻辑，新增协议只需注册新类。

**适配器接口定义**：
```python
class ProtocolAdapter(ABC):
    @abstractmethod
    async def fetch_data(self, endpoint: str, params: dict = None) -> Any: ...

    @abstractmethod
    async def push_data(self, endpoint: str, data: Any, method: str = "POST") -> dict: ...

    @abstractmethod
    async def test_connection(self) -> dict: ...
```

**与现有代码的关系**：`api_client.py` 中的 `fetch_from_api` / `push_to_api` 重构为 `RestAdapter` 的方法。`sync_handler.py` 和 `async_handler.py` 不直接调用 `fetch_from_api`，改为通过工厂获取适配器。

### 决策2：前端架构 — Vite + React 18 + Ant Design 5 + Zustand

**选择**：Vite 构建 + React 18 + Ant Design 5 + Zustand 状态管理 + @ant-design/charts 图表。

**备选方案**：
- Ant Design Pro 全家桶：开箱即用但重，包含大量不需要的模块（权限、国际化），与"简单优先"冲突
- Next.js：SSR 对管理后台无优势，增加复杂度
- UmiJS：约定式路由限制灵活性

**理由**：Vite+React 轻量灵活，Ant Design 组件库足够，Zustand 比 Redux 轻量无 boilerplate。不用 Pro 全家桶避免过度工程。

**前端目录结构**：
```
frontend/
├── src/
│   ├── api/           # API 请求封装（axios + 统一错误处理）
│   ├── components/    # 通用组件
│   ├── pages/
│   │   ├── connections/
│   │   ├── mappings/
│   │   ├── tasks/
│   │   ├── logs/
│   │   └── dashboard/
│   ├── stores/        # Zustand stores
│   ├── layouts/       # ProLayout 侧边栏布局
│   └── App.tsx
├── vite.config.ts
└── package.json
```

### 决策3：认证管理 — 加密中间件 + 适配器注入

**选择**：
- 存储层：`auth_config` JSONB 字段中的敏感值用 AES-256-Fernet 加密后存储，密钥从环境变量读取
- 注入层：`AuthInjector` 类根据 `auth_type` 将认证信息注入到适配器的请求中
- 查询脱敏：API 返回前统一脱敏（密码→`***`，Token→`***`）

**备选方案**：
- 数据库列级加密（pgcrypto）：绑定 PostgreSQL，与数据库可移植性冲突
- 不加密只脱敏：数据库泄露即凭证泄露，不可接受

**理由**：应用层加密兼顾安全与可移植性。`AuthInjector` 独立于适配器，新增认证方式不影响适配器代码。

### 决策4：gRPC 动态调用 — grpcio + 动态 proto 加载

**选择**：使用 `grpcio` + `grpcio-tools`，通过 `grpc_reflection` 或运行时编译 proto 文件实现动态调用。

**备选方案**：
- 预编译 proto：每个 gRPC 服务需提前编译，不灵活
- HTTP/2 直接调用：失去 gRPC 的 Protobuf 编码优势

**理由**：用户在前端配置 proto 文件内容，后端运行时编译加载，无需重启服务。使用 `grpc_tools.protoc` 在运行时编译 proto 字符串为 Python 模块。

### 决策5：监控统计 — SQL 聚合 + Redis 缓存

**选择**：监控统计数据通过 SQL 聚合查询 `execution_logs` 表生成，结果在 Redis 缓存 30 秒（与看板刷新间隔一致）。

**备选方案**：
- Prometheus 自定义指标：需要额外集成 prometheus_client，增加复杂度
- 定时预计算 + 物化视图：过度优化，当前数据量不需要

**理由**：`execution_logs` 表已有索引（task_id, created_at），SQL 聚合足够快。Redis 缓存避免重复计算。

### 决策6：前后端部署 — Nginx 合并部署

**选择**：前端构建产物（dist/）由 Nginx 提供静态服务，`/api/*` 反向代理到 FastAPI 后端，单端口（80/443）访问。

**备选方案**：
- FastAPI StaticFiles 直接服务：简单但无 gzip/缓存控制，生产性能差
- 前后端独立端口：内网部署多端口管理麻烦

**理由**：Nginx 提供高效的静态文件服务、gzip 压缩、缓存控制，反向代理统一入口。

### 决策7：Dubbo 协议接入 — HTTP 网关 + Triple 双模式

**选择**：Python 端无法直接调用 Dubbo RPC（Dubbo 协议基于 Java Hessian2 序列化 + TCP，需 Java 客户端），采用两种桥接模式：
1. **HTTP 网关模式（默认）**：通过 dubbo-go-pixiu / Nginx-dubbo-module / 自研 dubbo-proxy 将 Dubbo 服务暴露为 HTTP 接口，Python 端通过 HTTP/JSON 调用。网关负责协议转换。
2. **Triple 协议模式**：Dubbo 3.x 引入的 Triple 协议基于 HTTP/2 + Protobuf，与 gRPC 兼容，复用 `GrpcAdapter` 调用逻辑。

**备选方案**：
- 直接调用 Dubbo RPC：需要 Python 端实现 Dubbo 协议（Hessian2 序列化 + TCP 长连接 + 注册中心订阅），生态不成熟，dubbo-client-py 项目已停止维护
- 仅支持 Triple：要求 Dubbo 服务必须升级到 3.x，部分老系统不满足

**理由**：HTTP 网关模式兼容 Dubbo 2.x/3.x，部署成本低（网关为独立组件，可复用现有基础设施）。Triple 模式面向新系统，无需额外网关。双模式覆盖主流场景，符合"不引入 Java 依赖"的约束。

**配置约定**:
- gateway 模式: `config["mode"]="gateway"`, `config["base_url"]=网关地址`, `config["service"]=Dubbo 服务接口全名`
- triple 模式: `config["mode"]="triple"`, `config["base_url"]=服务地址`, `config["proto_content"]=.proto 文件内容`

## Risks / Trade-offs

| 风险 | 缓解措施 |
|------|----------|
| gRPC 动态 proto 编译有性能开销 | 编译结果缓存到内存，同一 proto 只编译一次 |
| WebSocket 适配器长连接管理复杂 | 限制 WebSocket 仅用于 fetch（拉取后断开），不做持久连接管理 |
| 前端离线 node_modules 包体积大 | 使用 npm ci --production 只装生产依赖，配合 .npmrc 缓存 |
| zeep（SOAP）同步阻塞 | 在 asyncio 线程池中运行 zeep 同步调用（`asyncio.to_thread`） |
| 加密密钥管理 | 密钥从环境变量 `APIBRIDGE_ENCRYPT_KEY` 读取，不硬编码，部署时配置 |
| 适配器重构影响已有测试 | 保留 `fetch_from_api`/`push_to_api` 函数签名作为 RestAdapter 的薄包装，已有测试不破坏 |
| Dubbo HTTP 网关为外部依赖（需部署 dubbo-go-pixiu 等） | 网关为可选组件，文档明确说明部署要求；Triple 模式无需网关作为备选 |
| Dubbo Triple 模式要求服务端升级到 Dubbo 3.x | gateway 模式兼容 Dubbo 2.x，用户按服务端版本选择模式 |
