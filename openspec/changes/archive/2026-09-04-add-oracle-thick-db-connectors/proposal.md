## Why

平台要支持数据库读写（设计文档 M2 交付物），四种形态中 DB↔API / API↔DB / DB↔DB 都依赖数据库连接器。实测发现连接器层是一个从未运行过的骨架：

1. `oracledb` 与 `aiomysql` 在 requirements.txt 声明了但**从未安装**，任何 Oracle/MySQL 调用当场 `ModuleNotFoundError`。
2. 目标对接系统（E:\mes-project，MES）的 Oracle 实证为 **11g Release 2**：`docker-compose.yml` 用 `oracleinanutshell/oracle-xe-11g` 镜像、Nacos 配置 `jdbc:oracle:thin:@localhost:1521:XE`、驱动 `ojdbc6 11.2.0.3`、全仓 ROWNUM 分页无 12c 语法。而 python-oracledb thin 模式要求 Oracle **12.1+**，**无法连接 11g**。
3. asyncio 支持仅存在于 thin 模式（官方明确 thick 模式无计划，issue #258）——因此 Oracle 必须走 **thick 模式 + 同步驱动 + `asyncio.to_thread`**。
4. 连接器层四个实测缺陷必须一并收编：连接池是假的（每次操作新建连接）、Oracle 逐行插入无批量、`push_data` 返回 `len(data_list)` 而非真实 rowcount（SQLite 分支 `except: pass` 静默丢行）、MySQL `fetch_data` 引用未导入的 `aiomysql.DictCursor`（NameError）。

## What Changes

### 新增：Oracle 11g thick 模式支持
- `OracleConnector` 重写为 thick 模式：`oracledb` 同步驱动 + `asyncio.to_thread` 线程包装，连接经 `oracledb.create_pool`（同步池）复用
- 进程级模式配置：`APIBRIDGE_ORACLE_CLIENT_MODE`（thin|thick，默认 thin 保证无 Instant Client 也能启动）+ `APIBRIDGE_ORACLE_CLIENT_LIB_DIR`；thick 首次 Oracle 连接时懒加载 `init_oracle_client()`（线程锁保护），失败给出可诊断错误
- 11g SQL 兼容：分页从 12c+ 的 `OFFSET ... ROWS FETCH NEXT` 改为 `ROWNUM` 双层包裹
- Instant Client 版本锁定 19（21c/23ai 已放弃 11g 服务端；python-oracledb 要求 client ≥19，19 是唯一交集）

### 修复：连接器硬化（跨库）
- 全局连接池注册表：按 `(db_type, config 指纹)` 缓存池（Oracle/PG/MySQL），进程生命周期内复用；提供 `close_all_pools()` 优雅停机
- Oracle/MySQL 写入改 `executemany` 批量，返回驱动真实 rowcount；SQLite 移除 `except: pass`，冲突上抛、upsert 用 `INSERT OR REPLACE`
- 修复 `MysqlConnector.fetch_data` 缺失 `import aiomysql` 导致的 NameError

### 文档与部署
- `docs/deployment.md` 补 Oracle 段：Instant Client 19 离线打包、两个新环境变量、11g 兼容性说明
- 测试库搭建议录：与目标系统同款 `oracle-xe-11g` 容器配置（见 design.md）

## 技术选型权衡分析

| 决策点 | 选项 | 选择 | 理由 |
|--------|------|------|------|
| Oracle 驱动 | python-oracledb / cx_Oracle / JDBC / ODBC | **python-oracledb** | 官方继任者、活跃维护、UPL 宽松许可；cx_Oracle 2021 年停更 |
| 连接模式 | thin / thick / 双进程双模式 | **thick（单进程）** | 目标库 11g 只能用 thick；thin 需 12.1+；双进程双模式是为"11g 与 12c+ 并存"设计的复杂度，当前无此需求（YAGNI） |
| 异步方式 | thick 下异步（不可能）/ 同步 + to_thread | **同步 + to_thread** | asyncio 仅 thin 支持（官方 issue #258，无计划）；线程包装与现有 SqliteConnector 同款模式 |
| Instant Client 版本 | 11.2 / 12.x / 19 / 21+ | **19** | python-oracledb 要求 ≥19；21+ 放弃 11g 服务端；19 官方支持 11.2.0.4–19c |
| client_mode 配置粒度 | 连接级 / 进程级 | **进程级（环境变量）** | python-oracledb 模式是进程级的（issue #518 未实现），连接级配置是假灵活性 |
| 池的生命周期 | 每次调用建连 / 实例级池 / 全局池注册表 | **全局池注册表** | db_client 每次调用新建 connector 实例，实例级池活不过一次调用；注册表按配置指纹复用 |
| SQLite upsert 语义 | 异常吞掉继续 / INSERT OR REPLACE | **INSERT OR REPLACE** | 吞异常会静默丢行且假报成功；REPLACE 语义与 MySQL REPLACE 对齐 |
| 密码解密时机 | 本变更内做 / 留给 add-agent-bridge-authoring task 1.3 | **不在本变更做** | db_config 明文入库与掩码问题已在彼变更立项，不夹带 |

## 非目标

- 不实现 thin 模式异步 Oracle 分支（目标库 11g 用不上；`client_mode` 配置位已留，将来接 12c+ 时在连接器内部分叉即可）
- 不做双执行面/专用队列路由（那是双进程方案的配套，本变更单进程）
- 不引入 SQLAlchemy core 重写连接器（与现有裸驱动风格不一致）
- 不处理 db_config 的加密存储与掩码回写（属 `add-agent-bridge-authoring` task 1.3）
- 不做连接池指标上报与 Grafana 面板（属孤立指标回收变更）
- 不改 add-agent-bridge-authoring 范围内的 compose 前缀修复（task 1.4）

## Capabilities

### New Capabilities

- `db-connectors`: 数据库连接器 — Oracle/MySQL/PostgreSQL/SQLite 统一元数据与数据读写；Oracle 11g thick 支持、进程级模式配置、连接池复用、批量写与真实 rowcount、11g SQL 兼容

### Modified Capabilities

（无 —— 首个 DB 连接器能力基线；`data-pipeline` 的凭据解密修复归 add-agent-bridge-authoring）

## Impact

- **修改代码**：`app/connectors/database.py`（Oracle 重写 + 池注册表 + SQLite/MySQL 修复）、`app/config.py`（2 个新配置）、`app/services/db_client.py`（池复用与关闭）
- **新增代码**：`tests/test_oracle_thick.py`（真实 11g 集成测试，环境变量门控）
- **修改文档**：`docs/deployment.md`（Oracle 段）；requirements.txt 版本注记
- **配置**：新增 `APIBRIDGE_ORACLE_CLIENT_MODE`、`APIBRIDGE_ORACLE_CLIENT_LIB_DIR`
- **测试影响**：现有 Oracle mock 单测需适配；新增真实 11g 集成测试（门控，CI 无 Oracle 时自动跳过）
- **部署影响**：若启用 Oracle thick，离线包需附带 Instant Client 19 basic-lite（约 75MB）；不启用则零变化
- **风险**：dev 用 XE 11.2.0.2 低于 client 19 官方认证下限（11.2.0.4），需真机验证；生产 MES 库小版本待 DBA 确认 `SELECT * FROM v$version`
