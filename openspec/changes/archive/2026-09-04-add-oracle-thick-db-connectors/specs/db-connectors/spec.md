## Purpose

定义 APIBridge 数据库连接器层的统一行为：对 Oracle/MySQL/PostgreSQL/SQLite 提供一致的元数据查询与数据读写接口。本能力重点固化 Oracle 11g（目标对接系统版本）的 thick 模式接入、连接复用、批量写入与真实行数语义，杜绝"探针/测试说通了、运行时跑不动"和"静默丢行假成功"两类假信号。

## ADDED Requirements

### Requirement: Oracle 11g thick 模式连接

系统 SHALL 支持通过 python-oracledb thick 模式连接 Oracle Database 11g（含 XE）。连接模式为进程级配置：`APIBRIDGE_ORACLE_CLIENT_MODE=thick` 时 SHALL 在首次 Oracle 连接前加载 `APIBRIDGE_ORACLE_CLIENT_LIB_DIR` 指定的 Instant Client（默认 thin 模式不加载任何客户端库）。

#### Scenario: thick 模式连接 11g 成功

- **WHEN** 配置 thick 模式且 Instant Client 就位，连接器以 host/port/service_name(SID) 连接 Oracle 11g XE
- **THEN** `test_connection` 返回 `status=ok` 且 `latency_ms` 为非负整数

#### Scenario: Instant Client 缺失时明确失败

- **WHEN** thick 模式下 Instant Client 路径无效或库不可加载
- **THEN** 连接器 SHALL 返回包含 `oracle_client_init_failed` 语义的可诊断错误，而非未处理崩溃

#### Scenario: 默认 thin 不加载客户端

- **WHEN** 未配置 `APIBRIDGE_ORACLE_CLIENT_MODE`
- **THEN** 进程 SHALL 以 thin 模式运行，不尝试加载 Instant Client

### Requirement: 11g SQL 兼容分页

对 Oracle 的分页查询 SHALL 使用 `ROWNUM` 双层包裹实现，SHALL NOT 生成 `OFFSET ... ROWS FETCH NEXT ... ROWS ONLY`（12c+ 语法）。

#### Scenario: offset+limit 分页正确

- **WHEN** `fetch_data(table, limit=2, offset=2)` 作用于 5 行种子表
- **THEN** 返回第 3–4 行，SQL 文本不含 `FETCH NEXT`

### Requirement: 连接池复用

系统 SHALL 维护按 `(db_type, 连接配置指纹)` 键控的全局连接池注册表：相同配置的后续操作 MUST 复用既有池，不同配置各自建池；进程 SHALL 提供统一关闭入口。

#### Scenario: 相同配置复用同一池

- **WHEN** 同一 db_config 连续执行两次操作（如 test_connection 两次）
- **THEN** 第二次操作 SHALL 复用已注册池，不新建物理连接池

#### Scenario: 配置变化生成新池

- **WHEN** host 或密码等连接参数变化
- **THEN** 注册表 SHALL 以新指纹创建独立池，不污染原池

### Requirement: 真实写入行数与不静默丢行

`push_data` SHALL 返回驱动报告的真实受影响行数。写入过程中的数据库错误 MUST 上抛，SHALL NOT 以空异常处理器吞掉；SQLite 的 upsert SHALL 使用 `INSERT OR REPLACE` 而非异常后忽略。

#### Scenario: 批量插入返回真实行数

- **WHEN** 对空表 `push_data` 写入 5 行（insert 模式）
- **THEN** 返回值等于 5，且表内实际行数为 5

#### Scenario: 主键冲突上抛而非静默

- **WHEN** insert 模式写入与既有主键重复的行
- **THEN** 连接器 SHALL 抛出含数据库错误信息的异常，不返回伪造的成功计数

### Requirement: MySQL 元数据查询可用

`MysqlConnector.fetch_data` SHALL 正确使用 `aiomysql.DictCursor`（模块导入完整），字典游标不可用时 SHALL 回退为显式列名组装字典。

#### Scenario: fetch_data 不再 NameError

- **WHEN** 以注入的 aiomysql 替身执行 `fetch_data`
- **THEN** 返回字典列表且过程无 NameError
