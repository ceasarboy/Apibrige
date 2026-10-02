## Context

目标对接系统（E:\mes-project，MES）的 Oracle 环境已实证为 11g R2：

- `docker-compose.yml:44` 使用 `oracleinanutshell/oracle-xe-11g`（11.2.0.2 XE）
- Nacos `mes-dev.yaml:8`：`jdbc:oracle:thin:@localhost:1521:XE`
- `mes/pom.xml:51` 与 `mes-basic/pom.xml:48`：`ojdbc6 11.2.0.3`（随 11gR2 发布的驱动）
- 全部 mapper 使用 `ROWNUM` 分页，无 12c 语法

调研结论（外部来源见 proposal 关联讨论）：python-oracledb thin 模式需 Oracle 12.1+；asyncio 仅 thin 支持（官方 issue #258，thick 无计划）；thick 需 Instant Client ≥19；19c 客户端官方支持 11.2.0.4–19c 服务端。

现有代码：`OracleConnector` 骨架按 thin 异步写成（从未运行，驱动未安装）；连接器层存在假池、逐行插入、假 rowcount、MySQL NameError 四个缺陷（`docs/project-status.md` P1 #9/#10 关联项）。

约束：内网离线部署、无外网依赖、中文注释、CLAUDE.md（简单优先/外科手术/目标驱动）。

## Goals / Non-Goals

**Goals:**

- 对真实 Oracle 11g 跑通 连接测试 / 读 / 写（分页、批量、upsert）
- 连接器层的连接复用与真实行数语义
- 保持 `DatabaseConnector` 接口与 `db_client`/`sync_handler` 调用方不变

**Non-Goals:**

- thin 模式异步分支、双执行面路由、SQLAlchemy 化、凭据加密（见 proposal 非目标）

## Decisions

### 决策 1：thick 模式 + 同步驱动 + asyncio.to_thread（单进程）

目标库只有 11g，thin 不可用；asyncio 在 thick 下不存在。方案：`oracledb` 同步 API 全部经 `asyncio.to_thread` 执行（与现有 `SqliteConnector` 同款模式），事件循环不被阻塞；并发由同步池的连接数上限（min=1, max=5, increment=1）与线程池共同约束。Oracle 连接非线程安全，单连接同一时刻只执行一个操作——池的 acquire/release 天然保证。

### 决策 2：进程级模式配置 + 懒初始化

`APIBRIDGE_ORACLE_CLIENT_MODE`：`thin`（默认）/`thick`。进程只能一种模式（python-oracledb 硬限制，issue #518），故不做连接级配置的假灵活性。thick 时首次创建 Oracle 池前调用 `init_oracle_client(lib_dir=...)`：模块级标志 + `threading.Lock` 守卫（该调用进程内仅允许一次）；失败抛出含 `oracle_client_init_failed` 前缀的错误。默认 thin 保证未部署 Instant Client 的环境启动与既有测试完全不受影响。

### 决策 3：全局池注册表

`db_client` 每次调用新建 connector 实例，实例级池活不过一次调用。方案：`app/connectors/database.py` 模块级 `_POOL_REGISTRY: dict[tuple, PoolEntry]`，key=`(db_type, config 规范化指纹)`（对 db_config 排序序列化后 sha1；密码参与指纹——密码变更自然产生新池）。Oracle 用 `oracledb.create_pool`（sync）；PostgreSQL 用 `asyncpg.create_pool`；MySQL 用 `aiomysql.create_pool`；SQLite 不池化（文件级单写者，沿用每次短连接）。提供 `close_all_pools()`（FastAPI shutdown 调用；本变更先暴露函数，main.py 挂接属最小一行，纳入任务）。池参数统一 min=1/max=5，不引入新配置项。

### 决策 4：11g 分页用 ROWNUM 双层包裹

```sql
SELECT * FROM (
  SELECT inner_q.*, ROWNUM rn FROM (
    SELECT {cols} FROM {table} [WHERE ...] [ORDER BY ...]
  ) inner_q WHERE ROWNUM <= :hi
) WHERE rn > :lo
```

limit-only（无 offset）保留单层 `ROWNUM <=` 短路。绑定变量传上下界，杜绝拼接注入。

### 决策 5：批量写与 rowcount 语义

| 库 | 写法 | 返回值 |
|---|---|---|
| Oracle insert | `executemany`（绑定数组）+ 单次 commit | `cursor.rowcount`（executemany 后为累计真实值） |
| Oracle upsert/replace | 逐行 `MERGE INTO`（11g 支持） | 逐行 `cursor.rowcount` 求和 |
| MySQL | `executemany`（ON DUPLICATE KEY / REPLACE / INSERT） | `cursor.rowcount` |
| PostgreSQL | `executemany`（asyncpg 原子事务：成功即全量） | 成功时 `len(data_list)` 即真实值 |
| SQLite | 逐行 execute，`rowcount` 累加；upsert 用 `INSERT OR REPLACE` | 累加真实值；异常上抛 |

### 决策 6：测试库与目标同源

测试库直接复用目标系统的镜像与参数：`oracleinanutshell/oracle-xe-11g`、端口 1521、SID `XE`、`system/oracle`（镜像默认）。种子数据由集成测试自建（`APIBRIDGE_ORACLE_TEST=1` 门控，CI/无 Oracle 环境自动跳过）。风险记录：XE 11.2.0.2 低于 client 19 认证下限 11.2.0.4——task 1.3 是首个验证点，若不通则升级讨论（换 wnameless 镜像同为 11.2.0.2，无更老镜像可选；届时评估连接日志再定）。

## Risks / Trade-offs

- **client 19 ↔ server 11.2.0.2 超官方矩阵**：极大概率可通（协议层 11.2 系列无变化），task 1.3 真机验证；生产库小版本需 DBA 确认
- **线程模型吞吐上限**：同步池 max=5，Oracle 路径并发 5；设计目标同步 500 TPS（批量写）余量充足，单条小事务场景约数百 TPS
- **池泄漏**：注册表无 TTL 淘汰；内部工具 + 连接数有限，以 `close_all_pools` + 进程生命周期管理兜底
- **executemany 异常粒度**：批量失败整批上抛（无部分成功），调用方（sync_handler）按失败重试语义处理，与现有行为一致

## Migration Plan

无数据迁移。新增环境变量均有安全默认值（thin / 不加载客户端），既有部署零感知。

## Open Questions

- 生产 MES 的 Oracle 精确小版本（11.2.0.x）与字符集（建议 `ZHS16GBK` 或 `AL32UTF8` 确认）——联调时确认，不影响本变更结构
