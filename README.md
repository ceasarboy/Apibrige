# APIBridge

**APIBridge 是一个数据交换平台**：让源系统与目标系统之间的业务数据**持续同步**——新增、变更、删除都会被捕获并落到目标端，而不是一次性的 API 调试或数据搬运脚本。

它解决三个问题：

1. **连得上**——统一管理 API 与数据库两端的连接，凭据加密存储，探针先验证再建桥；
2. **转得对**——字段映射 + 字段字典 + 逐条转换，L1/L2/L3 三级验证语义不谎报（L2 不写目标，L3 需人工授权写 1 条真实数据回读比对）；
3. **持续对齐**——全量、增量（水位线）、删除对账三种模式，配 Cron 调度与 Webhook 触发，源变了目标就跟着变。

在同步能力之上，内置一套 **BI 大屏与分析**：连接里的表单数据可以直接变成指标卡片、图表看板与对外投放页，不需要再导出数据到第三方 BI。

---

## 功能总览

### 数据同步核心

- **连接管理**：API（REST / SOAP / GraphQL / WebSocket / gRPC / Dubbo / 明道云）与数据库（Oracle / MySQL / PostgreSQL / SQLite）统一管理；认证方式（api_key / bearer / basic / oauth2 / 签名 / 自定义 Header）凭据 `enc:` 加密存储、对外输出统一脱敏。
- **字段映射与字段字典**：双栏字段选择、类型兼容校验；字段字典控制 readable / writable 与显示名，同步时按字典过滤。
- **同步任务**：
  - 全量（sync，进程内执行）、增量（水位线三重校验 + 轮次 / 租约 / 断点重试）、**删除对账**（按业务键差集清理目标端多余行，含"完整源集合"守卫与空键防护）；
  - Cron 调度（进程内，单 worker 互斥锁）、Webhook 触发（强制 secret）、失败转异步、冲突策略（跳过 / 覆盖）、幂等短路；
  - 明道云（HAP）专用：批量 upsert 计划机（租约 + 幂等重放）、增量拉取（`utime` 水位 + 服务端过滤 + 定期全量对账）。
- **执行日志与失败下钻**：逐条请求 / 响应 / 错误详情，失败行与字段级原因可导出。

### 建桥流程（探针 → 待确认清单）

平台内**唯一**的建桥通路是「提议 → 校验 → 人工批准」：

1. 联调探针 CLI 或管理页发起探针会话：**L1** 连通性 / **L2** 转换正确性（不写目标）/ **L3** 端到端（需人工授权，写 1 条真实数据回读比对）；
2. Schema 推断与映射草稿生成（无模型时退化为同名 + 类型兼容）；
3. 提交至**待确认清单**，由人在管理界面批准后一次性创建 连接 + 映射 + 任务（单事务）。

AI agent 与探针**不可直接创建**桥接；API 目标无契约的结果标记 `L2_partial_no_contract`，不算达标、不得进清单。

### BI 大屏与分析

- **指标定义**：按维度 / 度量 / 过滤 / 计算字段（行级表达式引擎）/ 聚合比 / 维度分组 / TopN 配置业务指标，类型感知校验；数据源支持连接表单、执行日志、数据缓冲与只读自定义 SQL。
- **聚合引擎**：时间桶预聚合（水位线增量推进 + 并发安全）；DB 源大表自动**聚合下推**（百万行毫秒级返回，单一 SQL 执行点、标识符白名单 + 全值绑定，可全局关闭）；HAP 源增量拉取。
- **看板**：72×36 自由画布、任意位置任意大小、多看板、布局编辑、筛选维度下发。
- **图表**：18 种标准图（折线 / 柱 / 饼 / 散点 / 雷达 / 桑基 / 关系 / 树 / 漏斗 / 仪表盘 / 热力 / 词云 / 排行 / 3D 矩阵等）+ KPI 卡。
- **投放模式**：大屏对外投放（视口适配四模式、深浅主题、明细表自动轮播），支持**只读访问授权**——只读令牌（哈希存储）+ 看板白名单 + 行过滤 + 列隐藏，服务端强制。
- **分析工作台**（`/bi/analysis`）：字段树 + 拖拽即席查询、即时出图、另存为看板卡片。
- **监控看板**：任务 / 连接健康度、延迟分位、失败原因归一化 Top N（Prometheus `/metrics` 与 Grafana 示例配置齐备）。

### 安全

- 凭证（auth_config 全量、db 密码、自定义 Header）Fernet 加密存储，密钥仅在本地 `.env`；
- 管理 API 管理令牌鉴权；BI 只读令牌与写权限默认拒绝分离；
- Webhook 强制 secret（恒定时间比较）；探针内置 SSRF 防线（DNS 预解析 + 内网段拒绝 + 白名单豁免，开关可控）；
- 对外输出（日志、响应、样本、指标标签）统一脱敏。

## 快速开始

### 方式一：Windows 便携包（推荐，开箱即用）

包内自带 Python 3.12 运行时、Redis、Oracle Instant Client 与已编译前端，无需安装环境：

```cmd
:: 解压到可写的英文目录后
start.cmd       rem 启动（后端 + Redis + 前端）
status.cmd      rem 查看状态
stop.cmd        rem 停止
```

打开 `http://127.0.0.1:3000`，把 `.env` 里 `APIBRIDGE_ADMIN_TOKEN=` 的值填到页面右上角「Token」即可。首次启动自动生成随机令牌、加密密钥与全新数据库；备份时同时保留 `.env`、`launcher.json` 与 `data\`。更多要点（VC++ 运行库、Oracle thick 模式、升级迁移、端口修改）见本文件末尾「便携包要点」。

### 方式二：源码运行

```bash
pip install -r requirements.txt
# 后端（SQLite 起步；BI 需要再开 APIBRIDGE_BI_ENABLED / APIBRIDGE_BI_AGGREGATION_ENABLED）
uvicorn app.main:app --host 127.0.0.1 --port 8000
# 前端（开发模式 3000 端口，代理到 8000）
cd frontend-source && npm ci && npm run dev
# 或构建生产前端（产物落 web/，由 server.py 一并托管）
cd frontend-source && npm ci && npm run build
```

### 方式三：Docker

`docker-compose.yml` 提供 app + PostgreSQL + Redis 编排（`APIBRIDGE_DATABASE_URL=postgresql+asyncpg://...`，时间列已做 asyncpg 兼容归一）。

## 测试与质量

```bash
python -m pytest tests/ -q     # 1144 passed / 13 skipped；测试自足（临时 SQLite + 测试密钥）
```

- 五轮代码审查闭环（4 P0 + 23 P1 + 38 P2/P3，含对修复 diff 的复审），记录见 `MERGE-REPORT.md` §3.1–3.4；
- PostgreSQL 部署级真机验证与明道云 API 参数矩阵实测，记录见 `VALIDATION.md`；
- 验证语义诚实：测试不 mock 掉核心链路，`L2_partial_no_contract` 不算达标。

## 文档索引

| 文档 | 内容 |
|------|------|
| `AGENTS.md` | 工程须知：硬约束、环境陷阱清单（58 条）、代码地图、工作流 |
| `docs/bi-dashboard-guide.md` | BI 大屏使用说明（启用、看板、投放、授权） |
| `docs/trial-guide.md` | 试用阶段须知（当前阶段：**开发冻结、测试试用**） |
| `incremental-sync.md` / `hap-bulk-upsert.md` / `manual-watermark.md` / `execution-log-rules.md` | 同步专项说明 |
| `MERGE-REPORT.md` / `VALIDATION.md` | 合并与五轮审查记录 / 逐轮验证记录 |
| `openspec/` | 13 个能力域的规格基线（改行为前先读对应 spec） |

## 目录结构

```
app/            后端（FastAPI：api/adapters/connectors/probe/services/security/cache）
frontend-source/  前端源码（React 19 + antd v5 + ECharts，构建产物落 web/）
tests/          pytest 全量测试
openspec/       能力域规格基线
docs/           设计与使用说明
scripts/        冒烟与运维脚本
skills/         探针 CLI 与 BI 配置 skill
web/            已编译前端（便携包内）
runtime/ redis/ oracle/   便携包运行时（仅发行包携带）
```

## Windows 便携包要点

- 解压到**可写英文目录**；无 VC++ 运行库先跑 `prerequisites\VC_redist.x64.exe`；
- Oracle 默认 thick 模式兼容 11g（Instant Client 19 随包）；仅连 12.1+ 可改 `.env` 为 thin；
- 探针访问内网目标需在 `.env` 的 `APIBRIDGE_PROBE_ALLOWED_HOSTS` 加白名单；
- 升级旧目录：stop → 备份 `.env` / `launcher.json` / `data` → 拷入新包 → `migrate.cmd` → start；勿用新密钥覆盖旧 `.env`（已存凭据将无法解密）；
- 端口冲突：改 `launcher.json`（改 Redis 端口需同步 `.env` 的 `APIBRIDGE_REDIS_URL`）；
- 任务请用 `sync` 模式；`async` / 触发异步分支的 `auto` 需自行部署 RabbitMQ + Celery Worker；
- 便携包面向单机内网；Redis Windows 移植定位开发测试，高可用请用 Linux Redis。

## 当前阶段

**开发冻结、测试试用中**（自 2026-10-01）：试用期问题统一记录、集中修复，详见 `docs/trial-guide.md`。
