## 1. 测试环境（目标系统同源）

- [x] 1.1 WSL docker 拉起与目标同款 `oracle-xe-11g`（端口 1521、SID XE、`ORACLE_DISABLE_ASYNCH_IO=true`） → 验证: `v$version` = Oracle Database 11g Express Edition Release 11.2.0.2.0，实例 XE 状态 OPEN（镜像经 skopeo 下载 docker-archive 后 `docker load`，见 scripts/get_oracle_image.sh；直接 pull 会因 CF 层下载 RST / containerd GC 竞态失败）
- [x] 1.2 安装 python-oracledb（4.x）+ 解压 Instant Client 19 basic-lite → 验证: `import oracledb` 成功；`init_oracle_client(lib_dir=...)` 加载成功（实测 client 19.25.0.0.0，`is_thin_mode()=False`）
- [x] 1.3 thick 模式真连 11g 冒烟（connect + SELECT 1 FROM DUAL） → 验证: 冒烟脚本 PASS（test_connection ok 117ms + 元数据 + MERGE 读写回读一致）；**client 19.25 ↔ server 11.2.0.2 实测兼容**

## 2. Oracle thick 连接器

- [x] 2.1 `config.py` 新增 `oracle_client_mode`（thin|thick，默认 thin）与 `oracle_client_lib_dir` → 验证: 缺省实例化不变；非法 mode 值启动即报错（实测 `APIBRIDGE_ORACLE_CLIENT_MODE=bogus` → pydantic literal_error）
- [x] 2.2 `OracleConnector` 改为同步驱动 + `asyncio.to_thread`，首次使用前经守卫的 `init_oracle_client`（线程锁 + 模块级标志） → 验证: Instant Client 缺失时返回含 `oracle_client_init_failed` 的可诊断错误而非未处理崩溃（单测 test_thick_init_failure_raises_diagnosable）
- [x] 2.3 连接经 `oracledb.create_pool` 同步池（min=1, max=5），acquire/release 包裹每次操作 → 验证: 注册表单测断言同配置复用；集成 9 用例全程同池往返真实 11g
- [x] 2.4 分页改 ROWNUM 双层包裹（offset+limit 与 limit-only 两形态），边界用绑定变量 → 验证: 对 5 行种子表 `fetch_data(limit=2, offset=2)` 返回第 3–4 行（test_fetch_pagination_offset_limit），SQL 不含 `FETCH NEXT`
- [x] 2.5 写入改 `executemany`（insert）与逐行 MERGE（upsert/replace），返回真实 rowcount，单次 commit → 验证: 空表插 5 行返回 5 且表内 5 行（test_push_insert_batch_real_rowcount）；MERGE 更新+新增返回真实计数（test_push_upsert_merge）；修复 ORA-38104（ON 键列不得进 UPDATE SET）
- [x] 2.6 保持现有大小写策略（标识符大写、结果列名小写）与 `_validate_identifier` 注入防护 → 验证: 非法标识符用例继续通过（test_injection_rejected + 全量回归）

## 3. 连接器硬化（跨库）

- [x] 3.1 模块级 `_POOL_REGISTRY`（key = `(db_type, sha1(规范化 db_config))`）+ `close_all_pools()`，PG/MySQL/Oracle 接入 → 验证: 同配置两次操作命中同池；改密码产生新池；close 后注册表清空（test_pool_registry 5 项单测）
- [x] 3.2 修复 `MysqlConnector.fetch_data` 缺失 `import aiomysql` 的 NameError（DictCursor 局部导入 + 字典组装兜底） → 验证: 注入 aiomysql 替身后 `fetch_data` 返回字典列表无异常（test_dictcursor_path_no_nameerror）
- [x] 3.3 MySQL 连接经 `aiomysql.create_pool` 复用 → 验证: 同配置两次操作同池（注册表单测，mysql 类型条目）
- [x] 3.4 PostgreSQL 经 `asyncpg` 池复用（消除"每次 `asyncpg.connect`"假池） → 验证: 同配置两次操作同池（注册表单测，异步工厂条目）
- [x] 3.5 SQLite 移除 `except: pass`：insert 逐行累计 rowcount、异常上抛；upsert 改 `INSERT OR REPLACE` → 验证: 主键冲突时 push 抛错且不返回伪造计数；upsert 语义正确（test_sqlite_push 3 项）
- [x] 3.6 FastAPI shutdown 挂接 `close_all_pools` → 验证: `close_all_pools` 清空注册表实测通过；main.py lifespan 关闭段已挂接

## 4. 测试与文档

- [ ] 4.1 单测：ROWNUM SQL 生成（两形态、绑定变量、无 12c 语法）、client 初始化守卫（并发单次）、池注册表命中/指纹隔离/关闭、MySQL NameError 回归、SQLite 冲突上抛 → 验证: 新增用例全绿
- [x] 4.2 集成测试 `tests/test_oracle_thick.py`（`APIBRIDGE_ORACLE_TEST=1` 门控）：test_connection / get_table_list / get_table_schema / fetch 分页 / push 批量与 upsert / 真实 rowcount → 验证: 对真实 11g XE **9/9 全绿**（11.2.0.2 实测）；无环境时 skip
- [x] 4.3 全量回归 `pytest -q` → 验证: 全绿，记录用例数变化（基线 307 → 326 passed + 9 skipped，新增 19 单测；集成用例门控跳过）
- [x] 4.4 `requirements.txt` 注记 oracledb 版本策略；`docs/deployment.md` 补 Oracle 段（Instant Client 19 离线打包、两个新环境变量、11g 兼容说明、测试库搭建命令） → 验证: 按文档可在离线机完成 Oracle thick 部署
- [ ] 4.5 `openspec validate add-oracle-thick-db-connectors --strict` → 验证: 校验通过
