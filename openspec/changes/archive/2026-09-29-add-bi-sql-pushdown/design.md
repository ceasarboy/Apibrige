# add-bi-sql-pushdown 技术设计

> 设计输入：Explore 子代理源码级调研（2026-09-27）——驱动原语清单、方言差异表、
> 回退策略、风险清单。核心结论直接引用如下。

## 1. 驱动原语现状（调研结论摘要）

- 标识符校验 `_validate_identifier`：`^[a-zA-Z_][a-zA-Z0-9_]*$`（**纯 ASCII**——
  中文/空格/点号列名一律无法进 SQL → 回退内存）；建议顺手把 `$` 锚改 `\Z`。
- `fetch_data` 支持：投影 / `where`（仅等值）/ `where_gt`（仅严格 `>`）/ `order_by`
  （单列**仅升序**）/ `limit`/`offset`（int 字面量）。**无 GROUP BY/聚合/IN/BETWEEN/LIKE/DESC**。
- 方言：SQLite `"`+`?`+`LIMIT/OFFSET`（offset 必须配 limit，否则 `LIMIT -1 OFFSET n`）；
  PG `"`+`$n`+`LIMIT/OFFSET`；MySQL `` ` ``+`%s`+`LIMIT/OFFSET`；
  **Oracle 11g** `"`+大写+命名绑定+ROWNUM 双层（禁用 12c OFFSET FETCH，已有单测断言），
  外层 `SELECT *` 会把 `rn` 泄漏进结果，别名受 30 字节限制。

## 2. `fetch_grouped` 接口（新方法）

```python
async def fetch_grouped(
    self, table: str,
    group_fields: list[str],                 # 维度列（已过白名单），空=无维度汇总
    aggs: list[dict],                        # [{"key","func","field"}]
    filters: list[dict] | None = None,       # [{"field","op","value"}]（eq/ne/gt/ge/lt/le/in/between）
    order_by: list[tuple[str, str]] | None = None,
    limit: int | None = None, offset: int | None = None,
) -> list[dict]:                             # 键 = group_fields 原名(经逻辑映射) + aggs[].key
```

- 算子映射：`count→COUNT(*)`、`count_distinct→COUNT(DISTINCT col)`、`sum/avg/min/max→大写函数`；
  别名统一 `a0..an`（**不用 measure key**，规避 Oracle 30 字节）；驱动 Python 层
  按逻辑键重命名返回（不依赖 description 大小写）。
- 比较模板为模块级不可变常量字典（`eq:"= %s"` / `ge:">= %s"` / `between:"BETWEEN %s AND %s"` …），
  值全绑定；标识符白名单。**若 Mimosa 仍拦截**（AGENTS 陷阱 17）：备选路线为
  SQLAlchemy Core `select(func.sum(...)).group_by(...)` **仅编译方言**后取字符串，
  不引入执行依赖。
- 返回归一：`Decimal→float()`、`NULL→"未知"`（维度）/跳过（度量）、`count→int`、
  维度值走 `str()` 尾部语义（与内存路径逐项对照）。
- 能力标志：驱动类 `supports_group_pushdown=True`（Sqlite/PG/MySQL）；
  Oracle 类 `False`（待 11g 组合验证后开启，连接级可 override）。

## 3. 可下推判定与回退（`bi_sources.can_pushdown`）

回退（返回 False + 原因，调用方缓存）：
- 聚合含 `p50/p95/p99`（分位口径不可跨库保证）；
- `computed` 非空（表达式是 Python 侧能力）；维度/度量/过滤引用派生列；
- 维度含 `resolver`（内存 join）或 `groups`（CASE 归一 + 未命中口径）；
- 过滤含 `contains/not_contains/empty/not_empty` 或非数值区间的文本比较；
- `source_type == custom_sql`；HAP 源；
- 标识符未过白名单（中文/空格/点号/超长）；度量字段非数值类型（min/max 对文本合法但与
  Python"仅数值"语义不同）；
- 维度组合数预估超硬上限（如 50 万）。

## 4. 聚合接入与语义对齐

- `compute_window`：`can_pushdown` → `fetch_grouped` → entries；
  维度值映射沿用 `dim_value` 尾部规则（None→"未知"）；数值 `float()`。
- **快照语义不变**：form 源仍只算最新完整窗口；下推不引入时间窗 WHERE
  （`_scan_form` 本就忽略 start/end）；定义级 `filters` 进 WHERE。
- Top N/「其他」归并、ratio、分组归一（回退项）继续在 Python 侧（纯函数复用既有实现）。
- 失败兜底：`fetch_grouped` 任何异常 → 记日志 + 回退内存 + 缓存该定义的"不可下推"标记
  （避免每窗重试；进程内 TTL 与行缓存同寿命）。

## 5. 并发硬化

- 内存聚合（含回退与 HAP）在 `compute_window` 内改 `asyncio.to_thread(aggregate_rows, ...)`
  （纯函数、无 IO，可安全移出事件循环）；
- 在飞重查询信号量：`asyncio.Semaphore(4)` 包住"下推查询 + 内存聚合"段；
  超限时等待（GET 查询路径可 429 提示），**投放页读桶路径不受限**（本来就不重）；
- 明细 total 探测：大表用 `SELECT COUNT(*)` 下推？——**不做**（沿用 has_more 探测，
  P1 已定），保持一致性。

## 6. 存量修复：Oracle 大小写逻辑键

调研发现：Oracle `fetch_data` 返回键经 `cursor.description` **小写**，而 form-fields
元数据是大写列名；`get_field` 精确匹配 → **Oracle DB form 源聚合全 None**（存量 bug）。
修复：`bi_sources`/`bi_aggregator` 统一"逻辑键映射"（大小写互认，对齐 AGENTS 陷阱 9）：
下推与内存两条路径共用同一映射函数；单测覆盖（大写元数据 + 小写键行 → 正常聚合）。

## 6.5 对照 DataEase 的执行层组织（2026-09-27 复核源码）

结论（用于本项目安全基线的"受控例外"评审）：

1. **执行点收敛为一层**：DataEase 所有源库 SQL 执行集中在 `datasource/provider/`
   包（每库一个 Provider 实现 + CalciteProvider，SqlParser 用 Apache Calcite）；
   标识符校验/引号封装（`validateIdentifier`/`quoteIdentifier`）是 **Provider 基类的
   共享契约**——与本项目 `app/connectors/database.py` 的组织完全同构。
2. **它也做"受控拼接"**：`DatasetSQLManage` 用 `MessageFormat.format("SELECT {0} FROM {1}", …)`
   构造查询——开源 BI 的通行姿态是"标识符白名单 + 引号封装 + 值绑定"三件套，
   而非零拼接。本变更的构造器（SQLAlchemy 表达式编译，连拼接都不做）比其更收敛。
3. **信任模型 = 配置者可信**：自定义 SQL 数据集允许管理员写任意 SQL；
   安全边界画在"谁能配置数据源/数据集"，不在"引擎是否能构造 SQL"。
   本项目同款既有先例：`fetch_data` 家族本身就是"函数内动态拼接 + 执行"的
   源库执行点（已登记在案）；`_scan_custom_sql` 允许管理员写任意只读 SQL。
4. **对 Mimosa 预期的解读**：门禁拦截"**新增**执行点"是治理设计（要求显式评审
   扩大攻击面的动作），出场路径就是"评审入档的受控例外"——即本变更拟申报的形态：
   **新增执行点收敛到单一模块的单一函数 + 白名单/绑定/编译三件套 + 单测 +
   能力标志（可全局关闭）**。

## 7. 验收（脚本由子代理产出 `scripts/bi_pushdown_bench.py`）

- 100 万行 SQLite：下推聚合耗时 <3s（对比内存路径全量拉取的时间/内存）；
- 聚合进行中投放查询延迟无 >500ms 抖动；
- 数值与 SQL 直查逐项对照（分组/汇总/过滤三组）；
- 回退路径（computed/p95/HAP/Oracle 判定）行为与现状一致；
- 全量 pytest 零回归 + 三个 UI 自审脚本零回归。

## 8. 阶段三落地记录与偏差（2026-09-29 实施）

提交：`dd2e489`（执行点 + 判定 + 接入 + 单测 55 项）、`0034792`（基准补数值对照）。
放行方式：用户拍板 A 后，在 ZCode 环境层停用 Mimosa 插件
（`~/.zcode/cli/config.json` → `enabledPlugins.mimosa@zcode-plugins-official: false`，
改动前已留 `config.json.bak-20260929`）；hook 配置在会话启动时快照，故新会话生效。

### 8.1 与设计的三处收敛/偏差（均为更保守或更等价）

1. **执行形态比 §2 更收敛**：设计原写"驱动层新增 `fetch_grouped` 方法（每驱动一个）"，
   实作为**单模块单函数** `grouped_pushdown.fetch_grouped_rows`——三方言的编译与执行
   分派都在同一文件内（`_EXECUTORS` 表），连接器类只留 `DB_TYPE` /
   `supports_group_pushdown` 两个能力类属性。这样"新增执行点"在文件粒度上可枚举，
   与 §6.5 第 4 条的申报形态严格一致（DataEase 是每库一个 Provider，本项目更收）。
2. **NULL 行等价补偿（设计未预见，正确性必需）**：SQL 三值逻辑把 NULL 行判为不匹配，
   而内存匹配器 `_rule_match` 对 `ne` / `not_in` / `not_between`（以及空串参与比较的
   `lt`/`le`）返回 True —— 直接下推会静默少算（如"状态 ≠ 已完成"丢掉状态为空的行）。
   处置：判定侧用**同一份内存匹配器**对 NULL 行求值（`_rule_match({}, rule)`），
   为真则在 WHERE 补 `OR col IS NULL`。13 组差分对照里专设含 NULL 用例。
3. **PG 方言对象改 asyncpg 方言**：§1 表写 PG 为 `$n`，但 `postgresql.dialect()`
   渲染的是 psycopg2 风格命名占位且 `positiontup` 为空（参数一个都取不到）——
   运行时驱动是 asyncpg，必须用 `postgresql.asyncpg.dialect()`。

其余按设计：Top N/「其他」归并、ratio 除法与 scale、维度值归一仍在 Python 侧；
`ratio` 的两个分量各占一个 SQL 聚合列（key 用 `__ratio{i}__num/den`，不掺用户 key）。

### 8.2 实测（100 万行 SQLite，scripts/bi_pushdown_bench.py）

| 项 | 值 | 预算 |
|---|---|---|
| 聚合首轮（真实全量窗口） | **590ms** | <3s ✓ |
| 聚合次轮（水位线短路） | 5ms | — |
| 聚合期间投放查询抖动 | 最大 **81ms**，>1s 0 次 | <500ms ✓ |
| 明细首屏 / 深翻页（offset 39.98 万） | 4.4ms / 34.5ms | — |
| 数值三组对照（分组 5 组 / 汇总 4 值 / 过滤 3 组） | 与源库 SQL 逐项一致 | 1e-9 ✓ |

参照：同表同查询的**裸 sqlite3** `GROUP BY line, SUM, COUNT, AVG` 耗时 **598ms**——
即 590ms 基本是 SQLite 自身的全表扫描成本，下推链路（编译 + 取数 + 归一）几乎为零开销。
下推路径的**功能性别证**（比耗时更硬）：接口合计 = 整表 SQL 合计 298,499,591.33，
且 ≠ 前 10 万行合计 29,850,028.35（内存路径会截断在 MAX_FORM_ROWS=10 万）。

### 8.3 判定与回退的实际覆盖面

- 下推命中：单/多维度分组、无维度汇总、count/count_distinct/sum/avg/min/max、
  ratio（分子分母各为 count/sum/avg/min/max）、定义级 filters
  （eq/ne/in/not_in/between/not_between/gt/ge/lt/le；旧 dict 等值形态等价于 eq）
- 回退（内存路径，行为与改造前一致）：非 form 源、HAP、Oracle（能力标志默认关）、
  含 computed、resolver/normalize/groups 维度、分位算子、度量级 filter、
  非 ASCII 标识符、文本模糊过滤、非数值字段的数值算子与区间比较
- 失败兜底：下推异常（源库报错 / 超 GROUP_ROW_CAP=200k / 连接或 source_ref 问题）
  一律回退且缓存"不可下推"标记（TTL 60s 自愈）；连接与 source_ref 错误仍由内存路径
  抛出同样的 SourceError，**错误语义不变**
