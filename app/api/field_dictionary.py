"""字段字典保存；规则只对显式启用的映射生效。"""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_session
from app.models.sync import ApiConnection, ConnectionField, FieldMapping
from app.schemas import ConnectionFieldSave
from app.services.field_policy import definitions, identity, effective_config
from app.adapters.errors import AdapterError

router = APIRouter()


@router.put('/{conn_id}/fields')
async def save_fields(conn_id: UUID, payload: ConnectionFieldSave, table: str | None = None,
                      worksheet_id: str | None = None, session: AsyncSession = Depends(get_session)):
    conn = await session.get(ApiConnection, conn_id)
    if conn is None:
        raise HTTPException(404, '连接不存在')
    scope = table if conn.db_type else worksheet_id if conn.protocol == 'hap' else ''
    if conn.db_type or conn.protocol == 'hap':
        if not scope or not scope.strip():
            raise HTTPException(422, '请选择表或工作表')
    else:
        raise HTTPException(422, '该协议无自动字段定义')
    scope = identity(conn, scope)
    defs = await definitions(session, conn, scope)
    seen = set()
    for item in payload.fields:
        key = identity(conn, item.field_id or item.path)
        if not key or key in seen:
            raise HTTPException(422, '字段标识不能为空或重复')
        seen.add(key)
        if conn.protocol == 'hap' and key in ('rowid', 'ctime', 'utime') and item.writable:
            raise HTTPException(422, '系统字段不可写')
        d = defs.get(key) or defs.get(identity(conn, item.path))
        if d is None:
            d = ConnectionField(connection_id=conn.id, scope=scope, path=identity(conn, item.path))
            session.add(d)
        d.path = identity(conn, item.path)
        d.field_id = item.field_id
        d.name, d.readable, d.writable = item.name, item.readable, item.writable
    try:
        await session.flush()
        # 拒绝破坏现有启用映射的关键字段规则；在途轮次始终使用旧快照。
        mappings = (await session.scalars(select(FieldMapping).where(
            (FieldMapping.source_connection_id == conn.id) | (FieldMapping.target_connection_id == conn.id)))).all()
        affected = []
        for mapping in mappings:
            if (mapping.mapping_config or {}).get('field_policy') == 'inherit':
                await effective_config(session, mapping)
                affected.append({'id': str(mapping.id), 'name': mapping.name})
        await session.commit()
    except AdapterError as exc:
        await session.rollback()
        raise HTTPException(422, exc.as_dict()) from None
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, '字段字典已被其他操作修改，请刷新后重试') from None
    return {'saved': len(seen), 'affected_mappings': affected,
            'message': '规则用于后续执行；已提交批次和轮次保留原规则，相关探针批准时需核对规则版本'}
