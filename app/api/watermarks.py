"""人工水位设置：与建轮共用事务锁，比较版本并保留审计。"""
import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal
from uuid import UUID
from pydantic import BaseModel, Field, StrictBool, model_validator
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_session
from app.models.sync import SyncTask, FieldMapping, ApiConnection, DataBuffer, HapWritePlan, ExecutionLog
from app.services.incremental_rounds import lock_task
from app.services.incremental import field_type, watermark, api_watermark
from app.services.sync_config import side_config

router = APIRouter()

class WatermarkUpdate(BaseModel):
    value: str | None = Field(..., max_length=200)
    expected_version: int = Field(..., ge=0, strict=True)
    expected_value: str | None = Field(...)
    config_token: str = Field(..., min_length=64, max_length=64)
    confirmed: StrictBool

    @model_validator(mode='after')
    def require_confirmation(self):
        if not self.confirmed:
            raise ValueError('必须二次确认后才能修改水位')
        return self

async def context(session, task):
    mapping = await session.get(FieldMapping, task.mapping_id, populate_existing=True)
    if mapping is None or mapping.sync_mode != 'incremental':
        raise HTTPException(422, '只有增量任务支持手动设置水位')
    source = await session.get(ApiConnection, mapping.source_connection_id, populate_existing=True)
    if source is None or not mapping.incremental_field:
        raise HTTPException(422, '源连接或增量字段未配置')
    raw_type = ''
    try:
        if source.db_type:
            from app.connectors.database import get_database_connector
            connector = get_database_connector(source.db_type, source.db_config or {})
            try:
                schema = await connector.get_table_schema(mapping.source_table)
            finally:
                await connector.disconnect()
            column = next((c for c in schema if c['name'].lower() == mapping.incremental_field.lower()), None)
            if not column:
                raise HTTPException(422, '源表中不存在配置的增量字段')
            raw_type = column['type']
            kind = field_type(raw_type)
            if source.db_type == 'oracle' and kind == 'date':
                kind = 'datetime'
        elif source.protocol == 'hap':
            from app.adapters.factory import ProtocolAdapterFactory
            from app.services.adapter_config import build_adapter_config
            adapter = ProtocolAdapterFactory.create('hap', build_adapter_config(source, mapping, 'source'))
            cfg = side_config(mapping.mapping_config or {}, 'source')
            controls = await adapter.get_worksheet_controls(cfg.get('worksheet_id') or mapping.source_endpoint)
            column = next((c for c in controls if mapping.incremental_field in (c.get('controlId'), c.get('alias'))), None)
            kind = {6:'number',8:'number',15:'date',16:'datetime'}.get(column.get('type')) if column else None
        else:
            kind = side_config(mapping.mapping_config or {}, 'source').get('incremental', {}).get('field_type')
        if kind not in ('number','date','datetime','datetime_tz'):
            raise HTTPException(422, '无法确定水位类型，请检查源增量配置')
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, '读取源字段类型失败，请检查源连接后重试') from None
    identity = {'mapping_id':str(mapping.id), 'source_id':str(source.id), 'source_table':mapping.source_table,
        'source_endpoint':mapping.source_endpoint, 'incremental_field':mapping.incremental_field,
        'source_protocol':side_config(mapping.mapping_config or {}, 'source'), 'kind':kind, 'raw_type':raw_type,
        'base_url':source.base_url, 'db_type':source.db_type,
        'db_identity':{k:v for k,v in (source.db_config or {}).items() if k != 'password'}}
    token = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
    return {'task_id':str(task.id), 'mapping_name':mapping.name, 'field':mapping.incremental_field,
        'type':kind, 'value':task.last_sync_value, 'version':task.checkpoint_version,
        'config_token':token, 'raw_type':raw_type, 'api_time_compatible':not bool(source.db_type), 'active_round_id':str(task.active_round_id) if task.active_round_id else None}

async def ensure_idle(session, task):
    if task.active_round_id:
        raise HTTPException(409, '存在未完成轮次，不能修改水位；失败轮次也必须先处理')
    pending = await session.scalar(select(func.count()).select_from(DataBuffer).where(
        DataBuffer.task_id == task.id, DataBuffer.status.in_(['pending','processing','failed','validation_failed'])))
    plans = await session.scalar(select(func.count()).select_from(HapWritePlan).where(
        HapWritePlan.task_id == task.id, HapWritePlan.active_target.isnot(None)))
    if pending or plans:
        raise HTTPException(409, '存在未完成批次或写入计划，不能修改水位')

@router.get('/{task_id}/watermark')
async def get_watermark(task_id: UUID, session: AsyncSession = Depends(get_session)):
    task = await session.get(SyncTask, task_id)
    if task is None:
        raise HTTPException(404, '任务不存在')
    await ensure_idle(session, task)
    result = await context(session, task)
    result.pop('raw_type')
    return result

@router.put('/{task_id}/watermark')
async def set_watermark(task_id: UUID, payload: WatermarkUpdate, session: AsyncSession = Depends(get_session)):
    task = await lock_task(session, task_id)
    if task is None:
        raise HTTPException(404, '任务不存在')
    await ensure_idle(session, task)
    if task.checkpoint_version != payload.expected_version or task.last_sync_value != payload.expected_value:
        raise HTTPException(409, '水位已被其他操作修改，请刷新并重新确认')
    info = await context(session, task)
    if info['config_token'] != payload.config_token:
        raise HTTPException(409, '增量来源或字段配置已变化，请刷新并重新确认')
    value = payload.value
    if value is not None:
        value = value.strip()
        if not value:
            raise HTTPException(422, '水位不能为空字符串；重新全量读取请明确选择清空水位')
        kind = info['type']
        if kind == 'date' and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise HTTPException(422, '日期水位格式应为 YYYY-MM-DD')
        if kind in ('datetime','datetime_tz') and not re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}',value):
            raise HTTPException(422, '时间水位应包含日期和时间，例如 2026-09-14 00:00:00')
        value = api_watermark(value, kind) if info['api_time_compatible'] else watermark(value, kind)
        if kind == 'date':
            value = datetime.fromisoformat(value).date().isoformat()
        if kind == 'number' and 'int' in info['raw_type'].lower() and Decimal(value) != Decimal(value).to_integral_value():
            raise HTTPException(422, '整数增量字段不能使用小数水位')
    old = task.last_sync_value
    task.last_sync_value = value
    task.checkpoint_version += 1
    session.add(ExecutionLog(task_id=task.id, action='watermark_manual',status='success',
        response_data={'old_watermark':old,'new_watermark':value,'field':info['field'],
            'checkpoint_version':task.checkpoint_version,'confirmed':True,'via':'management_ui'}))
    await session.commit()
    return {'value':value,'version':task.checkpoint_version,'message':'水位已更新；下次按增量字段严格大于该值读取'}