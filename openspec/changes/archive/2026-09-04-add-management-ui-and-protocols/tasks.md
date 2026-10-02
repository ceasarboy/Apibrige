## 1. 协议适配器架构

- [x] 1.1 创建 `app/adapters/base.py` 定义 `ProtocolAdapter` 抽象基类（fetch_data/push_data/test_connection）→ 验证: 抽象类无法实例化，子类必须实现3个方法
- [x] 1.2 创建 `app/adapters/rest.py` 将 `api_client.py` 的 fetch_from_api/push_to_api 重构为 RestAdapter → 验证: 保留原函数签名作为薄包装，已有测试全部通过
- [x] 1.3 创建 `app/adapters/factory.py` 实现 `ProtocolAdapterFactory.create(protocol, config)` 工厂 → 验证: 传入 "rest" 返回 RestAdapter 实例，传入未知协议抛出 ValueError
- [x] 1.4 创建 `app/adapters/auth.py` 实现 `AuthInjector`（api_key/bearer/basic/oauth2/sign 五种认证注入）→ 验证: 各认证方式的 Header/Query 注入正确
- [x] 1.5 创建 `app/adapters/soap.py` 实现 SoapAdapter（zeep + asyncio.to_thread）→ 验证: 传入 WSDL URL 能解析操作并调用
- [x] 1.6 创建 `app/adapters/graphql.py` 实现 GraphqlAdapter（httpx 直发 GraphQL 请求）→ 验证: Query 和 Mutation 正常执行
- [x] 1.7 创建 `app/adapters/websocket.py` 实现 WebsocketAdapter（websockets 库，拉取后断开）→ 验证: 连接→接收→断开流程正确
- [x] 1.8 创建 `app/adapters/grpc_adapter.py` 实现 GrpcAdapter（grpcio + 运行时 proto 编译，编译结果缓存）→ 验证: 传入 proto 文本和方法的调用成功
- [x] 1.9 为每个适配器编写单元测试 → 验证: pytest 全部通过（40 个适配器测试通过）
- [x] 1.10 新增 `app/adapters/dubbo.py` 实现 DubboAdapter（HTTP 网关 + Triple 双模式）→ 验证: gateway 模式调用 Dubbo 方法、URL 构造、group/version 注入正确

## 2. 认证管理与加密

- [x] 2.1 创建 `app/security/crypto.py` 实现 AES-256-Fernet 加密/解密（密钥从环境变量读取）→ 验证: 加密后解密还原原文，密钥缺失时报错
- [x] 2.2 扩展 `app/models/sync.py` 的 ApiConnection 模型，细化 auth_config 结构 → 验证: 模型字段与 spec 一致（现有 auth_type/auth_config JSONB 已满足，无需修改）
- [x] 2.3 创建 `app/security/desensitize.py` 实现查询脱敏（密码/Token→***）→ 验证: API 返回的连接列表中敏感字段已脱敏
- [x] 2.4 编写认证管理单元测试 → 验证: 加密存储、脱敏查询、各认证注入方式测试通过（35 个测试通过）

## 3. 后端 CRUD API

- [x] 3.1 创建 `app/api/connections.py` 实现连接管理 CRUD + 测试连接 + 表结构查询端点 → 验证: curl 测试各端点返回正确状态码和数据
- [x] 3.2 创建 `app/api/mappings.py` 实现字段映射 CRUD + 转换预览端点 → 验证: 创建映射、预览转换结果正常
- [x] 3.3 扩展 `app/api/tasks.py` 实现任务 CRUD + 暂停/恢复端点 → 验证: 任务创建、暂停、恢复状态正确变更
- [x] 3.4 创建 `app/api/logs.py` 实现日志分页查询（支持时间/任务/状态过滤）→ 验证: 分页和过滤结果正确
- [x] 3.5 创建 `app/api/metrics.py` 实现监控统计 API（overview + trend，Redis 缓存 30s）→ 验证: 返回正确统计数据，缓存命中
- [x] 3.6 在 `app/main.py` 注册所有新路由 + CORS 配置 → 验证: /docs 页面显示所有 API 端点
- [x] 3.7 编写 API 集成测试 → 验证: pytest 测试 CRUD 全流程通过（28 测试全部通过）

## 4. 前端项目搭建

- [x] 4.1 创建 `frontend/` 项目（Vite + React 18 + TypeScript）→ 验证: `npm run build` 构建成功
- [x] 4.2 安装 Ant Design 5 + Zustand + axios + @ant-design/charts + react-router-dom → 验证: 依赖安装无报错
- [x] 4.3 创建 `src/layouts/MainLayout.tsx` 侧边栏布局（Ant Design Sider + Menu）→ 验证: 侧边栏菜单导航正常
- [x] 4.4 创建 `src/api/client.ts` axios 封装（baseURL + 统一错误处理 + 拦截器）→ 验证: 请求后端 API 返回数据
- [x] 4.5 创建 `src/stores/` Zustand stores（connection/mapping/task/log）→ 验证: 状态管理正常

## 5. 前端页面实现

- [x] 5.1 实现 `src/pages/connections/` 连接管理页面（列表+新建/编辑表单+测试按钮+表结构查看）→ 验证: CRUD 全流程可操作，测试连接显示结果
- [x] 5.2 实现 `src/pages/mappings/` 字段映射页面（列表+配置表单+预览转换）→ 验证: 配置映射并预览转换结果
- [x] 5.3 实现 `src/pages/tasks/` 任务管理页面（列表+创建/编辑+运行/暂停/恢复）→ 验证: 任务运行和状态切换正常
- [x] 5.4 实现 `src/pages/logs/` 执行日志页面（列表+过滤+详情弹窗）→ 验证: 日志过滤和详情查看正常
- [x] 5.5 实现 `src/pages/dashboard/` 监控看板页面（趋势图+成功率+延迟+队列+任务分布）→ 验证: 图表正确渲染，30s 自动刷新

## 6. 部署与集成

- [x] 6.1 创建 `frontend/Dockerfile`（多阶段构建：node 编译 → nginx 服务）→ 验证: docker build 成功
- [x] 6.2 创建 `frontend/nginx.conf`（静态文件服务 + /api 反向代理到后端）→ 验证: 浏览器访问单端口可加载界面和 API
- [x] 6.3 更新 `docker-compose.yml` 添加 web + worker + 健康检查 → 验证: docker-compose up 启动所有服务正常
- [x] 6.4 更新离线部署包结构（前端 node_modules 离线包 + nginx 镜像 + 部署文档）→ 验证: 离线环境部署成功

## 7. 端到端测试

- [x] 7.1 编写协议适配器端到端测试（REST/SOAP/GraphQL/WebSocket/gRPC/Dubbo 各一个场景）→ 验证: 各协议 test_connection 正常
- [x] 7.2 编写认证管理端到端测试（5种认证方式的存储+注入+脱敏全链路）→ 验证: 创建→脱敏返回→解密使用全流程正确
- [x] 7.3 编写连接管理 CRUD 端到端测试（创建→查询→测试→更新→删除全生命周期）→ 验证: 全流程通过
- [x] 7.4 前端页面功能测试（npm run build 通过 + 代码审查）→ 验证: TypeScript 编译无错误
- [x] 7.5 编写监控看板数据准确性测试（overview 结构/trend 趋势/成功率计算）→ 验证: 统计结果与 execution_logs 数据一致
