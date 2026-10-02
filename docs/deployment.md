# APIBridge 离线部署指南

## 前置条件
- 内网 Docker 环境（Docker Compose v2+）
- 离线镜像已预加载（见下方准备步骤）

## 离线镜像准备（在联网环境执行一次）

```bash
# 1. 拉取所有镜像
docker pull postgres:16-alpine
docker pull redis:7-alpine
docker pull rabbitmq:3-alpine
docker pull python:3.13-slim
docker pull node:20-alpine
docker pull nginx:alpine

# 2. 导出镜像
docker save postgres:16-alpine redis:7-alpine rabbitmq:3-alpine \
  python:3.13-slim node:20-alpine nginx:alpine \
  -o apibridge-images.tar

# 3. 将 apibridge-images.tar 和项目代码拷贝到内网
```

## 内网部署步骤

```bash
# 1. 加载镜像
docker load -i apibridge-images.tar

# 2. 生成加密密钥（必须）
ENCRYPT_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
echo "APIBRIDGE_ENCRYPT_KEY=$ENCRYPT_KEY" > .env

# 3. 构建前端（内网无 npm registry，使用预装依赖）
#    方案A: 在联网环境预先构建
cd frontend && npm install && npm run build
#    将 dist/ 目录和 node_modules/ 一起拷贝到内网

#    方案B: 使用 Docker 多阶段构建（需基础镜像已加载）
#    docker-compose build web

# 4. 启动所有服务
docker-compose up -d

# 5. 验证
curl http://localhost:8000/health
curl http://localhost/
```

## 端口说明
| 端口 | 服务 | 用途 |
|------|------|------|
| 80 | Nginx | 前端界面 + API 代理 |
| 8000 | FastAPI | 后端 API（直连） |
| 5432 | PostgreSQL | 数据库 |
| 6379 | Redis | 缓存 |
| 5672 | RabbitMQ | 消息队列 |
| 15672 | RabbitMQ | 管理界面 |

## 环境变量
在 `.env` 文件中配置以下变量：

| 变量 | 说明 | 示例 |
|------|------|------|
| APIBRIDGE_ENCRYPT_KEY | Fernet 加密密钥 | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| APIBRIDGE_DATABASE_URL | (可选) 覆盖数据库连接 | `postgresql+asyncpg://user:pass@host/db` |
| APIBRIDGE_REDIS_URL | (可选) 覆盖 Redis 连接 | `redis://host:6379/0` |
| APIBRIDGE_RABBITMQ_URL | (可选) 覆盖 RabbitMQ 连接 | `amqp://user:pass@host:5672/` |

> 注意：所有后端环境变量必须带 `APIBRIDGE_` 前缀（`app/config.py` 的 `env_prefix`），
> 无前缀的写法会被 Pydantic 忽略并回落默认值。
| APIBRIDGE_ORACLE_CLIENT_MODE | (可选) Oracle 客户端模式 thin/thick，默认 thin | `thick`（目标库为 11g 时必须） |
| APIBRIDGE_ADMIN_TOKEN | (必填于生产) 管理 API 共享 token；空值生产模式拒绝启动 | 随机长字符串 |
| APIBRIDGE_PROBE_ALLOWED_HOSTS | (可选) 探针私网目标显式白名单（逗号分隔） | `10.0.0.5,192.168.1.1` |
| APIBRIDGE_PROBE_MAX_BODY_BYTES | (可选) 探针响应体上限 | `1000000` |
| APIBRIDGE_PROBE_BUDGET | (可选) 单会话对同一目标的调用预算 | `10` |
| APIBRIDGE_PROBE_SESSION_TTL_DAYS | (可选) 联调会话过期天数 | `7` |
| APIBRIDGE_ORACLE_CLIENT_LIB_DIR | (可选) Instant Client 解压目录，空则用系统 PATH | `/opt/instantclient_19_25` |

## apibridge CLI（属工程师开发机，非服务端组件）

`app/probe/cli.py` 是外部 agent/工程师本机的联调入口，**不进生产镜像、不进离线包**；
它只经 HTTP 调用服务端，使用方式见 `skills/apibridge-cli/SKILL.md`。

## Oracle 连接（目标系统为 11g 时必读）

目标对接系统（MES）的 Oracle 为 **11g Release 2**，python-oracledb thin 模式
只能连 12.1+，因此必须启用 **thick 模式**：

1. **Instant Client 版本必须为 19**：21c/23ai 已放弃 11g 服务端支持，而
   python-oracledb 要求客户端 ≥19，19 是唯一同时满足两头的版本
   （官方支持连 11.2.0.4–19c 服务端）。

2. **离线打包**（联网环境执行一次）：

```bash
# Python 驱动 wheel（thin/thick 同一包）
pip download oracledb -d packages/ --platform manylinux2014_x86_64 --python-version 3.13 --only-binary :all:

# Instant Client 19 basic-lite（约 75MB，UPL 许可允许再分发）
# 下载 instantclient-basiclite-linux.x64-19.*.zip 解压到部署机 /opt/instantclient_19_25
```

3. **启用 thick 模式**（`.env` 或环境变量）：

```
APIBRIDGE_ORACLE_CLIENT_MODE=thick
APIBRIDGE_ORACLE_CLIENT_LIB_DIR=/opt/instantclient_19_25
```

首次 Oracle 连接时懒加载客户端库；加载失败会返回
`oracle_client_init_failed` 的可诊断错误（不需要 Instant Client 的部署
保持默认 thin，启动与运行零影响）。

4. **测试库搭建**（与目标系统同源，开发机执行；**实测可行路径**）：

```bash
# 直接 docker pull 会因 CF 层下载被 RST / containerd GC 竞态失败，
# 用 skopeo 绕开（一次性安装：apt install skopeo 或 Windows 侧等价操作）
skopeo copy docker://registry.cn-hangzhou.aliyuncs.com/qida/oracle-xe-11g:latest \
  docker-archive:/tmp/oracle-xe-11g.tar:qida/oracle-xe-11g:latest
docker load -i /tmp/oracle-xe-11g.tar
docker run -d --name apibridge-oracle-test -p 1521:1521 \
  -e ORACLE_DISABLE_ASYNCH_IO=true -e ORACLE_ALLOW_REMOTE=true \
  qida/oracle-xe-11g:latest
# 默认账密 system/oracle，SID XE；首次初始化约需 2–4 分钟
# 跑真实集成测试：APIBRIDGE_ORACLE_TEST=1 pytest tests/test_oracle_thick.py -v
```

**兼容性实测记录（2026-09-03）**：Instant Client 19.25（Windows x64）↔ Oracle
11.2.0.2 XE（Docker）连接、元数据、ROWNUM 分页、executemany 批量写、MERGE
upsert 全部通过（9/9 集成用例）。低于认证矩阵下限 11.2.0.4 的版本亦可工作，
生产库仍建议按官方矩阵确认。

5. **兼容性注意**：11g 不支持 12c+ 的 `OFFSET ... FETCH` 分页语法，连接器
   已统一使用 ROWNUM 实现；联调前建议向 DBA 确认生产库小版本
   （`SELECT * FROM v$version`，11.2.0.4+ 与 client 19 完全兼容）。
