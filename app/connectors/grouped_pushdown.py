"""聚合下推执行器（add-bi-sql-pushdown）——本项目**唯一**新增的源库 SQL 执行点

受控例外评审材料（见 openspec/changes/add-bi-sql-pushdown/design.md §6.5）：
把"新增执行点"收敛为**单一模块的单一函数** `fetch_grouped_rows`，安全三件套齐全：

1. **标识符白名单**：表名/列名一律先过 `_validate_identifier`（纯 ASCII），
   再交给 SQLAlchemy `Column`/`Table`（中文列/空格/点号/超长 → 直接拒绝）；
2. **全值绑定**：过滤值/分页进参数占位（qmark / format / numeric_dollar），
   SQL 文本里不含业务值（单测断言 `SECRET_VALUE not in sql`）；
3. **表达式编译**：SQL 文本由 SQLAlchemy 表达式 API 编译产出（`_build_grouped_sql`），
   本模块不做任何字符串拼接。

能力开关：连接器类属性 `supports_group_pushdown`（Oracle 默认 False）——
调用方（`bi_sources.can_pushdown`）前置判定，不可用即回退内存路径，
故本执行点可全局关闭。

参照组织（design §6.5 第 1 条）：DataEase 把源库 SQL 执行收敛在
`datasource/provider/` 包、标识符校验是 Provider 基类共享契约——本模块与之同构：
执行语料只此一处，方言差异（占位风格/游标形态）在同文件内分派。

方言执行分派（占位与驱动一一对应）：

| DB_TYPE     | 驱动            | 占位         | 执行形态                        |
|-------------|-----------------|--------------|---------------------------------|
| sqlite      | sqlite3（同步） | `?`          | asyncio.to_thread + 短连接      |
| postgresql  | asyncpg（池）   | `$1`         | 池连接 `conn.fetch(sql, *args)` |
| mysql       | aiomysql（池）  | `%s`         | 池连接 `DictCursor`             |

Oracle 未接入（`supports_group_pushdown=False`）：11g 的 ROWNUM 双层分页与聚合/排序
组合行为需单独验证。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.connectors.database import _build_grouped_sql, _rename_grouped_row

logger = logging.getLogger(__name__)

# 单次下推返回的维度组合行数硬上限（超出即判"不可下推"并回退内存路径，
# 避免高基数维度把分组结果灌满内存；取值对齐 DEFAULT_SHARD_THRESHOLD 量级）
GROUP_ROW_CAP = 200_000


class PushdownUnsupported(Exception):
    """连接器不支持聚合下推（能力标志 / 未知方言）"""


class PushdownTooLarge(Exception):
    """分组结果超过 GROUP_ROW_CAP（维度基数过高）"""


async def fetch_grouped_rows(
    connector: Any,
    table: str,
    group_fields: list[str],
    aggs: list[dict],
    filters: list[dict] | None = None,
    order_by: list[tuple[str, str]] | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[dict]:
    """执行一次下推式分组聚合，返回按逻辑键命名的行

    - `group_fields`：维度列（空 = 无维度汇总，单行结果）
    - `aggs`：`[{"key","func","field"}]`；func ∈ count/count_distinct/sum/avg/min/max
    - `filters`：`[{"field","op","value","null_match"?}]`；op ∈
      eq/ne/gt/ge/lt/le/in/not_in/between/not_between（`null_match` 由调用方按
      内存匹配器的 NULL 结果给出，见 database._build_grouped_sql）
    - 返回：[{维度列原名: 值, aggs[].key: 值}]；数值已归一（count → int，
      其余 → float，NULL → None），维度保留源值（NULL 不在此处改写）
    - 超出 GROUP_ROW_CAP → `PushdownTooLarge`；能力不满足 → `PushdownUnsupported`
    """
    db_type = str(getattr(connector, "DB_TYPE", "") or "")
    executor = _EXECUTORS.get(db_type)
    if executor is None or not getattr(connector, "supports_group_pushdown", False):
        raise PushdownUnsupported(f"连接器不支持聚合下推: {db_type or type(connector).__name__}")

    cap = GROUP_ROW_CAP
    probe = cap + 1                     # 多取一行用于区分"恰好 cap"与"超过 cap"
    if limit is None or int(limit) > probe:
        limit = probe

    sql, params, alias_map = _build_grouped_sql(
        table, group_fields, aggs, filters, order_by, limit, offset, dialect=db_type)

    rows = await executor(connector, sql, params)
    if len(rows) > cap:
        raise PushdownTooLarge(
            f"分组结果超过上限 {cap}（维度基数过高），回退内存路径")
    return [_rename_grouped_row(r, alias_map, aggs, group_fields) for r in rows]


# ------------------------------------------------------------
# 方言执行（本执行点内部：占位风格与驱动形态一一对应）
# ------------------------------------------------------------

async def _exec_sqlite(connector: Any, sql: str, params: list) -> list[dict]:
    """sqlite3 为同步驱动：后台线程 + 短连接（与连接器既有读路径同款）"""
    def _do() -> list[dict]:
        conn = connector._connect_sync()
        try:
            cur = conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    return await asyncio.to_thread(_do)


async def _exec_postgresql(connector: Any, sql: str, params: list) -> list[dict]:
    """asyncpg：numeric_dollar 占位（$1…），位置参数按序展开"""
    pool = await connector._get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


async def _exec_mysql(connector: Any, sql: str, params: list) -> list[dict]:
    """aiomysql：format 占位（%s）；优先 DictCursor，缺失时按 description 组键"""
    try:
        from aiomysql import DictCursor  # 局部导入，缺失时走兜底路径
        cursor_cls = DictCursor
    except ImportError:
        cursor_cls = None

    pool = await connector._get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(cursor_cls) as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
            if cursor_cls is None and rows:
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in rows]
            return list(rows)


_EXECUTORS = {
    "sqlite": _exec_sqlite,
    "postgresql": _exec_postgresql,
    "mysql": _exec_mysql,
}
