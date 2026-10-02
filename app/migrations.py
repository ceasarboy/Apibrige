"""显式、可重复的增量轮次迁移；运行前停止 API/Worker 并备份数据库。"""
import asyncio
from sqlalchemy import inspect, text
from app.models.sync import Base


VERSION = '20260929_bi_access_scope'
ADDITIONS = {
    'connection_fields': {'field_id': 'VARCHAR(200)'},
    'sync_tasks': {'active_round_id': 'UUID', 'checkpoint_version': 'INTEGER NOT NULL DEFAULT 0'},
    'data_buffer': {'config_snapshot': 'TEXT', 'round_id': 'UUID', 'batch_no': 'INTEGER', 'attempt_token': 'VARCHAR(36)',
                    'lease_until': 'TIMESTAMP', 'next_retry_at': 'TIMESTAMP'},
}


def upgrade(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise RuntimeError('平台迁移仅支持 SQLite/PostgreSQL')
    if connection.dialect.name == 'postgresql':
        connection.execute(text('SELECT pg_advisory_xact_lock(20260910)'))
    tables = inspect(connection).get_table_names()
    for table, additions in ADDITIONS.items():
        if table not in tables:
            continue
        existing = {c['name'] for c in inspect(connection).get_columns(table)}
        for column, sql_type in additions.items():
            if column not in existing:
                # SQLite 中与 SQLAlchemy UUID 的 CHAR(32) 存储兼容。
                kind = 'CHAR(32)' if sql_type == 'UUID' and connection.dialect.name == 'sqlite' else sql_type
                connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {kind}'))
    from app.models import bi as _bi_models
    Base.metadata.create_all(connection)
    bi_columns = {c['name'] for c in inspect(connection).get_columns('bi_metric_defs')}
    for name in ('style', 'computed', 'display'):
        if name not in bi_columns:
            connection.execute(text(f'ALTER TABLE bi_metric_defs ADD COLUMN {name} JSON'))
    connection.execute(text('CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(100) PRIMARY KEY)'))
    connection.execute(text('INSERT INTO schema_migrations(version) VALUES (:version) ON CONFLICT DO NOTHING'),
                       {'version': '20260928_bi_monitoring'})
    connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS idx_round_batch ON data_buffer (round_id, batch_no)'))
    connection.execute(text('CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(100) PRIMARY KEY)'))
    connection.execute(text('INSERT INTO schema_migrations(version) VALUES (:version) ON CONFLICT DO NOTHING'), {'version': VERSION})


def check_schema(connection):
    tables = inspect(connection).get_table_names()
    if 'api_connections' in tables and ({'connection_forms', 'connection_fields', 'hap_write_plans'} - set(tables)):
        raise RuntimeError('数据库需要升级：备份并停止服务后运行 python -m app.migrations')
    for table, columns in ADDITIONS.items():
        if table in tables and set(columns) - {c['name'] for c in inspect(connection).get_columns(table)}:
            raise RuntimeError('数据库需要升级：备份并停止服务后运行 python -m app.migrations')


def check_bi_schema(connection):
    required = {'bi_metric_defs', 'bi_metric_buckets', 'bi_aggregation_watermark',
                'bi_screen_settings', 'bi_access_profiles'}
    tables = set(inspect(connection).get_table_names())
    if 'api_connections' in tables and not required.issubset(tables):
        raise RuntimeError('BI requires migration: stop services, back up, run python -m app.migrations')
    if 'bi_metric_defs' in tables:
        columns = {c['name'] for c in inspect(connection).get_columns('bi_metric_defs')}
        if not {'style', 'computed', 'display'}.issubset(columns):
            raise RuntimeError('BI requires migration: python -m app.migrations')


async def main():
    from app.database import engine
    async with engine.begin() as connection:
        await connection.run_sync(upgrade)
    await engine.dispose()
    print('Incremental round migration complete.')


if __name__ == '__main__':
    asyncio.run(main())
