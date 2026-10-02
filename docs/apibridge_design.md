# APIBridge - API数据桥接平台完整策划方案

## 一、项目概述

### 1.1 项目定位
**APIBridge** - 一个专注于**双API数据桥接**的中间件平台，通过中间数据库实现两个软件平台间的数据可靠传输。

### 1.2 核心价值主张
| 对比项 | Apifox | APIBridge（本项目） |
|--------|--------|---------------------|
| 定位 | API 设计/调试/测试工具 | **API 数据桥接平台** |
| 核心功能 | 单个 API 的管理和调试 | **两个 API 之间的数据互通** |
| 数据流向 | 单向调试 | **双向数据同步** |
| 数据存储 | 无持久化 | **中间数据库缓存** |

### 1.3 目标用户
- **初期**：内部团队使用（系统集成）
- **后期**：SaaS 产品对外服务（多租户、权限管理）

### 1.4 数据同步模式
| 同步模式 | 源端 | 目标端 | 使用场景 |
|---------|------|--------|----------|
| **API → API** | REST/GraphQL/gRPC | REST/GraphQL/gRPC | 两个SaaS系统数据同步 |
| **DB → API** | MySQL/Oracle/SQLite/PostgreSQL | REST/GraphQL/gRPC | 数据库数据同步到第三方系统 |
| **API → DB** | REST/GraphQL/gRPC | MySQL/Oracle/SQLite/PostgreSQL | 第三方数据落库 |
| **DB → DB** | MySQL/Oracle/SQLite/PostgreSQL | MySQL/Oracle/SQLite/PostgreSQL | 数据库间数据迁移 |

---

## 二、功能模块设计

### 2.1 功能架构图

```
┌─────────────────────────────────────────────────────────────┐
│                      APIBridge 平台                          │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │ 连接管理模块 │  │ 数据映射模块 │  │   任务调度模块      │ │
│  │             │  │             │  │                     │ │
│  │ • API连接配置│  │ • 字段映射   │  │ • 实时同步(Webhook) │ │
│  │ • DB连接配置 │  │ • 格式转换   │  │ • 定时任务(Cron)    │ │
│  │ • 认证管理  │  │ • 数据校验   │  │ • 任务监控          │ │
│  │ • 连接测试  │  │             │  │                     │ │
│  └─────────────┘  └─────────────┘  └─────────────────────┘ │
│                                                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │ 中间数据库   │  │ 日志监控模块 │  │   管理后台模块      │ │
│  │             │  │             │  │                     │ │
│  │ • 数据缓存  │  │ • 操作日志   │  │ • 用户管理          │ │
│  │ • 数据转换  │  │ • 错误追踪   │  │ • 权限管理          │ │
│  │ • 冲突处理  │  │ • 性能监控   │  │ • 租户管理          │ │
│  └─────────────┘  └─────────────┘  └─────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 核心功能清单

#### 模块1：连接管理
| 功能 | 说明 | 优先级 |
|------|------|--------|
| API 连接配置 | 支持 REST/GraphQL/gRPC 等多种协议 | P0 |
| 数据库连接配置 | 支持 MySQL/Oracle/SQLite/PostgreSQL | P0 |
| 认证管理 | API Key、OAuth2.0、JWT、Basic Auth | P0 |
| 连接池管理 | 连接复用、超时控制、重试机制 | P0 |
| 连接测试 | 一键测试连接可用性 | P0 |

#### 模块2：数据映射
| 功能 | 说明 | 优先级 |
|------|------|--------|
| 字段映射配置 | 可视化配置源→目标字段映射 | P0 |
| 数据类型转换 | 自动/手动类型转换 | P0 |
| 数据校验规则 | 必填、格式、范围校验 | P1 |
| 转换脚本 | 支持 JS/Python 脚本自定义转换 | P2 |

#### 模块3：任务调度
| 功能 | 说明 | 优先级 |
|------|------|--------|
| 实时同步 | Webhook 事件驱动实时推送 | P0 |
| 定时同步 | Cron 表达式配置定时任务 | P0 |
| 增量同步 | 基于时间戳/自增ID的增量同步 | P0 |
| 全量同步 | 完整数据批量同步 | P0 |
| 任务编排 | 多任务串联、条件分支 | P1 |
| 失败重试 | 指数退避重试策略 | P0 |

#### 模块4：中间数据库
| 功能 | 说明 | 优先级 |
|------|------|--------|
| 数据缓存 | 临时存储待同步数据 | P0 |
| 数据转换 | ETL 处理管道 | P0 |
| 冲突检测 | 数据版本控制、冲突标记 | P1 |
| 数据回溯 | 历史数据查询 | P1 |

#### 模块5：日志监控
| 功能 | 说明 | 优先级 |
|------|------|--------|
| 操作日志 | 全链路操作记录 | P0 |
| 错误追踪 | 错误分类、告警通知 | P0 |
| 性能监控 | QPS、延迟、成功率 | P1 |
| 数据看板 | 同步量、成功率可视化 | P2 |

#### 模块6：管理后台（SaaS 阶段）
| 功能 | 说明 | 优先级 |
|------|------|--------|
| 用户管理 | 注册、登录、角色 | P1 |
| 权限管理 | RBAC 权限模型 | P1 |
| 租户管理 | 多租户数据隔离 | P2 |
| 计费管理 | 按量计费、套餐管理 | P3 |

---

## 三、技术架构设计

### 3.1 技术栈选型

| 层级 | 技术选型 | 选型理由 |
|------|----------|----------|
| **后端框架** | FastAPI (Python) | 高性能异步框架、自动文档生成、类型提示 |
| **中间数据库** | PostgreSQL | 支持 JSONB、事务、扩展性强 |
| **缓存** | Redis | 任务队列、会话缓存、限流 |
| **消息队列** | RabbitMQ / Redis Stream | 异步任务处理、事件驱动 |
| **任务调度** | Celery + Beat | 分布式任务、定时任务 |
| **API 协议** | HTTP/REST, GraphQL, gRPC | 覆盖主流协议 |
| **数据库驱动** | aiomysql, oracledb, aiosqlite, asyncpg | 异步数据库连接 |
| **前端** | React + Ant Design | 管理后台、可视化配置 |
| **部署** | Docker + K8s | 容器化、弹性伸缩 |

### 3.2 系统架构图

```
┌──────────────────────────────────────────────────────────────────────┐
│                           客户端层                                    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐ │
│  │  管理后台    │  │  API 调试台  │  │  Webhook    │  │  第三方系统  │ │
│  │  (React)    │  │  (React)    │  │  接收端     │  │  (外部API)  │ │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘ │
└─────────┼────────────────┼────────────────┼────────────────┼────────┘
          │                │                │                │
┌─────────┼────────────────┼────────────────┼────────────────┼────────┐
│         ▼                ▼                ▼                ▼        │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │                      API Gateway (Nginx)                     │    │
│  │              路由、限流、认证、负载均衡                        │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                              │                                       │
│  ┌───────────────────────────┼───────────────────────────────────┐  │
│  │                           ▼                                   │  │
│  │  ┌─────────────────────────────────────────────────────────┐  │  │
│  │  │              FastAPI 应用服务层                           │  │  │
│  │  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────────┐   │  │  │
│  │  │  │ 连接管理服务 │ │ 映射管理服务 │ │  任务调度服务    │   │  │  │
│  │  │  └─────────────┘ └─────────────┘ └─────────────────┘   │  │  │
│  │  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────────┐   │  │  │
│  │  │  │ 数据同步服务 │ │ 日志监控服务 │ │  用户权限服务    │   │  │  │
│  │  │  └─────────────┘ └─────────────┘ └─────────────────┘   │  │  │
│  │  └─────────────────────────────────────────────────────────┘  │  │
│  │                              │                                 │  │
│  │  ┌───────────────────────────┼─────────────────────────────┐  │  │
│  │  │                           ▼                             │  │  │
│  │  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │  │  │
│  │  │  │  PostgreSQL  │  │    Redis    │  │  RabbitMQ   │     │  │  │
│  │  │  │  中间数据库  │  │   缓存/队列  │  │  消息队列   │     │  │  │
│  │  │  └─────────────┘  └─────────────┘  └─────────────┘     │  │  │
│  │  └─────────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                              │                                       │
│  ┌───────────────────────────┼───────────────────────────────────┐  │
│  │                           ▼                                   │  │
│  │  ┌─────────────────────────────────────────────────────────┐  │  │
│  │  │              Celery Worker 异步任务处理层                 │  │  │
│  │  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────────┐   │  │  │
│  │  │  │ 数据拉取任务 │ │ 数据推送任务 │ │  数据转换任务    │   │  │  │
│  │  │  └─────────────┘ └─────────────┘ └─────────────────┘   │  │  │
│  │  └─────────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                           APIBridge 服务层                          │
└──────────────────────────────────────────────────────────────────────┘
          │                │                │                │
          ▼                ▼                ▼                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                         外部系统层                                    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐ │
│  │   源系统A   │  │   源系统B   │  │  目标系统X  │  │  目标系统Y  │ │
│  │  (REST API) │  │ (GraphQL)   │  │  (MySQL)    │  │  (Oracle)   │ │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

### 3.3 数据库连接器架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    数据库连接器层 (DB Connectors)                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│  │ MySQL连接池   │  │ Oracle连接池  │  │ SQLite连接池  │         │
│  │ (aiomysql)   │  │ (oracledb)   │  │ (aiosqlite)  │         │
│  └──────────────┘  └──────────────┘  └──────────────┘         │
│  ┌──────────────┐  ┌──────────────┐                           │
│  │ PostgreSQL   │  │ SQL Server   │                           │
│  │ (asyncpg)    │  │ (aioodbc)    │                           │
│  └──────────────┘  └──────────────┘                           │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │              统一数据库接口 (DatabaseConnector)             │ │
│  │  • connect() / disconnect()                              │ │
│  │  • execute_query() / execute_update()                    │ │
│  │  • fetch_all() / fetch_one()                             │ │
│  │  • batch_insert() / batch_update()                       │ │
│  │  • get_table_schema()                                    │ │
│  └──────────────────────────────────────────────────────────┘ │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 四、数据库设计

### 4.1 核心数据表

```sql
-- 1. 租户表（SaaS 阶段）
CREATE TABLE tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) NOT NULL,
    api_key_hash VARCHAR(255) NOT NULL,
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 2. API/DB 连接配置表
CREATE TABLE api_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    name VARCHAR(100) NOT NULL,
    connection_type VARCHAR(20) NOT NULL, -- 'source' or 'target'
    protocol VARCHAR(20) NOT NULL, -- 'rest', 'graphql', 'grpc', 'mysql', 'oracle', 'sqlite', 'postgresql'
    -- API类型配置
    base_url VARCHAR(500),
    auth_type VARCHAR(20), -- 'api_key', 'oauth2', 'jwt', 'basic'
    auth_config JSONB, -- 认证配置（加密存储）
    headers JSONB, -- 自定义请求头
    timeout_ms INT DEFAULT 30000,
    retry_count INT DEFAULT 3,
    -- 数据库类型配置
    db_type VARCHAR(20), -- 'mysql', 'oracle', 'sqlite', 'postgresql'
    db_config JSONB, -- 数据库连接配置
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- db_config示例（MySQL）
-- {
--   "host": "192.168.1.100",
--   "port": 3306,
--   "database": "source_db",
--   "username": "readonly_user",
--   "password": "encrypted_password",
--   "charset": "utf8mb4",
--   "pool_size": 10
-- }

-- 3. 字段映射配置表
CREATE TABLE field_mappings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    name VARCHAR(100) NOT NULL,
    source_connection_id UUID REFERENCES api_connections(id),
    target_connection_id UUID REFERENCES api_connections(id),
    -- API类型使用endpoint，数据库类型使用table
    source_endpoint VARCHAR(200),
    target_endpoint VARCHAR(200),
    source_table VARCHAR(100), -- 数据库类型时使用
    target_table VARCHAR(100), -- 数据库类型时使用
    mapping_config JSONB NOT NULL, -- 字段映射配置
    transform_script TEXT, -- 转换脚本
    -- 数据库同步相关配置
    sync_mode VARCHAR(20) DEFAULT 'full', -- 'full', 'incremental'
    incremental_field VARCHAR(100), -- 增量同步字段
    batch_size INT DEFAULT 1000, -- 批量处理大小
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 4. 同步任务表
CREATE TABLE sync_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    mapping_id UUID REFERENCES field_mappings(id),
    task_type VARCHAR(20) NOT NULL, -- 'realtime', 'scheduled'
    schedule_cron VARCHAR(50), -- Cron 表达式
    webhook_config JSONB, -- Webhook 配置
    status VARCHAR(20) DEFAULT 'active', -- 'active', 'paused', 'error'
    last_run_at TIMESTAMP,
    next_run_at TIMESTAMP,
    last_sync_value VARCHAR(200), -- 增量同步的最新值
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 5. 数据缓存表（中间数据库核心）
CREATE TABLE data_buffer (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    task_id UUID REFERENCES sync_tasks(id),
    source_data JSONB NOT NULL,
    transformed_data JSONB,
    status VARCHAR(20) DEFAULT 'pending', -- 'pending', 'processing', 'completed', 'failed'
    error_message TEXT,
    retry_count INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    processed_at TIMESTAMP,
    expires_at TIMESTAMP -- 数据过期时间
);

-- 6. 执行日志表
CREATE TABLE execution_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    task_id UUID REFERENCES sync_tasks(id),
    buffer_id UUID REFERENCES data_buffer(id),
    action VARCHAR(50) NOT NULL, -- 'fetch', 'transform', 'push'
    status VARCHAR(20) NOT NULL, -- 'success', 'failed'
    request_data JSONB,
    response_data JSONB,
    error_message TEXT,
    duration_ms INT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- 7. 用户表
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(20) DEFAULT 'user', -- 'admin', 'user'
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
```

---

## 五、API接口设计

### 5.1 RESTful API 设计规范

#### URL设计规范

```
# 正确示例
GET    /api/v1/connections          # 获取连接列表
GET    /api/v1/connections/{id}     # 获取单个连接
POST   /api/v1/connections          # 创建连接
PUT    /api/v1/connections/{id}     # 更新连接（全量）
PATCH  /api/v1/connections/{id}     # 更新连接（部分）
DELETE /api/v1/connections/{id}     # 删除连接
```

#### URL命名规则

| 规则 | 说明 | 示例 |
|------|------|------|
| 使用名词 | URL代表资源，不使用动词 | `/users` 而非 `/getUsers` |
| 复数形式 | 资源集合使用复数 | `/orders` 而非 `/order` |
| 小写字母 | URL全部使用小写 | `/user-profiles` |
| 连字符分隔 | 多单词用连字符 | `/user-profiles` 而非 `/userProfiles` |
| 版本管理 | URL中包含版本号 | `/api/v1/users` |
| 嵌套资源 | 最多两层嵌套 | `/users/123/orders` |

#### HTTP状态码规范

| 状态码 | 含义 | 使用场景 |
|--------|------|----------|
| 200 OK | 请求成功 | GET/PUT/PATCH成功 |
| 201 Created | 创建成功 | POST创建资源成功 |
| 204 No Content | 无内容 | DELETE成功 |
| 400 Bad Request | 请求参数错误 | 参数验证失败 |
| 401 Unauthorized | 未授权 | 未登录或token失效 |
| 403 Forbidden | 禁止访问 | 无权限 |
| 404 Not Found | 资源不存在 | 请求的资源不存在 |
| 409 Conflict | 资源冲突 | 重复创建等冲突 |
| 422 Unprocessable | 无法处理 | 业务逻辑错误 |
| 429 Too Many Requests | 请求过多 | 触发限流 |
| 500 Internal Error | 服务器错误 | 系统异常 |

#### 统一响应格式

**成功响应：**
```json
{
  "code": 200,
  "message": "操作成功",
  "data": {
    "id": 123,
    "name": "示例数据",
    "created_at": "2026-01-15T10:30:00Z"
  },
  "timestamp": "2026-01-15T10:35:00Z"
}
```

**列表响应（带分页）：**
```json
{
  "code": 200,
  "message": "操作成功",
  "data": {
    "items": [
      {"id": 1, "name": "项目1"},
      {"id": 2, "name": "项目2"}
    ],
    "pagination": {
      "page": 1,
      "per_page": 20,
      "total": 100,
      "total_pages": 5
    }
  },
  "timestamp": "2026-01-15T10:35:00Z"
}
```

**错误响应：**
```json
{
  "code": 400,
  "message": "请求参数错误",
  "errors": [
    {
      "field": "email",
      "message": "邮箱格式不正确"
    }
  ],
  "timestamp": "2026-01-15T10:35:00Z"
}
```

### 5.2 核心API列表

| 模块 | 接口 | 方法 | 说明 |
|------|------|------|------|
| 连接管理 | `/api/v1/connections` | POST | 创建API/DB连接 |
| | `/api/v1/connections/{id}` | GET/PUT/DELETE | 获取/更新/删除连接 |
| | `/api/v1/connections/{id}/test` | POST | 测试连接 |
| | `/api/v1/connections/{id}/tables` | GET | 获取数据库表列表 |
| | `/api/v1/connections/{id}/schema/{table}` | GET | 获取表结构 |
| 字段映射 | `/api/v1/mappings` | POST | 创建字段映射 |
| | `/api/v1/mappings/{id}` | GET/PUT/DELETE | 获取/更新/删除映射 |
| | `/api/v1/mappings/{id}/preview` | POST | 预览转换结果 |
| 同步任务 | `/api/v1/tasks` | POST | 创建同步任务 |
| | `/api/v1/tasks/{id}` | GET/PUT/DELETE | 获取/更新/删除任务 |
| | `/api/v1/tasks/{id}/run` | POST | 手动执行任务 |
| | `/api/v1/tasks/{id}/pause` | POST | 暂停任务 |
| 数据查询 | `/api/v1/data/buffer` | GET | 查询缓存数据 |
| | `/api/v1/data/logs` | GET | 查询执行日志 |
| Webhook | `/webhook/{tenant_id}/{task_id}` | POST | 接收外部Webhook |

### 5.3 FastAPI代码规范示例

```python
# app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1 import connections, tasks, mappings
from app.core.config import settings

app = FastAPI(
    title="APIBridge",
    description="API数据桥接平台",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# CORS配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(connections.router, prefix="/api/v1/connections", tags=["连接管理"])
app.include_router(mappings.router, prefix="/api/v1/mappings", tags=["字段映射"])
app.include_router(tasks.router, prefix="/api/v1/tasks", tags=["同步任务"])

@app.get("/health")
async def health_check():
    """健康检查接口"""
    return {"status": "healthy", "version": "1.0.0"}
```

---

## 六、技术指标与考核标准

### 6.1 核心性能指标（KPIs）

| 指标类别 | 具体指标 | 目标值 | 告警阈值 | 测试方法 |
|---------|---------|--------|----------|----------|
| **吞吐量** | API请求QPS | ≥ 5,000/s | < 3,000/s | Locust压测 |
| | 数据同步TPS | ≥ 500条/秒 | < 300条/秒 | 实际业务场景测试 |
| | 批量处理吞吐 | ≥ 10,000条/分钟 | < 5,000条/分钟 | 批量同步任务测试 |
| **响应时间** | API平均响应时间 | < 100ms | > 200ms | 压测统计 |
| | P95响应时间 | < 200ms | > 500ms | 压测统计 |
| | P99响应时间 | < 500ms | > 1000ms | 压测统计 |
| | 数据同步延迟 | < 2秒 | > 5秒 | 端到端测试 |
| **并发能力** | 最大并发连接数 | ≥ 1,000 | < 500 | 并发压测 |
| | 单实例并发流 | ≥ 500 | < 200 | WebSocket/SSE测试 |
| **可靠性** | API成功率 | ≥ 99.9% | < 99% | 长期运行监控 |
| | 数据同步成功率 | ≥ 99.5% | < 98% | 业务监控 |
| | 错误率 | < 0.1% | > 1% | 错误日志统计 |
| **资源使用** | CPU使用率 | < 70% | > 85% | 系统监控 |
| | 内存使用率 | < 80% | > 90% | 系统监控 |
| | 数据库连接数 | < 80%最大连接 | > 90%最大连接 | 数据库监控 |

### 6.2 性能基准测试场景

| 测试场景 | 测试条件 | 预期指标 | 验收标准 |
|---------|---------|---------|----------|
| **简单API响应** | GET请求，无数据库查询 | QPS ≥ 10,000 | P99 < 50ms |
| **数据库查询API** | GET请求，单次数据库查询 | QPS ≥ 3,000 | P99 < 200ms |
| **数据同步任务** | 单条数据同步（源→转换→目标） | TPS ≥ 100 | 延迟 < 3秒 |
| **批量数据同步** | 1000条数据批量同步 | 吞吐 ≥ 500条/秒 | 成功率 100% |
| **高并发Webhook** | 500并发Webhook接收 | 无丢失，延迟 < 2秒 | 成功率 ≥ 99.9% |
| **长时间稳定性** | 24小时持续压测 | 无内存泄漏 | 错误率 < 0.1% |

### 6.3 硬件配置基准

| 环境级别 | CPU | 内存 | 存储 | 预期QPS | 适用场景 |
|---------|-----|------|------|---------|----------|
| **开发环境** | 2核 | 4GB | 50GB SSD | 1,000 | 开发调试 |
| **测试环境** | 4核 | 8GB | 100GB SSD | 3,000 | 功能测试 |
| **生产环境（标准）** | 8核 | 16GB | 500GB SSD | 5,000 | 中小规模业务 |
| **生产环境（高性能）** | 16核 | 32GB | 1TB SSD | 10,000+ | 大规模业务 |

### 6.4 SLA/SLO/SLI 定义

#### 服务等级指标（SLI）

| 指标 | 定义 | 测量方法 |
|------|------|----------|
| API可用性 | 成功请求比例 | 成功请求数 / 总请求数 |
| API延迟 | 请求响应时间 | P50/P95/P99响应时间 |
| 数据同步延迟 | 数据从源到目标的时间 | 端到端时间戳差值 |
| 数据一致性 | 源目标数据一致比例 | 对账检查结果 |

#### 服务等级目标（SLO）

| 指标 | 目标值 | 测量周期 |
|------|--------|----------|
| API可用性 | ≥ 99.9% | 月度 |
| API P95延迟 | < 200ms | 实时 |
| 数据同步成功率 | ≥ 99.5% | 月度 |
| 数据同步延迟 | < 5秒 | 实时 |

#### 服务等级协议（SLA）

| 服务级别 | 可用性 | 响应时间 | 错误率 | 补偿方案 |
|---------|--------|----------|--------|----------|
| 标准版 | 99.5% | P95 < 500ms | < 1% | 无 |
| 专业版 | 99.9% | P95 < 200ms | < 0.1% | 服务延期 |
| 企业版 | 99.95% | P95 < 100ms | < 0.05% | 费用减免 |

---

## 七、内网部署方案

### 7.1 内网部署架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        内网环境                                   │
│                                                                 │
│  ┌─────────────┐    ┌──────────────────────────────────────┐   │
│  │  离线镜像仓库 │    │          应用服务器集群                │   │
│  │  (Harbor)   │───▶│  ┌──────────┐  ┌──────────┐         │   │
│  │             │    │  │APIBridge │  │APIBridge │         │   │
│  └─────────────┘    │  │Instance1 │  │Instance2 │         │   │
│                     │  └──────────┘  └──────────┘         │   │
│  ┌─────────────┐    │         │              │             │   │
│  │  制品包仓库   │    │  ┌──────┴──────┐ ┌─────┴─────┐     │   │
│  │  (MinIO)    │    │  │  PostgreSQL │ │   Redis    │     │   │
│  └─────────────┘    │  └─────────────┘ └───────────┘     │   │
│                     │  ┌─────────────┐                    │   │
│  ┌─────────────┐    │  │  RabbitMQ   │                    │   │
│  │  配置中心    │    │  └─────────────┘                    │   │
│  │  (Nacos/    │    └──────────────────────────────────────┘   │
│  │   Consul)   │                                                │
│  └─────────────┘    ┌──────────────────────────────────────┐   │
│                     │          监控平台                      │   │
│  ┌─────────────┐    │  Prometheus + Grafana + AlertManager  │   │
│  │  日志中心    │    │  (全离线部署，无外网依赖)               │   │
│  │  (EFK/Loki) │    └──────────────────────────────────────┘   │
│  └─────────────┘                                                │
└─────────────────────────────────────────────────────────────────┘
```

### 7.2 内网部署要求清单

| 要求项 | 说明 | 实现方式 |
|--------|------|----------|
| **离线安装** | 不依赖外网下载依赖 | Docker离线镜像 + pip离线包 |
| **私有镜像仓库** | 内网镜像管理 | Harbor私有仓库 |
| **私有制品仓库** | 存储离线包、配置文件 | MinIO对象存储 |
| **离线数据库** | 数据库内网部署 | PostgreSQL内网安装 |
| **离线缓存** | 缓存内网部署 | Redis内网安装 |
| **离线消息队列** | 消息队列内网部署 | RabbitMQ内网安装 |
| **内网DNS** | 服务发现 | 内网DNS或/etc/hosts |
| **内网NTP** | 时间同步 | 内网NTP服务器 |
| **内网证书** | HTTPS证书 | 内部CA签发证书 |
| **安全审计** | 操作审计日志 | 审计日志系统 |

### 7.3 离线部署包结构

```
apibridge-deploy-v1.0.0/
├── images/                     # Docker离线镜像
│   ├── apibridge-server.tar
│   ├── apibridge-web.tar
│   ├── postgresql-15.tar
│   ├── redis-7.tar
│   ├── rabbitmq-3.tar
│   ├── prometheus.tar
│   ├── grafana.tar
│   └── harbor.tar
├── packages/                   # 离线依赖包
│   ├── python-packages/        # Python pip离线包
│   └── node-modules/           # 前端离线依赖
├── configs/                    # 配置文件模板
│   ├── docker-compose.yml
│   ├── nginx.conf
│   ├── postgresql.conf
│   ├── redis.conf
│   └── .env.template
├── scripts/                    # 部署脚本
│   ├── install.sh              # 一键安装
│   ├── upgrade.sh              # 升级脚本
│   ├── backup.sh               # 备份脚本
│   ├── restore.sh              # 恢复脚本
│   └── health-check.sh         # 健康检查
├── data/                       # 初始化数据
│   ├── init.sql                # 数据库初始化
│   └── seed-data.json          # 种子数据
├── docs/                       # 部署文档
│   ├── 部署手册.pdf
│   ├── 运维手册.pdf
│   └── FAQ.pdf
└── README.txt                  # 快速开始
```

### 7.4 Docker Compose 内网部署配置

```yaml
# docker-compose.yml
version: '3.8'

services:
  # PostgreSQL 中间数据库
  postgresql:
    image: harbor.internal/apibridge/postgresql:15
    container_name: apibridge-db
    environment:
      POSTGRES_DB: apibridge
      POSTGRES_USER: ${DB_USER}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pg_data:/var/lib/postgresql/data
      - ./data/init.sql:/docker-entrypoint-initdb.d/init.sql
    ports:
      - "127.0.0.1:5432:5432"
    networks:
      - apibridge-net
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USER}"]
      interval: 10s
      timeout: 5s
      retries: 5

  # Redis 缓存/队列
  redis:
    image: harbor.internal/apibridge/redis:7
    container_name: apibridge-redis
    command: redis-server --requirepass ${REDIS_PASSWORD}
    volumes:
      - redis_data:/data
    ports:
      - "127.0.0.1:6379:6379"
    networks:
      - apibridge-net

  # RabbitMQ 消息队列
  rabbitmq:
    image: harbor.internal/apibridge/rabbitmq:3-management
    container_name: apibridge-mq
    environment:
      RABBITMQ_DEFAULT_USER: ${MQ_USER}
      RABBITMQ_DEFAULT_PASS: ${MQ_PASSWORD}
    volumes:
      - mq_data:/var/lib/rabbitmq
    ports:
      - "127.0.0.1:5672:5672"
      - "127.0.0.1:15672:15672"
    networks:
      - apibridge-net

  # APIBridge 后端服务
  server:
    image: harbor.internal/apibridge/server:1.0.0
    container_name: apibridge-server
    environment:
      DATABASE_URL: postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@postgresql:5432/apibridge
      REDIS_URL: redis://:${REDIS_PASSWORD}@redis:6379/0
      RABBITMQ_URL: amqp://${MQ_USER}:${MQ_PASSWORD}@rabbitmq:5672/
      SECRET_KEY: ${SECRET_KEY}
    ports:
      - "8000:8000"
    depends_on:
      postgresql:
        condition: service_healthy
      redis:
        condition: service_started
      rabbitmq:
        condition: service_started
    networks:
      - apibridge-net
    deploy:
      replicas: 2

  # Celery Worker 异步任务
  worker:
    image: harbor.internal/apibridge/server:1.0.0
    container_name: apibridge-worker
    command: celery -A app.celery_app worker -l info -c 4
    environment:
      DATABASE_URL: postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@postgresql:5432/apibridge
      REDIS_URL: redis://:${REDIS_PASSWORD}@redis:6379/0
      RABBITMQ_URL: amqp://${MQ_USER}:${MQ_PASSWORD}@rabbitmq:5672/
    depends_on:
      - server
    networks:
      - apibridge-net
    deploy:
      replicas: 2

  # Celery Beat 定时任务
  beat:
    image: harbor.internal/apibridge/server:1.0.0
    container_name: apibridge-beat
    command: celery -A app.celery_app beat -l info
    environment:
      DATABASE_URL: postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@postgresql:5432/apibridge
      REDIS_URL: redis://:${REDIS_PASSWORD}@redis:6379/0
      RABBITMQ_URL: amqp://${MQ_USER}:${MQ_PASSWORD}@rabbitmq:5672/
    depends_on:
      - server
    networks:
      - apibridge-net

  # 前端管理后台
  web:
    image: harbor.internal/apibridge/web:1.0.0
    container_name: apibridge-web
    ports:
      - "80:80"
    depends_on:
      - server
    networks:
      - apibridge-net

  # Prometheus 监控
  prometheus:
    image: harbor.internal/apibridge/prometheus:latest
    container_name: apibridge-prometheus
    volumes:
      - ./configs/prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus_data:/prometheus
    ports:
      - "127.0.0.1:9090:9090"
    networks:
      - apibridge-net

  # Grafana 监控面板
  grafana:
    image: harbor.internal/apibridge/grafana:latest
    container_name: apibridge-grafana
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_PASSWORD}
    volumes:
      - grafana_data:/var/lib/grafana
    ports:
      - "3000:3000"
    networks:
      - apibridge-net

volumes:
  pg_data:
  redis_data:
  mq_data:
  prometheus_data:
  grafana_data:

networks:
  apibridge-net:
    driver: bridge
```

---

## 八、测试方案

### 8.1 测试策略

| 测试层级 | 覆盖范围 | 工具 | 执行频率 |
|----------|----------|------|----------|
| 单元测试 | 核心函数、工具类 | pytest | 每次提交 |
| 集成测试 | API 接口、数据库交互 | pytest + httpx | 每日构建 |
| 功能测试 | 完整业务流程 | pytest + Playwright | 每周发布 |
| 性能测试 | 并发、吞吐量 | Locust | 每月压测 |

### 8.2 测试平台选型：自研收发比对模块

**选型理由：**
1. **内网部署**是硬性要求，公共平台私有化版本可能收费或有功能限制
2. 本项目的核心是**双API桥接**，公共测试平台无法原生测试"源API→中间件→目标API"的完整链路
3. 数据比对是核心需求，需要自动验证"源数据经过转换后是否与目标数据一致"
4. 模块轻量，开发成本可控

### 8.3 自研测试比对模块设计

#### 模块架构

```
┌─────────────────────────────────────────────────────────────┐
│                   测试比对模块 (TestBridge)                    │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐ │
│  │  数据发送器    │  │  数据接收器   │  │   数据比对引擎    │ │
│  │  (Sender)     │  │  (Receiver)  │  │  (Comparator)    │ │
│  │              │  │              │  │                  │ │
│  │ • 构造测试数据 │  │ • 监听目标API │  │ • 字段级比对     │ │
│  │ • 调用源API   │  │ • 捕获同步数据 │  │ • 类型转换验证   │ │
│  │ • 模拟Webhook │  │ • 数据快照    │  │ • 容差比对      │ │
│  │ • 批量发送    │  │ • 超时检测    │  │ • 差异报告      │ │
│  └──────────────┘  └──────────────┘  └──────────────────┘ │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐ │
│  │  压测引擎     │  │  报告生成器   │  │   测试用例管理    │ │
│  │  (Stress)     │  │  (Reporter)  │  │  (CaseManager)   │ │
│  │              │  │              │  │                  │ │
│  │ • 并发控制    │  │ • HTML报告   │  │ • 用例CRUD      │ │
│  │ • 梯度加压    │  │ • 性能图表   │  │ • 用例导入导出   │ │
│  │ • 持续时间    │  │ • 差异明细   │  │ • 用例分组      │ │
│  │ • 结果统计    │  │ • PDF导出    │  │ • 标签管理      │ │
│  └──────────────┘  └──────────────┘  └──────────────────┘ │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 8.4 单元测试用例

#### 连接管理模块
| 用例ID | 测试场景 | 输入 | 预期输出 |
|--------|----------|------|----------|
| UT-CONN-001 | 创建REST连接 | 有效配置 | 返回连接ID，状态200 |
| UT-CONN-002 | 创建连接-URL无效 | 无效URL | 返回400错误 |
| UT-CONN-003 | 测试连接-成功 | 有效连接 | 返回连接状态正常 |
| UT-CONN-004 | 测试连接-超时 | 超时配置 | 返回超时错误 |
| UT-CONN-005 | 测试连接-认证失败 | 错误凭证 | 返回401错误 |
| UT-CONN-006 | 创建MySQL连接 | 有效配置 | 返回连接ID |
| UT-CONN-007 | 创建Oracle连接 | 有效配置 | 返回连接ID |
| UT-CONN-008 | 获取数据库表列表 | 有效连接 | 返回表名列表 |

#### 字段映射模块
| 用例ID | 测试场景 | 输入 | 预期输出 |
|--------|----------|------|----------|
| UT-MAP-001 | 创建简单映射 | 字段映射配置 | 返回映射ID |
| UT-MAP-002 | 预览转换-成功 | 测试数据 | 返回转换后数据 |
| UT-MAP-003 | 预览转换-类型不匹配 | 类型错误数据 | 返回转换错误 |
| UT-MAP-004 | 日期格式转换 | 各种日期格式 | 统一输出格式 |
| UT-MAP-005 | 嵌套字段映射 | JSON嵌套数据 | 正确映射深层字段 |

#### 数据同步模块
| 用例ID | 测试场景 | 输入 | 预期输出 |
|--------|----------|------|----------|
| UT-SYNC-001 | 实时同步-Webhook触发 | Webhook数据 | 数据写入缓存 |
| UT-SYNC-002 | 定时同步-Cron触发 | Cron配置 | 按时拉取数据 |
| UT-SYNC-003 | 数据推送-成功 | 转换后数据 | 目标API返回成功 |
| UT-SYNC-004 | 数据推送-失败重试 | 推送失败 | 按策略重试 |
| UT-SYNC-005 | 数据推送-最终失败 | 多次重试失败 | 标记失败、告警 |
| UT-SYNC-006 | DB→API同步 | 数据库数据 | 数据推送到API |
| UT-SYNC-007 | API→DB同步 | API数据 | 数据写入数据库 |
| UT-SYNC-008 | DB→DB同步 | 源数据库 | 数据同步到目标数据库 |

### 8.5 集成测试用例

| 用例ID | 测试场景 | 测试步骤 | 预期结果 |
|--------|----------|----------|----------|
| IT-001 | 完整同步流程 | 1.创建连接 2.创建映射 3.创建任务 4.触发同步 | 数据完整同步 |
| IT-002 | Webhook实时同步 | 1.配置Webhook任务 2.模拟发送Webhook | 数据实时同步 |
| IT-003 | 失败重试机制 | 1.模拟目标API故障 2.触发同步 | 自动重试直到成功 |
| IT-004 | 大数据量同步 | 1.同步10000条数据 | 分批处理、不超时 |
| IT-005 | MySQL→API同步 | 1.配置MySQL连接 2.配置API连接 3.创建映射 4.执行同步 | 数据正确同步 |
| IT-006 | API→Oracle同步 | 1.配置API连接 2.配置Oracle连接 3.创建映射 4.执行同步 | 数据正确写入Oracle |

### 8.6 功能测试用例

| 用例ID | 测试场景 | 前置条件 | 测试步骤 | 预期结果 |
|--------|----------|----------|----------|----------|
| FT-001 | 端到端同步 | 源系统和目标系统就绪 | 1.配置连接 2.配置映射 3.启动任务 4.验证数据 | 数据完整同步 |
| FT-002 | 多任务并行 | 配置多个同步任务 | 同时触发多个任务 | 各任务独立执行 |
| FT-003 | 任务暂停恢复 | 任务运行中 | 1.暂停任务 2.恢复任务 | 任务正确暂停恢复 |
| FT-004 | 日志查询 | 有执行记录 | 查询执行日志 | 显示完整日志 |
| FT-005 | 错误告警 | 同步失败 | 触发失败 | 发送告警通知 |
| FT-006 | 数据回溯 | 有历史数据 | 查询历史数据 | 显示历史记录 |

### 8.7 性能测试用例

| 用例ID | 测试场景 | 并发数 | 预期指标 |
|--------|----------|--------|----------|
| PT-001 | API响应时间 | 100 | P99 < 500ms |
| PT-002 | 数据同步吞吐量 | 50 | > 100条/秒 |
| PT-003 | Webhook处理能力 | 200 | 无丢失、延迟<2s |
| PT-004 | 数据库查询性能 | 50 | P99 < 200ms |
| PT-005 | 长时间稳定性 | 20 | 24小时无内存泄漏 |

---

## 九、监控告警体系

### 9.1 监控指标

| 监控维度 | 核心指标 | 告警阈值 | 监控工具 |
|---------|---------|----------|----------|
| 应用层 | QPS/TPS | < 3000/s | Prometheus |
| | P99响应时间 | > 1000ms | Grafana |
| | 错误率 | > 1% | Prometheus |
| 数据层 | 同步延迟 | > 5秒 | 自定义监控 |
| | 队列深度 | > 10000 | Redis监控 |
| | 数据库连接数 | > 80%最大连接 | 数据库监控 |
| 系统层 | CPU使用率 | > 85% | 系统监控 |
| | 内存使用率 | > 90% | 系统监控 |
| | 磁盘使用率 | > 85% | 系统监控 |

### 9.2 告警级别

| 级别 | 定义 | 响应时间 | 通知方式 |
|------|------|----------|----------|
| P0-紧急 | 服务不可用 | 5分钟内 | 电话+短信+邮件 |
| P1-严重 | 核心功能受损 | 15分钟内 | 短信+邮件 |
| P2-一般 | 性能下降 | 1小时内 | 邮件 |
| P3-提示 | 潜在风险 | 4小时内 | 邮件 |

### 9.3 Prometheus 监控配置

```yaml
# configs/prometheus.yml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  - "alert_rules.yml"

alerting:
  alertmanagers:
    - static_configs:
        - targets:
          - alertmanager:9093

scrape_configs:
  # FastAPI 应用监控
  - job_name: 'apibridge-server'
    static_configs:
      - targets: ['server:8000']
    metrics_path: '/metrics'
    scrape_interval: 10s

  # PostgreSQL 监控
  - job_name: 'postgresql'
    static_configs:
      - targets: ['postgresql-exporter:9187']

  # Redis 监控
  - job_name: 'redis'
    static_configs:
      - targets: ['redis-exporter:9121']

  # RabbitMQ 监控
  - job_name: 'rabbitmq'
    static_configs:
      - targets: ['rabbitmq:15692']

  # 节点监控
  - job_name: 'node'
    static_configs:
      - targets: ['node-exporter:9100']
```

```yaml
# configs/alert_rules.yml
groups:
  - name: apibridge-alerts
    rules:
      # API 响应时间告警
      - alert: HighResponseTime
        expr: histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m])) > 0.2
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "API P95响应时间超过200ms"

      # API 错误率告警
      - alert: HighErrorRate
        expr: rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m]) > 0.01
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "API错误率超过1%"

      # QPS 过低告警
      - alert: LowQPS
        expr: rate(http_requests_total[5m]) < 10
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "API QPS过低，可能服务异常"

      # 数据库连接池告警
      - alert: DBConnectionPoolExhausted
        expr: pg_stat_activity_count / pg_settings_max_connections > 0.8
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "数据库连接池使用率超过80%"

      # 同步延迟告警
      - alert: HighSyncLatency
        expr: apibridge_sync_latency_seconds > 5
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "数据同步延迟超过5秒"

      # 队列积压告警
      - alert: QueueBacklog
        expr: rabbitmq_queue_messages > 10000
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "消息队列积压超过10000条"
```

---

## 九·五、实现状态勘误（2026-09-03 实测）

本文档早期版本宣称的部分能力与实现存在出入，以本节为准：

- 数据库表存储使用 SQLAlchemy `JSON` 类型（SQLite 兼容），并非 PostgreSQL 专有 JSONB
- 连接池：早期版本各连接器每次操作新建连接（"假池"）；
  现已实现全局池注册表（Oracle/MySQL/PostgreSQL 按 `db_type+配置指纹` 复用），见 `app/connectors/database.py`
- 增量同步（`incremental_field`）、定时 Cron（`schedule_cron`）、Webhook 接收
  目前**只有字段没有实现**，属后续变更范围
- 多租户/RBAC/计费为远期非目标，代码中无对应实现
- 同步链路传输已统一走协议适配器（`ProtocolAdapterFactory`），
  `api_client.py` 仅保留 Header 构建与变量解析能力

## 九·六、联调探针与待确认清单（已实现）

对标"数据流"定位的核心增量（详见 `openspec/changes/add-agent-bridge-authoring`）：

- **探针内核**（`app/probe/`）：三级可复现验证
  L1 可达 → L2 转换已验证（≤20 条样本、零写入）→ L3 端到端（人工授权、写 1 条回读比对），
  与运行时同一条适配器传输路径；目标受 SSRF 白名单与调用预算约束
- **待确认清单**：探针产物经人批准（单事务建桥）后才生效，
  批准支持就地修改映射（强制重校验）与 L3 授权二次确认
- **agent 入口**：`apibridge` CLI（`app/probe/cli.py`，仅标准库 HTTP），
  说明书见 `skills/apibridge-cli/SKILL.md` 与 `skills/<协议>/SKILL.md`；
  服务端零模型依赖、零 MCP 依赖

## 十、项目里程碑

| 阶段 | 内容 | 交付物 |
|------|------|--------|
| M1 | 基础框架搭建 | FastAPI 骨架、数据库初始化、基础 API |
| M2 | 连接管理模块 | API/DB 连接配置、测试功能 |
| M3 | 字段映射模块 | 可视化映射配置、数据转换 |
| M4 | 同步任务模块 | 实时/定时同步、任务调度 |
| M5 | 管理后台 | React 前端、用户界面 |
| M6 | 测试模块 | 自研收发比对模块、压测脚本 |
| M7 | 监控部署 | Prometheus + Grafana 配置 |
| M8 | 测试优化 | 完整测试覆盖、性能优化 |
| M9 | SaaS 化 | 多租户、权限、计费 |

---

## 十一、风险与应对

| 风险 | 影响 | 应对措施 |
|------|------|----------|
| 外部 API 不稳定 | 同步失败 | 重试机制、熔断降级 |
| 数据格式差异大 | 转换复杂 | 灵活映射配置、脚本扩展 |
| 高并发压力 | 性能瓶颈 | 异步处理、队列缓冲 |
| 数据安全 | 泄露风险 | 加密存储、权限控制 |
| 多租户隔离 | 数据混乱 | 租户ID隔离、行级安全 |
| 数据库兼容性 | 连接失败 | 统一接口抽象、充分测试 |
| 内网环境限制 | 部署困难 | 离线部署包、完整文档 |

---

## 附录：数据库连接器核心代码

```python
# app/connectors/database/base.py - 统一数据库接口
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, AsyncIterator

class DatabaseConnector(ABC):
    """数据库连接器抽象基类"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.host = config.get("host")
        self.port = config.get("port")
        self.database = config.get("database")
        self.username = config.get("username")
        self.password = config.get("password")
        self.pool = None
        self._connected = False
    
    @abstractmethod
    async def connect(self) -> bool:
        """建立数据库连接"""
        pass
    
    @abstractmethod
    async def disconnect(self):
        """关闭数据库连接"""
        pass
    
    @abstractmethod
    async def execute_query(self, sql: str, params: tuple = None) -> List[Dict]:
        """执行查询SQL"""
        pass
    
    @abstractmethod
    async def execute_update(self, sql: str, params: tuple = None) -> int:
        """执行更新SQL，返回受影响行数"""
        pass
    
    @abstractmethod
    async def fetch_one(self, sql: str, params: tuple = None) -> Optional[Dict]:
        """获取单条记录"""
        pass
    
    @abstractmethod
    async def batch_insert(self, table: str, data_list: List[Dict]) -> int:
        """批量插入数据"""
        pass
    
    @abstractmethod
    async def batch_update(self, table: str, data_list: List[Dict], 
                           key_field: str) -> int:
        """批量更新数据"""
        pass
    
    @abstractmethod
    async def get_table_schema(self, table: str) -> List[Dict]:
        """获取表结构"""
        pass
    
    @abstractmethod
    async def get_tables(self) -> List[str]:
        """获取所有表名"""
        pass
    
    @property
    def is_connected(self) -> bool:
        return self._connected


# app/connectors/database/factory.py
from typing import Dict, Any
from .base import DatabaseConnector
from .mysql_connector import MySQLConnector
from .oracle_connector import OracleConnector
from .sqlite_connector import SQLiteConnector
from .postgresql_connector import PostgreSQLConnector

class DatabaseConnectorFactory:
    """数据库连接器工厂"""
    
    SUPPORTED_TYPES = {
        "mysql": MySQLConnector,
        "oracle": OracleConnector,
        "sqlite": SQLiteConnector,
        "postgresql": PostgreSQLConnector,
        "postgres": PostgreSQLConnector,
    }
    
    @classmethod
    def create(cls, db_type: str, config: Dict[str, Any]) -> DatabaseConnector:
        """创建数据库连接器"""
        db_type = db_type.lower()
        if db_type not in cls.SUPPORTED_TYPES:
            raise ValueError(f"不支持的数据库类型: {db_type}，"
                           f"支持: {list(cls.SUPPORTED_TYPES.keys())}")
        
        connector_class = cls.SUPPORTED_TYPES[db_type]
        return connector_class(config)
    
    @classmethod
    def get_supported_types(cls) -> list:
        """获取支持的数据库类型"""
        return list(cls.SUPPORTED_TYPES.keys())
```

---

**文档版本：** v1.0  
**创建日期：** 2026-08-11  
**最后更新：** 2026-08-11  
**作者：** APIBridge 架构组
