"""在一个一致性游标中读取有界增量集合；不以多次 OFFSET 查询拼接快照。"""
import asyncio
from app.config import settings
from app.connectors.database import get_database_connector, _validate_identifier
from app.services.incremental import field_type, typed, watermark, get_value, serialize, reject


async def read_snapshot(source, mapping, last):
    if not source.db_type:
        from app.services.api_snapshot import read_api_snapshot
        return await read_api_snapshot(source, mapping, last)
    connector = get_database_connector(source.db_type, source.db_config or {})
    try:
        schema = await connector.get_table_schema(mapping.source_table)
        column = next((c for c in schema if c['name'].lower() == mapping.incremental_field.lower()), None)
        if not column:
            reject('incremental_field_invalid', '源表不存在配置的增量字段')
        if column.get('nullable') and not column.get('primary_key'):
            reject('incremental_field_nullable', '请使用非空增量字段，避免 NULL 记录无法进入水位范围')
        keys = [c['name'] for c in schema if c.get('primary_key')]
        if not keys:
            reject('incremental_primary_key_required', '源表必须有主键，以确保相同水位下排序稳定')
        kind = field_type(column['type'])
        # Oracle DATE 含时间，按 datetime 绑定；PostgreSQL DATE 则还原为 date。
        value = typed(last, kind) if last is not None else None
        if kind == 'number' and value is not None:
            raw_type = column['type'].lower()
            if 'int' in raw_type:
                if value != value.to_integral_value():
                    reject('incremental_watermark_invalid', '整数增量字段不能使用小数水位')
                value = int(value)
            elif any(t in raw_type for t in ('real', 'double', 'float')):
                value = float(value)
        if kind == 'date' and source.db_type == 'postgresql' and value is not None:
            value = value.date()
        if source.db_type == 'sqlite' and value is not None:
            value = str(value) if kind == 'number' else str(value).replace('T', ' ')
        field = _validate_identifier(column['name'])
        table = _validate_identifier(mapping.source_table)
        ordered = [field] + [_validate_identifier(key) for key in keys if key != field]
        quote = '`' if source.db_type == 'mysql' else '"'
        q = lambda name: f'{quote}{name}{quote}'
        marker = {'sqlite': '?', 'oracle': ':watermark', 'mysql': '%s', 'postgresql': '$1'}[source.db_type]
        sql = f'SELECT * FROM {q(table)}'
        if value is not None:
            sql += (f' WHERE julianday({q(field)}) > julianday({marker})' if source.db_type == 'sqlite' and kind != 'number'
                    else f' WHERE {q(field)} > {marker}')
        sql += ' ORDER BY ' + ', '.join(q(key) for key in ordered)
        params = [] if value is None else [value]
        rows = []
        size = 0

        def collect(page):
            nonlocal size
            for row in page:
                row = dict(row)
                # 数据类型和空值在任何目标写入前校验。
                typed(get_value(row, field), kind)
                size += len(serialize(row).encode('utf-8'))
                if len(rows) >= settings.incremental_max_rows or size > settings.incremental_max_bytes:
                    reject('incremental_round_too_large', '本轮数据超过配置的条数或字节上限，请缩小数据范围')
                rows.append(row)

        batch_size = min(mapping.batch_size or 1000, 10000)
        if source.db_type == 'sqlite':
            def read_sqlite():
                connection = connector._connect_sync()
                try:
                    connection.execute('BEGIN')
                    cursor = connection.execute(sql, params)
                    while page := cursor.fetchmany(batch_size):
                        collect(page)
                finally:
                    connection.rollback()
                    connection.close()
            await asyncio.to_thread(read_sqlite)
        elif source.db_type == 'oracle':
            pool = await connector._get_pool()
            def read_oracle():
                with pool.acquire() as connection:
                    try:
                        connection.rollback()
                        with connection.cursor() as cursor:
                            cursor.execute('SET TRANSACTION READ ONLY')
                            cursor.execute(sql, {'watermark': value} if value is not None else {})
                            columns = [c[0].lower() for c in cursor.description]
                            while page := cursor.fetchmany(batch_size):
                                collect([dict(zip(columns, r)) for r in page])
                    finally:
                        connection.rollback()
            await asyncio.to_thread(read_oracle)
        elif source.db_type == 'postgresql':
            pool = await connector._get_pool()
            async with pool.acquire() as connection:
                async with connection.transaction(isolation='repeatable_read', readonly=True):
                    async for row in connection.cursor(sql, *params, prefetch=batch_size):
                        collect([row])
        else:
            import aiomysql
            pool = await connector._get_pool()
            async with pool.acquire() as connection:
                try:
                    async with connection.cursor() as cursor:
                        await cursor.execute('SELECT ENGINE FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s', [table])
                        engine = await cursor.fetchone()
                        if not engine or engine[0].lower() != 'innodb':
                            reject('incremental_snapshot_unsupported', 'MySQL 增量源必须使用 InnoDB 表')
                        await cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                        await cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
                    async with connection.cursor(aiomysql.SSDictCursor) as cursor:
                        await cursor.execute(sql, params)
                        while page := await cursor.fetchmany(batch_size):
                            collect(page)
                finally:
                    await connection.rollback()
        candidate = watermark(max(typed(get_value(row, field), kind) for row in rows), kind) if rows else last
        return rows, candidate, kind
    finally:
        await connector.disconnect()
