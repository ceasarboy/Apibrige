"""字段映射 API — CRUD + 转换预览"""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_session
from app.models.sync import FieldMapping, ApiConnection
from app.schemas import (
    MappingCreate, MappingUpdate, MappingResponse,
    MappingPreviewRequest, MappingPreviewResponse, MessageResponse,
)
from app.services.transformer import transform_data
from app.security.headers import store_headers, mask_headers
from app.security.crypto import CryptoError
from app.security.desensitize import desensitize_record
from app.adapters.errors import AdapterError
from app.services.sync_config import validate_mapping_config


def _mapping_response(mapping):
    out = MappingResponse.model_validate(mapping, from_attributes=True).model_dump()
    for key in ('source_headers', 'target_headers'):
        out[key] = mask_headers(out.get(key))
    return out


def _stored_headers(value, old=None):
    try:
        return store_headers(value, old)
    except (ValueError, CryptoError) as exc:
        raise HTTPException(422, str(exc)) from None

router = APIRouter()


@router.post("", response_model=MappingResponse, status_code=201)
async def create_mapping(
    payload: MappingCreate,
    session: AsyncSession = Depends(get_session),
):
    """创建字段映射"""
    from app.services.incremental_config import validate_mapping_change
    checked = payload.model_dump()
    await validate_mapping_change(session, checked)
    payload = MappingCreate(**checked)
    mapping = FieldMapping(
        name=payload.name,
        source_connection_id=payload.source_connection_id,
        target_connection_id=payload.target_connection_id,
        source_endpoint=payload.source_endpoint,
        target_endpoint=payload.target_endpoint,
        source_table=payload.source_table,
        target_table=payload.target_table,
        source_headers=_stored_headers(payload.source_headers),
        target_headers=_stored_headers(payload.target_headers),
        mapping_config=payload.mapping_config,
        transform_script=payload.transform_script,
        sync_mode=payload.sync_mode,
        incremental_field=payload.incremental_field,
        batch_size=payload.batch_size,
    )
    session.add(mapping)
    await session.commit()
    await session.refresh(mapping)
    return _mapping_response(mapping)


@router.get("", response_model=list[MappingResponse])
async def list_mappings(session: AsyncSession = Depends(get_session)):
    """查询映射列表"""
    result = await session.execute(
        select(FieldMapping).order_by(FieldMapping.created_at.desc())
    )
    return [_mapping_response(m) for m in result.scalars().all()]


@router.get("/{mapping_id}", response_model=MappingResponse)
async def get_mapping(mapping_id: UUID, session: AsyncSession = Depends(get_session)):
    """查询单个映射"""
    mapping = await session.get(FieldMapping, mapping_id)
    if mapping is None:
        raise HTTPException(404, "映射不存在")
    return _mapping_response(mapping)


@router.put("/{mapping_id}", response_model=MappingResponse)
async def update_mapping(
    mapping_id: UUID,
    payload: MappingUpdate,
    session: AsyncSession = Depends(get_session),
):
    """更新字段映射"""
    mapping = await session.get(FieldMapping, mapping_id)
    if mapping is None:
        raise HTTPException(404, "映射不存在")

    update_data = payload.model_dump(exclude_unset=True)
    from app.api._guards import reject_null_non_nullable
    update_data = reject_null_non_nullable(FieldMapping, update_data)
    from app.services.incremental_config import validate_mapping_change
    await validate_mapping_change(session, update_data, mapping)
    for key in ('source_headers', 'target_headers'):
        if key in update_data:
            update_data[key] = _stored_headers(update_data[key], getattr(mapping, key))
    for field, value in update_data.items():
        setattr(mapping, field, value)

    await session.commit()
    await session.refresh(mapping)
    return _mapping_response(mapping)


@router.delete("/{mapping_id}", response_model=MessageResponse)
async def delete_mapping(mapping_id: UUID, session: AsyncSession = Depends(get_session)):
    """删除字段映射"""
    from app.models.sync import SyncTask
    from sqlalchemy import func
    if await session.scalar(select(func.count()).select_from(SyncTask).where(SyncTask.mapping_id == mapping_id)):
        raise HTTPException(409, '映射已被任务引用，不能删除')
    mapping = await session.get(FieldMapping, mapping_id)
    if mapping is None:
        raise HTTPException(404, "映射不存在")

    await session.delete(mapping)
    await session.commit()
    return MessageResponse(message="映射已删除", id=mapping_id)


@router.post("/{mapping_id}/preview", response_model=MappingPreviewResponse)
async def preview_mapping(
    mapping_id: UUID,
    payload: MappingPreviewRequest,
    session: AsyncSession = Depends(get_session),
):
    """预览映射转换结果"""
    mapping = await session.get(FieldMapping, mapping_id)
    if mapping is None:
        raise HTTPException(404, "映射不存在")

    try:
        from app.services.field_policy import freeze
        mapping = await freeze(session, mapping)
        transformed = transform_data(payload.test_data, mapping.mapping_config)
        return MappingPreviewResponse(success=True, transformed_data=desensitize_record(transformed))
    except Exception as e:
        return MappingPreviewResponse(success=False, error=str(e))


from pydantic import BaseModel


class RequestPreview(BaseModel):
    test_data: dict | list[dict]
    draft: MappingUpdate | None = None


@router.post('/{mapping_id}/hap-preview')
async def hap_preview(mapping_id: UUID, payload: RequestPreview, session: AsyncSession = Depends(get_session)):
    """使用提交样本，只读查询目标后给出新增/修改计划；不保存配置。"""
    from types import SimpleNamespace
    from app.services.field_policy import freeze
    from app.services.hap_bulk import preview, is_upsert
    from app.services.transformer import transform_payload
    from app.services.adapter_config import build_adapter_config
    from app.adapters.factory import ProtocolAdapterFactory
    from app.services.validator import validate_batch
    mapping = await session.get(FieldMapping, mapping_id)
    if mapping is None:
        raise HTTPException(404, '映射不存在')
    values = MappingResponse.model_validate(mapping, from_attributes=True).model_dump()
    if payload.draft:
        changes = payload.draft.model_dump(exclude_unset=True)
        for key in ('source_headers','target_headers'):
            if key in changes:
                changes[key] = _stored_headers(changes[key], values.get(key))
        values.update(changes)
    candidate = await freeze(session, SimpleNamespace(**values))
    target = await session.get(ApiConnection, candidate.target_connection_id)
    if target is None or target.protocol != 'hap' or not is_upsert(candidate):
        raise HTTPException(422, '仅支持 HAP 新增更新预览')
    rows = transform_payload(payload.test_data, candidate.mapping_config)
    if len(rows) > 20:
        raise HTTPException(422, '预览最多 20 条样本')
    if not validate_batch(rows, candidate.mapping_config.get('validations') or {})['valid']:
        raise HTTPException(422, '样本未通过字段校验')
    adapter = ProtocolAdapterFactory.create('hap', build_adapter_config(target, candidate, 'target'))
    try:
        return await preview(adapter, candidate, rows)
    except AdapterError as exc:
        raise HTTPException(422, exc.as_dict()) from None


@router.post('/{mapping_id}/request-preview')
async def request_preview(mapping_id: UUID, payload: RequestPreview, session: AsyncSession = Depends(get_session)):
    """只准备请求；不调用目标或源网络接口，不保存草稿。"""
    from types import SimpleNamespace
    from app.services.adapter_config import build_adapter_config
    from app.services.transformer import transform_payload
    from app.services.validator import validate_batch
    from app.adapters.factory import ProtocolAdapterFactory
    mapping = await session.get(FieldMapping, mapping_id)
    if mapping is None:
        raise HTTPException(404, '映射不存在')
    values = MappingResponse.model_validate(mapping, from_attributes=True).model_dump()
    if payload.draft:
        draft = payload.draft.model_dump(exclude_unset=True)
        for key in ('source_headers', 'target_headers'):
            if key in draft:
                draft[key] = _stored_headers(draft[key], values.get(key))
        values.update(draft)
    candidate = SimpleNamespace(**values)
    conn = await session.get(ApiConnection, candidate.target_connection_id)
    if conn is None:
        raise HTTPException(404, '目标连接不存在')
    try:
        validate_mapping_config(candidate.mapping_config)
        from app.services.field_policy import freeze
        candidate = await freeze(session, candidate)
        records = transform_payload(payload.test_data, candidate.mapping_config)
        validation = validate_batch(records, candidate.mapping_config.get('validations') or {})
        cfg = build_adapter_config(conn, candidate, 'target')
        prepared = None
        if conn.protocol in ('rest', 'http') and not conn.db_type:
            adapter = ProtocolAdapterFactory.create(conn.protocol, cfg)
            prepared = adapter.prepare_push_request(candidate.target_endpoint or '', records)
            # OAuth2预览不取token；显示静态认证头（全部敏感值脱敏）。
            if conn.auth_type != 'oauth2':
                prepared['headers'], _ = adapter.auth.inject(prepared['headers'])
            prepared['headers'] = mask_headers(prepared['headers'])
        return desensitize_record({'success': validation['valid'], 'zero_write': True,
                                  'transformed_records': records, 'validation': validation,
                                  'prepared_request': prepared,
                                  'warnings': ['使用提交样本离线预览，不代表真实源L2已通过。']})
    except (ValueError, CryptoError) as exc:
        error = exc.as_dict() if isinstance(exc, AdapterError) else {'code':'invalid_config', 'message':'配置或凭据无效，请检查配置路径'}
        return {'success': False, 'zero_write': True, 'error': error}
