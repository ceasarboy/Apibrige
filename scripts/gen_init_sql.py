"""由 ORM 模型生成 scripts/init.sql（单一事实源：app/models/sync.py）

用法:
    python scripts/gen_init_sql.py           # 生成/覆盖 init.sql
    python scripts/gen_init_sql.py --check   # 仅校验一致性（CI 用），不一致退出码 1

原理: 用 PostgreSQL 方言渲染 Base.metadata 的 CreateTable/CreateIndex，
再与现有 init.sql 的表/列集合做一致性比对，杜绝 ORM 与 init.sql 漂移。
"""
import re
import sys
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable, CreateIndex

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.sync import Base  # noqa: E402

INIT_SQL = Path(__file__).resolve().parent / "init.sql"

# init.sql 中表示约束的行首关键字（不属于列定义）
_CONSTRAINT_KEYWORDS = ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT")


def render_ddl() -> str:
    """渲染 ORM 元数据为 PostgreSQL DDL 文本"""
    stmts: list[str] = []
    for table in Base.metadata.sorted_tables:
        create = CreateTable(table).compile(dialect=postgresql.dialect())
        stmts.append(str(create).strip() + ";")
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            create_idx = CreateIndex(index).compile(dialect=postgresql.dialect())
            stmts.append(str(create_idx).strip() + ";")
    return "\n\n".join(stmts) + "\n"


def parse_tables(sql_text: str) -> dict[str, set[str]]:
    """从 init.sql 文本解析 表名 → 列名集合"""
    tables: dict[str, set[str]] = {}
    current: str | None = None
    for line in sql_text.splitlines():
        stripped = line.strip()
        m = re.match(r'CREATE TABLE (?:IF NOT EXISTS )?"?(\w+)"?\s*\(', stripped, re.IGNORECASE)
        if m:
            current = m.group(1).lower()
            tables[current] = set()
            continue
        if current is None:
            continue
        if stripped.startswith(")"):
            current = None
            continue
        if not stripped or stripped.startswith("--"):
            continue
        first = stripped.split()[0].strip('"').lower() if stripped.split() else ""
        if first.upper() in _CONSTRAINT_KEYWORDS:
            continue
        tables[current].add(first)
    return tables


def metadata_tables() -> dict[str, set[str]]:
    """从 ORM 元数据取 表名 → 列名集合"""
    return {
        table.name.lower(): {col.name.lower() for col in table.columns}
        for table in Base.metadata.sorted_tables
    }


def check(existing_sql: str) -> list[str]:
    """比对 init.sql 与 ORM 元数据，返回不一致项列表（空列表 = 一致）"""
    problems: list[str] = []
    actual = parse_tables(existing_sql)
    expected = metadata_tables()
    for table in expected:
        if table not in actual:
            problems.append(f"init.sql 缺少表: {table}")
            continue
        missing = expected[table] - actual[table]
        extra = actual[table] - expected[table]
        for col in sorted(missing):
            problems.append(f"init.sql 表 {table} 缺少列: {col}")
        for col in sorted(extra):
            problems.append(f"init.sql 表 {table} 多出列: {col}（ORM 中不存在）")
    for table in actual:
        if table not in expected:
            problems.append(f"init.sql 多出表: {table}（ORM 中不存在）")
    return problems


def main() -> int:
    if "--check" in sys.argv:
        if not INIT_SQL.exists():
            print(f"FAIL: {INIT_SQL} 不存在")
            return 1
        problems = check(INIT_SQL.read_text(encoding="utf-8"))
        if problems:
            print("init.sql 与 ORM 模型不一致:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print(f"OK: init.sql 与 ORM 模型一致（{len(metadata_tables())} 张表）")
        return 0

    ddl = render_ddl()
    INIT_SQL.write_text(ddl, encoding="utf-8")
    problems = check(ddl)
    if problems:  # 自检：刚生成的文本必须与元数据一致
        print("生成后自检失败（生成器 bug）:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"OK: 已生成 {INIT_SQL}（{len(metadata_tables())} 张表），自检一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
