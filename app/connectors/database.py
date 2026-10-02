"""数据库连接器 — MySQL/Oracle/SQLite/PostgreSQL 的元数据查询 + 数据读写

提供统一的 get_database_connector 工厂和 DatabaseConnector 基类。
驱动按需延迟导入，缺失时给出明确错误。

连接复用：Oracle/MySQL/PostgreSQL 经全局池注册表 _POOL_REGISTRY 按
(db_type, 连接配置指纹) 复用连接池，进程退出时用 close_all_pools() 统一关闭。
连接器实例的 disconnect() 为 no-op（池归注册表所有，不随单次调用关闭）。

Oracle 模式：python-oracledb 的 thin/thick 是进程级属性（官方限制，不可按连接混用）。
目标系统为 Oracle 11g，只能走 thick 模式（thin 需 12.1+）；
thick 下驱动是同步实现，统一经 asyncio.to_thread 包装避免阻塞事件循环。
"""
import asyncio
import hashlib
import inspect
import json
import logging
import re
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

# 表名/列名安全校验（防止 SQL 注入）
_IDENTIFIER_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

# 池参数（内部工具规模，不引入配置项）
_POOL_MIN = 1
_POOL_MAX = 5


def _validate_identifier(name: str, label: str = "identifier") -> str:
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"非法 {label}: {name}")
    return name


def is_safe_identifier(name: Any) -> bool:
    """标识符是否可进下推 SQL（纯 ASCII 白名单，与 _validate_identifier 同口径）

    非 ASCII 列名（中文/空格/点号）与超长名一律判否 → 调用方回退内存路径。
    """
    return isinstance(name, str) and bool(_IDENTIFIER_RE.match(name))


# ============================================================
# 聚合下推 SQL 构造（add-bi-sql-pushdown）
#
# 安全模型：用 SQLAlchemy 表达式 API 构造并编译（无任何字符串拼接）——
# 表名/列名先经 _validate_identifier 白名单再入 Column/Table；
# 过滤值经绑定参数（literal_binds 内联时由 SQLAlchemy 做类型化转义）。
# 编译产物 + alias_map（SQL 列别名 a0..an → 逻辑键）供驱动执行与重命名。
# ============================================================

def _dialect_object(dialect: str):
    """编译方言对象（未知方言显式拒绝，不静默回落 sqlite）

    PostgreSQL 用 **asyncpg 方言**：运行时驱动是 asyncpg（$n 位置参数），
    默认方言渲染的是 psycopg2 风格命名占位（%(name)s）且 positiontup 为空——
    拿它的编译文本交 asyncpg 执行必然失败。
    """
    from sqlalchemy.dialects import mysql as _mysql
    from sqlalchemy.dialects import postgresql as _pg
    from sqlalchemy.dialects import sqlite as _sqlite

    factory = {
        "sqlite": _sqlite.dialect,
        "postgresql": _pg.asyncpg.dialect,
        "mysql": _mysql.dialect,
    }.get(str(dialect))
    if factory is None:
        raise ValueError(f"不支持的下推方言: {dialect}")
    return factory()


def _build_grouped_sql(
    table: str,
    group_fields: list[str],
    aggs: list[dict],
    filters: list[dict] | None,
    order_by: list[tuple[str, str]] | None,
    limit: int | None,
    offset: int | None,
    *,
    dialect: str = "sqlite",
) -> tuple[str, list, dict[str, str]]:
    """构造聚合下推 SQL（SQLAlchemy 表达式编译，纯函数便于单测）

    返回 (sql, params, alias_map)：
    - sql：SELECT 维度列, 聚合 AS a0..an FROM 表 [WHERE …] [GROUP BY …]
      [ORDER BY …] [LIMIT/OFFSET]
    - params：按 positiontup 顺序展开的位置参数列表（qmark/format/numeric_dollar
      驱动直接消费）；值不内联
    - alias_map：{SQL 列别名: 逻辑键}（驱动层据此重命名，规避键大小写差异）
    """
    from sqlalchemy import (
        Column,
        MetaData,
        Table,
        and_,
        func as safunc,
        or_,
        select,
    )

    if not group_fields and not aggs:
        # 空选择会编译出 "SELECT FROM t"（SQLite: near "LIMIT": syntax error）——
        # 调用方（can_pushdown）本应拦下；此处 fail fast 给出可定位的错误
        raise ValueError("聚合下推需要至少一个分组列或聚合列")
    t_name = _validate_identifier(table, "table")
    ordered_fields: list[str] = []
    candidates: list[Any] = list(group_fields)
    candidates += [a.get("field") for a in aggs if a.get("field")]
    candidates += [f.get("field") for f in (filters or []) if f.get("field")]
    for name in candidates:
        _validate_identifier(name)
        if name not in ordered_fields:
            ordered_fields.append(name)

    md = MetaData()
    columns = {name: Column(name) for name in ordered_fields}
    tbl = Table(t_name, md, *columns.values())

    alias_map: dict[str, str] = {f: f for f in group_fields}
    select_items: list = [columns[f] for f in group_fields]
    agg_exprs: dict[str, Any] = {}
    for i, agg in enumerate(aggs):
        alias = f"a{i}"                       # 不用逻辑键做别名（Oracle 30 字节）
        fn = str(agg.get("func") or "")
        field = agg.get("field")
        col = columns.get(str(field)) if field else None
        if fn == "count" and col is None:
            expr = safunc.count()
        elif fn == "count_distinct" and col is not None:
            expr = safunc.count(col.distinct())
        elif fn in ("sum", "avg", "min", "max") and col is not None:
            expr = getattr(safunc, fn)(col)
        else:
            raise ValueError(f"不支持的聚合: {fn}")
        labeled = expr.label(alias)
        select_items.append(labeled)
        agg_exprs[alias] = expr
        alias_map[alias] = str(agg["key"])

    conds: list = []
    for rule in filters or []:
        col = columns[_validate_identifier(str(rule.get("field")))]
        op = str(rule.get("op") or "")
        value = rule.get("value")
        if value is None:
            raise ValueError(f"不可下推的空值过滤: {op}")
        if op == "eq":
            cond = col == value
        elif op == "ne":
            cond = col != value
        elif op == "gt":
            cond = col > value
        elif op == "ge":
            cond = col >= value
        elif op == "lt":
            cond = col < value
        elif op == "le":
            cond = col <= value
        elif op == "in" and isinstance(value, (list, tuple)) and value:
            # 用 OR 等值元组表达 IN——避免 SQLAlchemy expanding 的 POSTCOMPILE
            # 占位（编译文本无法交 raw 驱动执行）
            cond = or_(*[col == v for v in value])
        elif op == "not_in" and isinstance(value, (list, tuple)) and value:
            cond = or_(*[col != v for v in value])
        elif (op == "between" and isinstance(value, (list, tuple))
              and len(value) == 2 and all(v is not None for v in value)):
            cond = col.between(value[0], value[1])
        elif (op == "not_between" and isinstance(value, (list, tuple))
              and len(value) == 2 and all(v is not None for v in value)):
            cond = or_(col < value[0], col > value[1])
        else:
            raise ValueError(f"不可下推的过滤: {op}")
        # NULL 行等价性：SQL 三值逻辑把 NULL 行判为"不匹配"，而内存匹配器对
        # ne/not_in/not_between 等算子可能返回 True（如空串参与比较）。调用方
        # 用同一份内存匹配器对 NULL 行取值，为真时给 null_match——补 OR IS NULL
        # 后两条路径逐行等价。
        if rule.get("null_match"):
            cond = or_(cond, col.is_(None))
        conds.append(cond)

    stmt = select(*select_items)
    if conds:
        stmt = stmt.where(and_(*conds))
    if group_fields:
        stmt = stmt.group_by(*[columns[f] for f in group_fields])
    if order_by:
        order_exprs: list = []
        for key, direction in order_by:
            target = columns[key] if key in columns else agg_exprs.get(key)
            if target is None:
                raise ValueError(f"不可下推的排序字段: {key}")
            order_exprs.append(
                target.desc() if str(direction).lower() == "desc" else target.asc())
        stmt = stmt.order_by(*order_exprs)
    if limit is not None:
        stmt = stmt.limit(int(limit))
    if offset is not None:
        stmt = stmt.offset(int(offset))

    dialect_obj = _dialect_object(dialect)
    compiled = stmt.compile(dialect=dialect_obj)
    # 占位为方言风格（sqlite/mysql: ? / ?、pg: %s 由编译文本给出），参数按
    # positiontup 顺序展开为列表（qmark/format 驱动直接消费）；值不内联
    positiontup = list(getattr(compiled, "positiontup", None) or [])
    ordered_params = [compiled.params[name] for name in positiontup]
    return str(compiled), ordered_params, alias_map


def _rename_grouped_row(
    row: dict, alias_map: dict[str, str], aggs: list[dict],
    group_fields: list[str] | None = None,
) -> dict:
    """下推结果重命名到逻辑键 + 数值归一（对齐内存路径口径）

    - 维度列按 group_fields 显式匹配（大小写无关）优先判定：列名恰为 a0/a1 这类
      与聚合别名同形时不会被误判为聚合列；
    - 聚合列（别名 a0..an）数值归一：count/count_distinct → int，其余 → float
      （None 保留）——与 bi_aggregator.compute_measure 的返回类型逐项一致
      （内存路径 sum/avg/min/max 恒为 float，count 恒为 int）。
    """
    func_of: dict[str, str] = {
        f"a{i}": str((a or {}).get("func") or "") for i, a in enumerate(aggs)
    }
    group_map: dict[str, str] = {}
    for g in group_fields or []:
        group_map[str(g)] = str(g)
        group_map[str(g).lower()] = str(g)

    def _normalize(func: str, v: Any) -> Any:
        if v is None:
            return None
        if func in ("count", "count_distinct"):
            return int(v)
        try:
            return float(v)
        except (TypeError, ValueError):
            return v

    out: dict[str, Any] = {}
    for key, value in row.items():
        skey = str(key)
        if skey in group_map:
            out[group_map[skey]] = value
            continue
        lower = skey.lower()
        if lower in group_map:
            out[group_map[lower]] = value
            continue
        alias = skey if skey in func_of else (lower if lower in func_of else None)
        if alias is not None:
            out[alias_map.get(alias, alias)] = _normalize(func_of[alias], value)
        else:
            out[alias_map.get(skey, alias_map.get(lower, skey))] = value
    return out


# ============================================================
# 全局连接池注册表
# ============================================================

_POOL_REGISTRY: dict[tuple[str, str], Any] = {}
_POOL_LAST_USED: dict[tuple[str, str], float] = {}
_POOL_LOCK = threading.Lock()


def _config_fingerprint(db_config: dict) -> str:
    """连接配置指纹：规范化后 sha1；任一字段（含密码）变化即产生新池"""
    normalized = json.dumps(db_config, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


async def _close_pool(db_type: str, pool: Any) -> None:
    """按驱动类型关闭单个池"""
    if pool is None:
        return
    if db_type == "oracle":
        await asyncio.to_thread(pool.close)
    elif db_type == "mysql":
        pool.close()
        await pool.wait_closed()
    else:  # postgresql
        await pool.close()


async def _get_registered_pool(db_type: str, db_config: dict, factory):
    """取注册表中的池，缺失时经 factory 创建并登记

    factory: 无参调用，返回池或池的协程。并发首次创建时后到者
    关闭自己多建的池、采用已注册池，保证同一配置始终只有一个池。
    """
    key = (db_type, _config_fingerprint(db_config))
    with _POOL_LOCK:
        pool = _POOL_REGISTRY.get(key)
    if pool is not None:
        _POOL_LAST_USED[key] = time.time()
        return pool

    created = factory()
    if inspect.isawaitable(created):
        created = await created

    with _POOL_LOCK:
        existing = _POOL_REGISTRY.get(key)
        if existing is not None:
            try:
                await _close_pool(db_type, created)
            except Exception as e:  # 关闭失败不影响取用已注册池
                logger.warning("关闭并发重复创建的 %s 池失败: %s", db_type, e)
            return existing
        _POOL_REGISTRY[key] = created
        _POOL_LAST_USED[key] = time.time()
        return created


async def sweep_idle_pools(max_idle_minutes: float | None = None) -> int:
    """回收空闲超时的连接池（task 2.1）：返回回收数量

    由应用生命周期内的定时任务周期性调用。
    """
    from app.config import settings

    ttl = (max_idle_minutes if max_idle_minutes is not None
           else settings.pool_idle_ttl_minutes) * 60
    now = time.time()
    with _POOL_LOCK:
        stale = [key for key, used in _POOL_LAST_USED.items()
                 if now - used > ttl and key in _POOL_REGISTRY]
    recovered = 0
    for key in stale:
        with _POOL_LOCK:
            pool = _POOL_REGISTRY.get(key)
            if pool is None:
                continue
            _POOL_REGISTRY.pop(key, None)
            _POOL_LAST_USED.pop(key, None)
        try:
            await _close_pool(key[0], pool)
            recovered += 1
        except Exception as e:
            logger.warning("回收空闲连接池 %s 失败: %s", key[0], e)
    return recovered


async def close_all_pools() -> None:
    """关闭全部连接池（应用停机时调用），并清空注册表"""
    with _POOL_LOCK:
        entries = list(_POOL_REGISTRY.items())
        _POOL_REGISTRY.clear()
        _POOL_LAST_USED.clear()
    for (db_type, _fingerprint), pool in entries:
        try:
            await _close_pool(db_type, pool)
        except Exception as e:
            logger.warning("关闭 %s 连接池失败: %s", db_type, e)


# ============================================================
# Oracle 客户端模式（进程级）
# ============================================================

_ORACLE_THICK_INITIALIZED = False
_ORACLE_INIT_LOCK = threading.Lock()


def _ensure_oracle_client() -> None:
    """thick 模式下加载 Instant Client（进程内仅允许一次，线程安全）

    默认 thin 模式不加载任何客户端库；
    加载失败给出含 oracle_client_init_failed 的可诊断错误。
    """
    global _ORACLE_THICK_INITIALIZED
    from app.config import settings

    if settings.oracle_client_mode != "thick":
        return
    with _ORACLE_INIT_LOCK:
        if _ORACLE_THICK_INITIALIZED:
            return
        try:
            import oracledb

            oracledb.init_oracle_client(
                lib_dir=settings.oracle_client_lib_dir or None
            )
            _ORACLE_THICK_INITIALIZED = True
        except Exception as e:
            raise RuntimeError(
                f"oracle_client_init_failed: 无法加载 Oracle Instant Client "
                f"(lib_dir={settings.oracle_client_lib_dir or '系统PATH'}): {e}"
            ) from e


class DatabaseConnector(ABC):
    """数据库连接器抽象基类

    所有数据库连接器 MUST 实现此接口，供 sync_handler 统一调用。
    """

    # 驱动类型标识（聚合下推的方言选择与执行分派用；与 get_database_connector 的
    # db_type 取值一致）
    DB_TYPE: str = ""

    # 聚合下推能力标志（add-bi-sql-pushdown）：False = 调用方回退内存路径。
    # Oracle 默认 False——11g 的 ROWNUM 双层分页 + 聚合 + 排序组合需单独验证，
    # 验证通过后可在此（或连接级 override）开启。
    supports_group_pushdown: bool = False

    def __init__(self, db_config: dict):
        self.db_config = db_config or {}

    # ---- 元数据查询 ----

    @abstractmethod
    async def get_table_list(self) -> list[str]:
        """获取所有表名列表"""
        ...

    @abstractmethod
    async def get_table_schema(self, table: str) -> list[dict]:
        """获取表字段定义

        Returns:
            [{"name": "id", "type": "INTEGER", "nullable": False, "primary_key": True}, ...]
        """
        ...

    # ---- 数据读写 ----

    @abstractmethod
    async def fetch_data(
        self,
        table: str,
        columns: list[str] | None = None,
        where: dict[str, Any] | None = None,
        order_by: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        where_gt: dict[str, Any] | None = None,
    ) -> list[dict]:
        """从表查询数据

        Args:
            table: 表名
            columns: 要查询的列，None=全部
            where: 等值过滤条件 {column: value}
            where_gt: 大于过滤条件 {column: value}（增量同步用）
            order_by: 排序字段（仅简单列名）
            limit: 返回行数上限
            offset: 偏移量

        Returns:
            字典列表
        """
        ...

    # ---- 下推聚合 ----
    #
    # 聚合下推不在本类上开方法：执行点收敛到 app/connectors/grouped_pushdown.py
    # 的单一入口 fetch_grouped_rows（编译走本模块 _build_grouped_sql，标识符白名单
    # + 全值绑定）；能力由 DB_TYPE / supports_group_pushdown 两个类属性声明，
    # 调用方（bi_sources.can_pushdown）据此前置判定，不可用时回退内存路径。

    @abstractmethod
    async def push_data(
        self,
        table: str,
        data_list: list[dict],
        key_field: str | None = None,
        mode: str = "insert",
    ) -> int:
        """写入数据到表

        Args:
            table: 目标表名
            data_list: 要写入的数据行
            key_field: 用于 upsert/replace 的主键字段
            mode: insert | upsert | replace

        Returns:
            受影响行数（驱动报告的真实值）
        """
        ...

    @abstractmethod
    async def delete_rows(self, table: str, key_field: str,
                          values: list) -> int:
        """按键删除行：DELETE FROM table WHERE key_field IN (values)

        Returns:
            删除行数
        """
        ...

    @abstractmethod
    async def test_connection(self) -> dict:
        """测试连接可用性

        Returns:
            {"status": "ok"|"failed", "latency_ms": int, "error": str|None}
        """
        ...

    @abstractmethod
    async def disconnect(self):
        """关闭连接/连接池

        池化实现中为 no-op：池由全局注册表持有，不随单次调用关闭，
        进程退出统一走 close_all_pools()。
        """
        ...


# ============================================================
# SQLite
# ============================================================

class SqliteConnector(DatabaseConnector):
    """SQLite 连接器（标准 sqlite3 + asyncio.to_thread）

    文件级单写者，不做池化，沿用每次短连接。
    """

    DB_TYPE = "sqlite"
    supports_group_pushdown = True

    def __init__(self, db_config: dict):
        super().__init__(db_config)
        self._conn = None

    def _connect_sync(self):
        import sqlite3
        db_path = self.db_config.get("database", ":memory:")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def _call(self, func, *args, **kwargs):
        """在后台线程执行同步 SQLite 操作"""
        return await asyncio.to_thread(func, *args, **kwargs)

    # ---- 元数据 ----

    async def get_table_list(self) -> list[str]:
        def _do():
            conn = self._connect_sync()
            try:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
                return [r[0] for r in rows]
            finally:
                conn.close()

        return await self._call(_do)

    async def get_table_schema(self, table: str) -> list[dict]:
        t = _validate_identifier(table)

        def _do():
            conn = self._connect_sync()
            try:
                rows = conn.execute(f'PRAGMA table_info("{t}")').fetchall()
                return [
                    {
                        "name": row[1],
                        "type": row[2],
                        "nullable": row[3] == 0,
                        "primary_key": row[5] > 0,
                    }
                    for row in rows
                ]
            finally:
                conn.close()

        return await self._call(_do)

    # ---- 数据读写 ----

    async def fetch_data(
        self, table: str, columns=None, where=None,
        order_by=None, limit=None, offset=None, where_gt=None
    ) -> list[dict]:
        t = _validate_identifier(table)
        cols = ", ".join(_validate_identifier(c) for c in columns) if columns else "*"
        sql = f"SELECT {cols} FROM \"{t}\""
        params = []

        conditions = []
        if where:
            for k, v in where.items():
                _validate_identifier(k)
                conditions.append(f'"{k}" = ?')
                params.append(v)
        if where_gt:
            for k, v in where_gt.items():
                _validate_identifier(k)
                conditions.append(f'"{k}" > ?')
                params.append(v)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        if order_by:
            sql += f' ORDER BY "{_validate_identifier(order_by)}"'

        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        if offset is not None:
            sql += f" OFFSET {int(offset)}"

        def _do():
            conn = self._connect_sync()
            try:
                rows = conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await self._call(_do)

    async def push_data(
        self, table: str, data_list: list[dict],
        key_field=None, mode="insert"
    ) -> int:
        """写入数据；返回真实受影响行数。

        upsert（带 key_field）用 ON CONFLICT DO UPDATE——只更新本次携带的列；
        INSERT OR REPLACE 是"删+插"，未携带的列会被重置为默认值并触发 DELETE
        语义，partial upsert 会悄悄清列。replace 模式保留 OR REPLACE（其语义
        就是整行替换）。insert 模式下主键冲突等错误直接上抛，不做静默吞并。
        """
        if not data_list:
            return 0
        t = _validate_identifier(table)

        def _do():
            total = 0
            conn = self._connect_sync()
            try:
                if mode in ("upsert", "replace") and key_field:
                    _validate_identifier(key_field)
                    columns = [_validate_identifier(c) for c in data_list[0]]
                    col_list = ", ".join(f'"{c}"' for c in columns)
                    placeholders = ", ".join("?" for _ in columns)
                    if mode == "upsert":
                        updates = ", ".join(f'"{c}" = excluded."{c}"'
                                            for c in columns if c != key_field)
                        conflict = (f'ON CONFLICT("{key_field}") DO UPDATE SET {updates}'
                                    if updates else
                                    f'ON CONFLICT("{key_field}") DO NOTHING')
                        sql = (f'INSERT INTO "{t}" ({col_list}) '
                               f'VALUES ({placeholders}) {conflict}')
                    else:
                        sql = (
                            f'INSERT OR REPLACE INTO "{t}" '
                            f'({col_list}) VALUES ({placeholders})'
                        )
                    for row in data_list:
                        values = [row[c] for c in columns]
                        try:
                            total += conn.execute(sql, values).rowcount
                        except sqlite3.OperationalError as exc:
                            if "ON CONFLICT clause does not match" in str(exc):
                                # key_field 无 UNIQUE/PK 约束时 ON CONFLICT 无法成立
                                # （INSERT OR REPLACE 旧语义是静默重复插入，同样错误）——
                                # 翻译为可行动错误而非驱动原文（审查 P1）
                                from app.adapters.errors import AdapterError
                                raise AdapterError(
                                    'upsert_key_not_unique',
                                    f'目标表 "{t}" 的业务键 "{key_field}" 没有'
                                    '主键/唯一约束，无法安全 upsert；'
                                    '请为目标键建立唯一索引，或改用 insert/replace 模式',
                                    'write') from exc
                            raise
                else:
                    columns = [_validate_identifier(c) for c in data_list[0]]
                    placeholders = ", ".join("?" for _ in columns)
                    sql = (
                        f'INSERT INTO "{t}" ({", ".join(f"\"{c}\"" for c in columns)}) '
                        f'VALUES ({placeholders})'
                    )
                    for row in data_list:
                        values = [row[c] for c in columns]
                        total += conn.execute(sql, values).rowcount
                conn.commit()
                return total
            finally:
                conn.close()

        return await self._call(_do)

    async def delete_rows(self, table: str, key_field: str, values: list) -> int:
        t = _validate_identifier(table)
        kf = _validate_identifier(key_field)
        vals = [v for v in values if v is not None]
        if not vals:
            return 0
        # 分块执行：SQLite 绑定变量有上限（旧版 999/新版 32766），一次性 IN 大集合
        # 会抛 too many SQL variables（审查 P1：大表首次删差即失败）；单连接内
        # 分块、最后一次性提交保持原子语义
        chunk_size = 500

        def _do():
            total = 0
            conn = self._connect_sync()
            try:
                for i in range(0, len(vals), chunk_size):
                    chunk = vals[i:i + chunk_size]
                    placeholders = ", ".join("?" for _ in chunk)
                    cur = conn.execute(
                        f'DELETE FROM "{t}" WHERE "{kf}" IN ({placeholders})', chunk)
                    total += cur.rowcount
                conn.commit()
                return total
            finally:
                conn.close()

        return await self._call(_do)

    async def test_connection(self) -> dict:
        def _do():
            t0 = time.perf_counter()
            try:
                conn = self._connect_sync()
                try:
                    conn.execute("SELECT 1")
                finally:
                    conn.close()
                latency = int((time.perf_counter() - t0) * 1000)
                return {"status": "ok", "latency_ms": latency, "error": None}
            except Exception as e:
                latency = int((time.perf_counter() - t0) * 1000)
                return {"status": "failed", "latency_ms": latency, "error": str(e)}

        return await self._call(_do)

    async def disconnect(self):
        if self._conn:
            self._conn.close()
            self._conn = None


# ============================================================
# PostgreSQL
# ============================================================

class PostgresqlConnector(DatabaseConnector):
    """PostgreSQL 连接器（asyncpg，连接池复用）"""

    DB_TYPE = "postgresql"
    supports_group_pushdown = True

    def __init__(self, db_config: dict):
        super().__init__(db_config)
        self._pool = None

    def _dsn(self) -> str:
        cfg = self.db_config
        host = cfg.get("host", "localhost")
        port = cfg.get("port", 5432)
        database = cfg.get("database", "")
        user = cfg.get("username", cfg.get("user", ""))
        password = cfg.get("password", "")
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"

    async def _get_pool(self):
        import asyncpg
        if self._pool is not None:
            return self._pool
        self._pool = await _get_registered_pool(
            "postgresql",
            self.db_config,
            lambda: asyncpg.create_pool(
                self._dsn(), min_size=_POOL_MIN, max_size=_POOL_MAX
            ),
        )
        return self._pool

    # ---- 元数据 ----

    async def get_table_list(self) -> list[str]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' ORDER BY table_name"
            )
            return [r["table_name"] for r in rows]

    async def get_table_schema(self, table: str) -> list[dict]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT column_name, data_type, is_nullable, "
                "COALESCE((SELECT TRUE FROM information_schema.key_column_usage kcu "
                "JOIN information_schema.table_constraints tc "
                "ON kcu.constraint_name=tc.constraint_name AND tc.constraint_type='PRIMARY KEY' "
                "WHERE kcu.table_name=c.table_name AND kcu.column_name=c.column_name), FALSE) as is_pk "
                "FROM information_schema.columns c WHERE c.table_name=$1 ORDER BY ordinal_position",
                table,
            )
            return [
                {
                    "name": r["column_name"],
                    "type": r["data_type"],
                    "nullable": r["is_nullable"] == "YES",
                    "primary_key": r["is_pk"],
                }
                for r in rows
            ]

    # ---- 数据读写 ----

    async def fetch_data(
        self, table: str, columns=None, where=None,
        order_by=None, limit=None, offset=None, where_gt=None
    ) -> list[dict]:
        t = _validate_identifier(table)
        cols = ", ".join(f'"{_validate_identifier(c)}"' for c in columns) if columns else "*"
        sql = f'SELECT {cols} FROM "{t}"'
        params = []
        idx = 1

        clauses = []
        if where:
            for k, v in where.items():
                _validate_identifier(k)
                clauses.append(f'"{k}" = ${idx}')
                params.append(v)
                idx += 1
        if where_gt:
            for k, v in where_gt.items():
                _validate_identifier(k)
                clauses.append(f'"{k}" > ${idx}')
                params.append(v)
                idx += 1
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)

        if order_by:
            sql += f' ORDER BY "{_validate_identifier(order_by)}"'
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        if offset is not None:
            sql += f" OFFSET {int(offset)}"

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            return [dict(r) for r in rows]

    async def push_data(
        self, table: str, data_list: list[dict],
        key_field=None, mode="insert"
    ) -> int:
        if not data_list:
            return 0
        t = _validate_identifier(table)
        columns = [_validate_identifier(c) for c in data_list[0]]

        if mode == "upsert" and key_field:
            kf = _validate_identifier(key_field)
            col_list = ", ".join(f'"{c}"' for c in columns)
            placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
            updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in columns)
            sql = (
                f'INSERT INTO "{t}" ({col_list}) VALUES ({placeholders}) '
                f'ON CONFLICT ("{kf}") DO UPDATE SET {updates}'
            )
        else:
            col_list = ", ".join(f'"{c}"' for c in columns)
            placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
            sql = f'INSERT INTO "{t}" ({col_list}) VALUES ({placeholders})'

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            # asyncpg executemany 在单个事务内原子执行：成功即全部生效，
            # 因此 len(data_list) 就是真实受影响行数；失败整批上抛。
            await conn.executemany(sql, [[row[c] for c in columns] for row in data_list])
        return len(data_list)

    async def delete_rows(self, table: str, key_field: str, values: list) -> int:
        t = _validate_identifier(table)
        kf = _validate_identifier(key_field)
        vals = [v for v in values if v is not None]
        if not vals:
            return 0
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                f'DELETE FROM "{t}" WHERE "{kf}" = ANY($1)', vals)
        return int(result.split()[-1])

    async def test_connection(self) -> dict:
        start = time.perf_counter()
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "ok", "latency_ms": latency, "error": None}
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "failed", "latency_ms": latency, "error": str(e)}

    async def disconnect(self):
        self._pool = None  # 池归注册表持有


# ============================================================
# MySQL
# ============================================================

class MysqlConnector(DatabaseConnector):
    """MySQL 连接器（aiomysql，连接池复用）"""

    DB_TYPE = "mysql"
    supports_group_pushdown = True

    def __init__(self, db_config: dict):
        super().__init__(db_config)
        self._pool = None

    async def _get_pool(self):
        import aiomysql
        if self._pool is not None:
            return self._pool
        self._pool = await _get_registered_pool(
            "mysql",
            self.db_config,
            lambda: aiomysql.create_pool(
                host=self.db_config.get("host", "localhost"),
                port=self.db_config.get("port", 3306),
                user=self.db_config.get("username", self.db_config.get("user", "")),
                password=self.db_config.get("password", ""),
                db=self.db_config.get("database", ""),
                charset=self.db_config.get("charset", "utf8mb4"),
                autocommit=True,
                minsize=_POOL_MIN,
                maxsize=_POOL_MAX,
            ),
        )
        return self._pool

    # ---- 元数据 ----

    async def get_table_list(self) -> list[str]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SHOW TABLES")
                rows = await cur.fetchall()
                return [r[0] for r in rows]

    async def get_table_schema(self, table: str) -> list[dict]:
        t = _validate_identifier(table)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"DESCRIBE `{t}`")
                rows = await cur.fetchall()
                return [
                    {
                        "name": r[0],
                        "type": r[1],
                        "nullable": r[2] == "YES",
                        "primary_key": r[3] == "PRI",
                    }
                    for r in rows
                ]

    # ---- 数据读写 ----

    async def fetch_data(
        self, table: str, columns=None, where=None,
        order_by=None, limit=None, offset=None, where_gt=None
    ) -> list[dict]:
        t = _validate_identifier(table)
        cols = ", ".join(f'`{_validate_identifier(c)}`' for c in columns) if columns else "*"
        sql = f"SELECT {cols} FROM `{t}`"
        params = []

        conditions = []
        if where:
            for k, v in where.items():
                _validate_identifier(k)
                conditions.append(f"`{k}` = %s")
                params.append(v)
        if where_gt:
            for k, v in where_gt.items():
                _validate_identifier(k)
                conditions.append(f"`{k}` > %s")
                params.append(v)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        if order_by:
            sql += f' ORDER BY `{_validate_identifier(order_by)}`'
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        if offset is not None:
            sql += f" OFFSET {int(offset)}"

        try:
            from aiomysql import DictCursor  # 局部导入，缺失时走兜底路径
            cursor_cls = DictCursor
        except ImportError:
            cursor_cls = None

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(cursor_cls) as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
                if cursor_cls is None and rows:
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, r)) for r in rows]
                return rows

    async def push_data(
        self, table: str, data_list: list[dict],
        key_field=None, mode="insert"
    ) -> int:
        if not data_list:
            return 0
        t = _validate_identifier(table)
        columns = [_validate_identifier(c) for c in data_list[0]]
        col_list = ", ".join(f"`{c}`" for c in columns)
        placeholders = ", ".join("%s" for _ in columns)

        if mode == "upsert" and key_field:
            kf = _validate_identifier(key_field)
            updates = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in columns)
            sql = (
                f"INSERT INTO `{t}` ({col_list}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {updates}"
            )
        elif mode == "replace":
            sql = f"REPLACE INTO `{t}` ({col_list}) VALUES ({placeholders})"
        else:
            sql = f"INSERT INTO `{t}` ({col_list}) VALUES ({placeholders})"

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(sql, [[row[c] for c in columns] for row in data_list])
                return cur.rowcount  # executemany 后为累计真实受影响行数

    async def delete_rows(self, table: str, key_field: str, values: list) -> int:
        t = _validate_identifier(table)
        kf = _validate_identifier(key_field)
        vals = [v for v in values if v is not None]
        if not vals:
            return 0
        # 分块执行：MySQL 预编译占位符上限 65535，且超长语句受包大小限制；
        # 连接 autocommit=True，逐块即逐事务提交（与既有写路径语义一致）
        chunk_size = 1000
        pool = await self._get_pool()
        deleted = 0
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                for i in range(0, len(vals), chunk_size):
                    chunk = vals[i:i + chunk_size]
                    placeholders = ", ".join("%s" for _ in chunk)
                    await cur.execute(
                        f"DELETE FROM `{t}` WHERE `{kf}` IN ({placeholders})", chunk)
                    deleted += cur.rowcount
        return deleted

    async def test_connection(self) -> dict:
        start = time.perf_counter()
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "ok", "latency_ms": latency, "error": None}
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "failed", "latency_ms": latency, "error": str(e)}

    async def disconnect(self):
        self._pool = None  # 池归注册表持有


# ============================================================
# Oracle
# ============================================================

def build_oracle_fetch_sql(
    table: str,
    columns: list[str] | None = None,
    where: dict[str, Any] | None = None,
    order_by: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    where_gt: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """构造 Oracle 查询 SQL（11g 兼容：ROWNUM 分页）

    目标库为 11g，禁止生成 12c+ 的 `OFFSET ... ROWS FETCH NEXT` 语法：
    - limit-only：单层 `ROWNUM <= :lim` 短路
    - offset+limit：ROWNUM 双层包裹，上下界用绑定变量

    Returns:
        (sql, 命名绑定参数)
    """
    t = _validate_identifier(table).upper()
    cols = (
        ", ".join(_validate_identifier(c).upper() for c in columns) if columns else "*"
    )
    sql = f'SELECT {cols} FROM "{t}"'
    params: dict[str, Any] = {}

    conditions = []
    if where:
        for i, (k, v) in enumerate(where.items()):
            _validate_identifier(k)
            pname = f"w_{i}"
            conditions.append(f'"{k.upper()}" = :{pname}')
            params[pname] = v
    if where_gt:
        for i, (k, v) in enumerate(where_gt.items()):
            _validate_identifier(k)
            pname = f"gt_{i}"
            conditions.append(f'"{k.upper()}" > :{pname}')
            params[pname] = v
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    if order_by:
        sql += f' ORDER BY "{_validate_identifier(order_by).upper()}"'

    if limit is None:
        return sql, params

    if not offset:
        return (
            f"SELECT * FROM ({sql}) WHERE ROWNUM <= :lim",
            {**params, "lim": int(limit)},
        )

    hi = int(offset) + int(limit)
    paged = (
        "SELECT * FROM ("
        f"SELECT paged_q.*, ROWNUM rn FROM ({sql}) paged_q WHERE ROWNUM <= :hi"
        ") WHERE rn > :lo"
    )
    return paged, {**params, "hi": hi, "lo": int(offset)}


class OracleConnector(DatabaseConnector):
    """Oracle 连接器（oracledb，进程级 thin/thick 模式）

    目标系统为 Oracle 11g：thick 模式下驱动为同步实现，
    所有阻塞调用经 asyncio.to_thread 执行；连接经同步池复用。

    聚合下推默认关闭（supports_group_pushdown 继承基类 False）：
    11g 需 ROWNUM 双层包裹，聚合 + 排序 + 分页组合行为待单独验证后开启。
    """

    DB_TYPE = "oracle"

    def __init__(self, db_config: dict):
        super().__init__(db_config)
        self._pool = None

    def _dsn(self) -> str:
        """构造 DSN；db_config 配置 sid 时用 SID 形式（11g XE 常见），否则按服务名"""
        cfg = self.db_config
        host = cfg.get("host", "localhost")
        port = cfg.get("port", 1521)
        sid = cfg.get("sid")
        if sid:
            import oracledb
            return oracledb.makedsn(host, port, sid=sid)
        service = cfg.get("service_name", cfg.get("database", ""))
        return f"{host}:{port}/{service}"

    async def _get_pool(self):
        import oracledb
        if self._pool is not None:
            return self._pool
        _ensure_oracle_client()

        def _create():
            return oracledb.create_pool(
                user=self.db_config.get("username", self.db_config.get("user", "")),
                password=self.db_config.get("password", ""),
                dsn=self._dsn(),
                min=_POOL_MIN,
                max=_POOL_MAX,
                increment=1,
            )

        self._pool = await _get_registered_pool(
            "oracle", self.db_config, lambda: asyncio.to_thread(_create)
        )
        return self._pool

    # ---- 元数据 ----

    async def get_table_list(self) -> list[str]:
        pool = await self._get_pool()

        def _do():
            with pool.acquire() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT table_name FROM user_tables ORDER BY table_name")
                    return [r[0] for r in cursor.fetchall()]

        return await asyncio.to_thread(_do)

    async def get_table_schema(self, table: str) -> list[dict]:
        pool = await self._get_pool()

        def _do():
            with pool.acquire() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT tc.column_name, tc.data_type, tc.nullable, "
                        "CASE WHEN EXISTS(SELECT 1 FROM user_constraints c "
                        "JOIN user_cons_columns cc ON c.constraint_name=cc.constraint_name "
                        "WHERE c.table_name=UPPER(:t) AND c.constraint_type='P' "
                        "AND cc.table_name=tc.table_name "
                        "AND cc.column_name=tc.column_name) "
                        "THEN 1 ELSE 0 END as is_pk "
                        "FROM user_tab_columns tc "
                        "WHERE tc.table_name=UPPER(:t) ORDER BY tc.column_id",
                        t=table,
                    )
                    rows = cursor.fetchall()
                    return [
                        {
                            "name": r[0],
                            "type": r[1],
                            "nullable": r[2] == "Y",
                            "primary_key": bool(r[3]),
                        }
                        for r in rows
                    ]

        return await asyncio.to_thread(_do)

    # ---- 数据读写 ----

    async def fetch_data(
        self, table: str, columns=None, where=None,
        order_by=None, limit=None, offset=None, where_gt=None
    ) -> list[dict]:
        sql, params = build_oracle_fetch_sql(
            table, columns=columns, where=where,
            order_by=order_by, limit=limit, offset=offset, where_gt=where_gt,
        )
        pool = await self._get_pool()

        def _do():
            with pool.acquire() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(sql, params)
                    rows = cursor.fetchall()
                    names = [d[0].lower() for d in cursor.description]
                    return [dict(zip(names, row)) for row in rows]

        return await asyncio.to_thread(_do)

    async def push_data(
        self, table: str, data_list: list[dict],
        key_field=None, mode="insert"
    ) -> int:
        """写入数据，返回驱动报告的真实受影响行数。

        - insert：executemany 数组绑定，一次提交
        - upsert/replace（带 key_field）：逐行 MERGE（11g 支持），行数累加
        """
        if not data_list:
            return 0
        pool = await self._get_pool()
        t = _validate_identifier(table).upper()
        columns = [_validate_identifier(c) for c in data_list[0]]
        col_list = ", ".join(f'"{c.upper()}"' for c in columns)

        if mode in ("upsert", "replace") and key_field:
            kf = _validate_identifier(key_field).upper()
            using_cols = ", ".join(
                f':c_{i} AS "{c.upper()}"' for i, c in enumerate(columns)
            )
            # ORA-38104：ON 子句引用的键列不能出现在 UPDATE SET 中
            update_cols = [c for c in columns if c.upper() != kf]
            matched_clause = ""
            if update_cols:
                updates = ", ".join(
                    f'target."{c.upper()}" = source."{c.upper()}"'
                    for c in update_cols
                )
                matched_clause = f"WHEN MATCHED THEN UPDATE SET {updates} "
            merge_sql = (
                f'MERGE INTO "{t}" target '
                f'USING (SELECT {using_cols} FROM DUAL) source '
                f'ON (target."{kf}" = source."{kf}") '
                f'{matched_clause}'
                f'WHEN NOT MATCHED THEN INSERT ({col_list}) '
                f'VALUES ({", ".join(f"source.\"{c.upper()}\"" for c in columns)})'
            )

            def _do_merge():
                total = 0
                with pool.acquire() as conn:
                    with conn.cursor() as cursor:
                        for row in data_list:
                            params = {f"c_{i}": row[c] for i, c in enumerate(columns)}
                            cursor.execute(merge_sql, params)
                            total += cursor.rowcount
                    conn.commit()
                return total

            return await asyncio.to_thread(_do_merge)

        placeholders = ", ".join(f":{i+1}" for i in range(len(columns)))
        insert_sql = f'INSERT INTO "{t}" ({col_list}) VALUES ({placeholders})'

        def _do_insert():
            with pool.acquire() as conn:
                with conn.cursor() as cursor:
                    cursor.executemany(
                        insert_sql, [[row[c] for c in columns] for row in data_list]
                    )
                    affected = cursor.rowcount  # executemany 后为累计真实值
                conn.commit()
                return affected

        return await asyncio.to_thread(_do_insert)

    async def delete_rows(self, table: str, key_field: str, values: list) -> int:
        vals = [v for v in values if v is not None]
        if not vals:
            return 0
        # 分块执行：Oracle IN 列表上限 1000 个表达式（ORA-01795），大集合一次删除必失败
        t = _validate_identifier(table).upper()
        kf = _validate_identifier(key_field).upper()
        chunk_size = 1000
        pool = await self._get_pool()

        def _do():
            deleted = 0
            with pool.acquire() as conn:
                with conn.cursor() as cursor:
                    for i in range(0, len(vals), chunk_size):
                        chunk = vals[i:i + chunk_size]
                        binds = ", ".join(f":{j+1}" for j in range(len(chunk)))
                        cursor.execute(
                            f'DELETE FROM "{t}" WHERE "{kf}" IN ({binds})', chunk)
                        deleted += cursor.rowcount
                conn.commit()
            return deleted

        return await asyncio.to_thread(_do)

    async def test_connection(self) -> dict:
        start = time.perf_counter()
        try:
            pool = await self._get_pool()

            def _do():
                with pool.acquire() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT 1 FROM DUAL")
                        cursor.fetchall()

            await asyncio.to_thread(_do)
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "ok", "latency_ms": latency, "error": None}
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "failed", "latency_ms": latency, "error": str(e)}

    async def disconnect(self):
        self._pool = None  # 池归注册表持有


# ============================================================
# 工厂
# ============================================================

_CONNECTOR_REGISTRY: dict[str, type[DatabaseConnector]] = {
    "sqlite": SqliteConnector,
    "postgresql": PostgresqlConnector,
    "postgres": PostgresqlConnector,
    "mysql": MysqlConnector,
    "oracle": OracleConnector,
}


def get_database_connector(db_type: str, db_config: dict) -> DatabaseConnector:
    """获取数据库连接器实例

    Args:
        db_type: 数据库类型 (sqlite/postgresql/mysql/oracle)
        db_config: 连接配置 {"host":..., "port":..., "database":..., "username":..., "password":...}

    Raises:
        ValueError: 不支持的数据库类型
    """
    db_type = db_type.lower()
    cls = _CONNECTOR_REGISTRY.get(db_type)
    if cls is None:
        raise ValueError(f"不支持的数据库类型: {db_type}，支持: {list(_CONNECTOR_REGISTRY.keys())}")
    from app.security.crypto import decrypt_db_config
    return cls(decrypt_db_config(db_config) or {})


def supports_group_pushdown(db_type: str) -> bool:
    """按 db_type 查询聚合下推能力（读类属性，不实例化连接器/不碰凭证）"""
    cls = _CONNECTOR_REGISTRY.get(str(db_type or "").lower())
    return bool(cls is not None and cls.supports_group_pushdown)
