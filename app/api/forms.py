"""表单注册表 API — 连接下可调用表单的登记/拉取/命名与桥引用统计

桥是表单到表单的：先在注册表里知道"哪些表单可用"，才能建桥。
注册表不参与同步执行链路——删除注册记录不影响已建映射。
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.database import get_session
from app.models.sync import ApiConnection, ConnectionForm
from app.schemas import (
    FormCreate, FormUpdate, FormResponse, FormSyncResponse, MessageResponse,
)
from app.services.form_registry import (
    merge_forms, attach_bridge_refs, FormSyncUnsupportedError,
)

router = APIRouter()


async def _to_response(session: AsyncSession, forms: list[ConnectionForm]) -> list[dict]:
    """ORM → 响应 dict（合并所属连接名与桥引用统计）"""
    conn_ids = {f.connection_id for f in forms}
    conn_names: dict = {}
    if conn_ids:
        rows = (await session.execute(
            select(ApiConnection).where(ApiConnection.id.in_(conn_ids))
        )).scalars().all()
        conn_names = {c.id: c.name for c in rows}
    await attach_bridge_refs(session, forms)
    return [{
        "id": f.id,
        "connection_id": f.connection_id,
        "connection_name": conn_names.get(f.connection_id, ""),
        "form_key": f.form_key,
        "original_name": f.original_name,
        "display_name": f.display_name,
        "remark": f.remark,
        "source": f.source,
        "bridge_count": getattr(f, "bridge_count", 0),
        "bridges": getattr(f, "bridges", []),
        "created_at": f.created_at,
        "updated_at": f.updated_at,
    } for f in forms]


@router.get("", response_model=list[FormResponse])
async def list_forms(
    connection_id: UUID | None = Query(None, description="按连接筛选（空=全部）"),
    session: AsyncSession = Depends(get_session),
):
    """查询表单注册表（平铺，含桥引用统计）"""
    stmt = select(ConnectionForm).order_by(ConnectionForm.created_at.desc())
    if connection_id:
        stmt = stmt.where(ConnectionForm.connection_id == connection_id)
    forms = (await session.execute(stmt)).scalars().all()
    return await _to_response(session, list(forms))


@router.post("/{connection_id}/sync", response_model=FormSyncResponse)
async def sync_forms(connection_id: UUID, session: AsyncSession = Depends(get_session)):
    """从源端拉取表单清单合并入注册表

    - DB 连接：列出全部表；HAP 连接：app/get 列出全部工作表
    - 合并规则：新 form_key 插入；已存在仅刷新原始名（不覆盖用户命名/备注）；
      源端已消失的保留不删
    """
    conn = await session.get(ApiConnection, connection_id)
    if conn is None:
        raise HTTPException(404, "连接不存在")
    try:
        result = await merge_forms(session, conn)
    except FormSyncUnsupportedError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        # 拉取失败（网络/鉴权/版本）不吞错：给明确原因，功能其余部分不受影响
        raise HTTPException(502, "从源端拉取表单失败，请检查连接配置及服务状态") from None
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, '表单注册表已变化，请刷新后重试') from None
    return result


@router.post("", response_model=FormResponse, status_code=201)
async def create_form(payload: FormCreate, session: AsyncSession = Depends(get_session)):
    """手动登记表单（source=manual；无结构协议连接的主要登记方式）"""
    conn = await session.get(ApiConnection, payload.connection_id)
    if conn is None:
        raise HTTPException(404, "连接不存在")
    if not payload.form_key.strip():
        raise HTTPException(422, '表单标识不能为空')
    dup = (await session.execute(
        select(ConnectionForm).where(
            ConnectionForm.connection_id == payload.connection_id,
        )
    )).scalars().all()
    if any(f.form_key.strip().lower() == payload.form_key.strip().lower() for f in dup):
        raise HTTPException(409, f"该连接下已存在表单 {payload.form_key}")
    form = ConnectionForm(
        connection_id=payload.connection_id,
        form_key=payload.form_key.strip(),
        original_name=payload.display_name or payload.form_key,
        display_name=payload.display_name or payload.form_key,
        remark=payload.remark or "",
        source="manual",
    )
    session.add(form)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, '该表单已登记，请刷新后重试') from None
    await session.refresh(form)
    return (await _to_response(session, [form]))[0]


@router.put("/{form_id}", response_model=FormResponse)
async def update_form(form_id: UUID, payload: FormUpdate,
                      session: AsyncSession = Depends(get_session)):
    """编辑表单（仅显示名/备注；原始名与来源不变）"""
    form = await session.get(ConnectionForm, form_id)
    if form is None:
        raise HTTPException(404, "表单不存在")
    if payload.display_name is not None:
        form.display_name = payload.display_name
    if payload.remark is not None:
        form.remark = payload.remark
    await session.commit()
    await session.refresh(form)
    return (await _to_response(session, [form]))[0]


@router.delete("/{form_id}", response_model=MessageResponse)
async def delete_form(form_id: UUID, session: AsyncSession = Depends(get_session)):
    """删除表单注册记录（仅清理登记信息，不影响已建映射与任务）"""
    form = await session.get(ConnectionForm, form_id)
    if form is None:
        raise HTTPException(404, "表单不存在")
    await session.delete(form)
    await session.commit()
    return {"message": "表单注册记录已删除（已建映射不受影响）"}
