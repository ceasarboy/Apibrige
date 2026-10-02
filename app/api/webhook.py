"""Webhook 接收端点（task 2.4）

POST /api/v1/webhook/{task_id}：
    - 外部系统推送数据 → 按 task.sync_mode 走同步/异步处理（source_data 直接注入）
    - webhook_config 可配置 secret（{"secret": "..."}），请求头 X-Webhook-Secret 必须匹配
"""
from uuid import UUID
import json
import secrets
from app.config import settings

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.sync import ApiConnection, FieldMapping, SyncTask
from app.services import async_handler, sync_handler

router = APIRouter()


async def _load_webhook_context(session: AsyncSession, task_id: UUID):
    """加载任务及其映射/连接；校验任务可接收 Webhook"""
    task = await session.get(SyncTask, task_id)
    if task is None or task.status != "active":
        raise HTTPException(404, "任务不存在或未激活")
    if not task.mapping_id:
        raise HTTPException(409, "任务未绑定字段映射")

    mapping = await session.get(FieldMapping, task.mapping_id)
    if mapping is None:
        raise HTTPException(409, "字段映射不存在")
    source_conn = await session.get(ApiConnection, mapping.source_connection_id)
    target_conn = await session.get(ApiConnection, mapping.target_connection_id)
    if source_conn is None or target_conn is None:
        raise HTTPException(409, "映射引用的连接不存在")
    return task, mapping, source_conn, target_conn


def _check_secret(task: SyncTask, provided: str | None) -> None:
    """webhook 必须配置 secret 且匹配（恒定时间比较）

    本端点豁免管理 token（外部系统不持有管理令牌，见 main.py）——无 secret 即等于
    完全开放的写入入口，故默认强制要求（APIBRIDGE_WEBHOOK_REQUIRE_SECRET=false
    可显式放开，仅建议在已由网络/网关层挡住的场景使用）。
    """
    config = task.webhook_config or {}
    expected = config.get("secret")
    if not expected:
        if settings.webhook_require_secret:
            raise HTTPException(409, detail={
                "code": "webhook_secret_required",
                "message": "该任务未配置 webhook secret，请在任务的 webhook_config 中设置后重试"})
        return
    try:
        if not provided or not secrets.compare_digest(str(expected), provided):
            raise HTTPException(401, detail={"code": "invalid_webhook_secret",
                                             "message": "X-Webhook-Secret 校验失败"})
    except TypeError:
        # 非 ASCII 输入（HTTP 头 latin-1 解码放行 0x80-0xFF）会让 compare_digest
        # 抛 TypeError → 未认证 500（审查 P3）；按校验失败处理
        raise HTTPException(401, detail={"code": "invalid_webhook_secret",
                                         "message": "X-Webhook-Secret 校验失败"})


@router.post("/{task_id}")
async def receive_webhook(
    task_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    x_webhook_secret: str | None = Header(default=None),
):
    """接收外部系统推送并触发同步"""
    task, mapping, source_conn, target_conn = await _load_webhook_context(
        session, task_id)
    _check_secret(task, x_webhook_secret)
    from app.services.hap_target import enabled
    if enabled(mapping):
        raise HTTPException(422, 'HAP 目标策略不接受 Webhook 数据')
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if settings.webhook_max_body_bytes and len(body) > settings.webhook_max_body_bytes:
            raise HTTPException(413, 'Webhook 请求体超过配置上限')
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        raise HTTPException(422, '请求体必须是 JSON 对象') from None
    if not isinstance(payload, dict):
        raise HTTPException(422, '请求体必须是 JSON 对象')
    if mapping.sync_mode == 'incremental':
        raise HTTPException(422, detail={'code': 'incremental_push_unsupported',
                                         'message': '水位增量任务不接受 Webhook 推入数据'})
    # 删除对账任务同样不接受推入数据：推入的单条不是完整源集合，
    # 删差会把"目标里除这条之外的所有行"当成陈旧行删除（P0 数据丢失向量，审查实测）
    if (mapping.mapping_config or {}).get('delete_sync') or (mapping.mapping_config or {}).get('hap_delete_sync'):
        raise HTTPException(422, detail={'code': 'delete_sync_push_unsupported',
                                         'message': '删除对账任务不接受 Webhook 推入数据（推入的单条不是完整源集合，会误删目标其余行）'})

    if task.sync_mode == "sync":
        result = await sync_handler.sync_transfer(
            session, task, mapping, source_conn, target_conn, source_data=payload)
        return {"status": result.get("status"), "buffer_id": result.get("buffer_id")}

    # async / auto（webhook 场景数据已在手，直接投递异步）
    buffer_id = await async_handler.submit_async(session, task.id, payload)
    return {"status": "async_submitted", "buffer_id": buffer_id}
