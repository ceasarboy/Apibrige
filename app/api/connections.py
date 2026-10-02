"""连接管理 API — CRUD + 测试连接 + 表结构查询"""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_session
from app.models.sync import ApiConnection, FieldMapping
from app.schemas import (
    ConnectionCreate, ConnectionUpdate, ConnectionResponse,
    ConnectionTestResult, MessageResponse,
)
from app.security.crypto import (
    CryptoError, encrypt_auth_config, decrypt_auth_config, SENSITIVE_FIELDS, sensitive_auth_fields, encrypt_db_config,
)
from app.security.desensitize import desensitize_connection, redact_text
from app.adapters.factory import ProtocolAdapterFactory
from app.services.db_client import test_db_connection
from app.schemas import ConnectionTestConfig, ConnectionTestRun
from app.services.connection_tester import test_connection_config

router = APIRouter()

# 敏感值掩码（与 desensitize 输出一致）
_MASK = "***"


def _store_headers(headers, old=None):
    from app.security.headers import store_headers
    try:
        return store_headers(headers, old)
    except (ValueError, CryptoError) as exc:
        raise HTTPException(422, str(exc)) from None


def _preserve_masked_credentials(auth_type: str | None, new_config: dict | None,
                                 old_config: dict | None) -> dict | None:
    """PUT 时敏感字段若回传掩码 *** 则保留库中原密文（避免销毁真实凭证）"""
    if not new_config or not old_config:
        return new_config
    sensitive = sensitive_auth_fields(auth_type, new_config)
    for field in sensitive:
        if new_config.get(field) == _MASK and old_config.get(field) is not None:
            new_config[field] = old_config[field]
    return new_config


def _connection_to_dict(conn: ApiConnection) -> dict:
    """将 ORM 对象转为 dict（auth_config 保持加密状态，用于脱敏）"""
    return {
        "id": conn.id,
        "name": conn.name,
        "connection_type": conn.connection_type,
        "protocol": conn.protocol,
        "base_url": conn.base_url,
        "auth_type": conn.auth_type,
        "auth_config": conn.auth_config,
        "headers": conn.headers,
        "timeout_ms": conn.timeout_ms,
        "retry_count": conn.retry_count,
        "db_type": conn.db_type,
        "db_config": conn.db_config,
        "status": conn.status,
        "created_at": conn.created_at,
        "updated_at": conn.updated_at,
    }


@router.post("", response_model=ConnectionResponse, status_code=201)
async def create_connection(
    payload: ConnectionCreate,
    session: AsyncSession = Depends(get_session),
):
    """创建连接（敏感信息加密存储）"""
    try:
        encrypted_auth = encrypt_auth_config(payload.auth_type, payload.auth_config)
        encrypted_db = encrypt_db_config(payload.db_config)
    except CryptoError as e:
        raise HTTPException(422, detail={"code": "encrypt_key_missing", "message": str(e)})

    conn = ApiConnection(
        name=payload.name,
        connection_type=payload.connection_type,
        protocol=payload.protocol,
        base_url=payload.base_url,
        auth_type=payload.auth_type,
        auth_config=encrypted_auth,
        headers=_store_headers(payload.headers),
        timeout_ms=payload.timeout_ms,
        retry_count=payload.retry_count,
        db_type=payload.db_type,
        db_config=encrypted_db,
    )
    session.add(conn)
    await session.commit()
    await session.refresh(conn)
    # 返回脱敏后的数据
    return desensitize_connection(_connection_to_dict(conn))


@router.get("", response_model=list[ConnectionResponse])
async def list_connections(
    connection_type: str | None = Query(None, description="按类型过滤: source/target"),
    protocol: str | None = Query(None, description="按协议过滤"),
    session: AsyncSession = Depends(get_session),
):
    """查询连接列表（敏感字段脱敏）"""
    stmt = select(ApiConnection).order_by(ApiConnection.created_at.desc())
    if connection_type:
        stmt = stmt.where(ApiConnection.connection_type == connection_type)
    if protocol:
        stmt = stmt.where(ApiConnection.protocol == protocol)

    result = await session.execute(stmt)
    conns = result.scalars().all()
    return [desensitize_connection(_connection_to_dict(c)) for c in conns]


@router.get("/{conn_id}", response_model=ConnectionResponse)
async def get_connection(conn_id: UUID, session: AsyncSession = Depends(get_session)):
    """查询单个连接（敏感字段脱敏）"""
    conn = await session.get(ApiConnection, conn_id)
    if conn is None:
        raise HTTPException(404, "连接不存在")
    return desensitize_connection(_connection_to_dict(conn))


@router.put("/{conn_id}", response_model=ConnectionResponse)
async def update_connection(
    conn_id: UUID,
    payload: ConnectionUpdate,
    session: AsyncSession = Depends(get_session),
):
    """更新连接（如更新 auth_config 会重新加密）"""
    conn = await session.get(ApiConnection, conn_id)
    if conn is None:
        raise HTTPException(404, "连接不存在")

    update_data = payload.model_dump(exclude_unset=True)
    from app.api._guards import reject_null_non_nullable
    update_data = reject_null_non_nullable(ApiConnection, update_data)
    # auth_config 需要重新加密；掩码 *** 的敏感字段保留库中原值（task 1.3）
    if "auth_config" in update_data:
        auth_type = update_data.get("auth_type", conn.auth_type)
        if auth_type != conn.auth_type and any(v == _MASK for v in (update_data['auth_config'] or {}).values()):
            raise HTTPException(422, '切换认证类型时请重新填写凭据')
        new_cfg = _preserve_masked_credentials(auth_type, update_data["auth_config"], conn.auth_config)
        try:
            update_data["auth_config"] = encrypt_auth_config(auth_type, new_cfg)
        except CryptoError as e:
            raise HTTPException(422, detail={"code": "encrypt_key_missing", "message": str(e)})

    # db_config 密码回传掩码 *** 时保留库中原值（与 auth_config 同规，避免改其他字段时毁掉密码）
    if update_data.get("db_config"):
        new_db = dict(update_data["db_config"])
        if new_db.get("password") == _MASK and (conn.db_config or {}).get("password"):
            new_db["password"] = conn.db_config["password"]
        try:
            update_data["db_config"] = encrypt_db_config(new_db)
        except CryptoError as exc:
            raise HTTPException(422, str(exc)) from None

    if 'headers' in update_data:
        update_data['headers'] = _store_headers(update_data['headers'], conn.headers)
    from app.services.incremental_config import guard_connection_change
    await guard_connection_change(session, conn, update_data)
    for field, value in update_data.items():
        setattr(conn, field, value)

    await session.commit()
    await session.refresh(conn)
    return desensitize_connection(_connection_to_dict(conn))


@router.delete("/{conn_id}", response_model=MessageResponse)
async def delete_connection(conn_id: UUID, session: AsyncSession = Depends(get_session)):
    """删除连接（如被字段映射引用则返回 409）"""
    # 检查是否被映射引用
    stmt = select(func.count()).select_from(FieldMapping).where(
        (FieldMapping.source_connection_id == conn_id) |
        (FieldMapping.target_connection_id == conn_id)
    )
    ref_count = (await session.execute(stmt)).scalar()
    if ref_count > 0:
        raise HTTPException(409, f"连接被 {ref_count} 个字段映射引用，无法删除")

    conn = await session.get(ApiConnection, conn_id)
    if conn is None:
        raise HTTPException(404, "连接不存在")

    from app.models.sync import ConnectionField, ConnectionForm
    await session.execute(delete(ConnectionField).where(ConnectionField.connection_id == conn_id))
    await session.execute(delete(ConnectionForm).where(ConnectionForm.connection_id == conn_id))
    await session.delete(conn)
    await session.commit()
    return MessageResponse(message="连接已删除", id=conn_id)


@router.post("/test", response_model=ConnectionTestRun)
async def test_connection_config(payload: ConnectionTestConfig,
                                 session: AsyncSession = Depends(get_session)):
    """测试未入库的连接配置（先测试后保存；凭证不落库）"""
    from app.services.connection_tester import test_connection_config as _run

    return await _run(payload.model_dump(exclude_none=True))


@router.post("/{conn_id}/test", response_model=ConnectionTestResult)
async def test_connection(conn_id: UUID, session: AsyncSession = Depends(get_session)):
    """测试连接可用性"""
    conn = await session.get(ApiConnection, conn_id)
    if conn is None:
        raise HTTPException(404, "连接不存在")

    # 数据库连接：走独立测试路径
    if conn.db_type:
        try:
            result = await test_db_connection(conn.db_type, conn.db_config or {})
            return result
        except Exception as e:
            # 驱动错误保留可诊断性，但过 redact_text：SQLAlchemy/驱动异常串可能带
            # 连接串片段（redact_text 会掩掉 password/token/secret/sign 等键值）
            return {"status": "failed", "latency_ms": 0,
                    "error": redact_text(str(e))}

    # API 连接：走协议适配器（经唯一的 adapter_config 构造点，与运行时同源）
    from app.services.adapter_config import build_adapter_config

    try:
        adapter_config = build_adapter_config(conn)
        adapter = ProtocolAdapterFactory.create(conn.protocol, adapter_config)
        result = await adapter.test_connection()
        return result
    except ValueError as e:
        return {"status": "failed", "latency_ms": 0, "error": redact_text(str(e))}
    except Exception as e:
        return {"status": "failed", "latency_ms": 0, "error": redact_text(str(e))}


# ============================================================
# 数据库表结构查询
# ============================================================

@router.get("/{conn_id}/tables", response_model=list[str])
async def list_tables(conn_id: UUID, session: AsyncSession = Depends(get_session)):
    """获取数据库的所有表名列表（仅数据库类型连接）"""
    conn = await session.get(ApiConnection, conn_id)
    if conn is None:
        raise HTTPException(404, "连接不存在")
    if not conn.db_type:
        raise HTTPException(400, "非数据库类型连接，不支持查询表列表")

    from app.connectors.database import get_database_connector
    connector = get_database_connector(conn.db_type, conn.db_config)
    try:
        tables = await connector.get_table_list()
        return tables
    except Exception as e:
        raise HTTPException(500, f"获取表列表失败: {redact_text(str(e))}")


@router.get("/{conn_id}/schema/{table}")
async def get_table_schema(conn_id: UUID, table: str, session: AsyncSession = Depends(get_session)):
    """获取表的字段定义列表"""
    conn = await session.get(ApiConnection, conn_id)
    if conn is None:
        raise HTTPException(404, "连接不存在")
    if not conn.db_type:
        raise HTTPException(400, "非数据库类型连接，不支持查询表结构")

    from app.connectors.database import get_database_connector
    connector = get_database_connector(conn.db_type, conn.db_config)
    try:
        schema = await connector.get_table_schema(table)
        return {"table": table, "columns": schema}
    except Exception as e:
        raise HTTPException(500, f"获取表结构失败: {redact_text(str(e))}")
@router.get("/{conn_id}/fields")
async def get_connection_fields(
    conn_id: UUID,
    table: str | None = Query(None, description="DB 源的表名"),
    worksheet_id: str | None = Query(None, description="HAP 源的工作表 ID"),
    session: AsyncSession = Depends(get_session),
):
    """统一字段定义：DB 按表返回列定义，HAP 返回控件定义，REST 无自动 Schema"""
    conn = await session.get(ApiConnection, conn_id)
    if conn is None:
        raise HTTPException(404, "连接不存在")

    try:
        from app.services.field_policy import enrich_fields
        result = await _load_connection_fields(conn, table, worksheet_id)
        return await enrich_fields(session, conn, result)
    except HTTPException:
        raise
    except Exception:
        system = {"oracle": "Oracle", "hap": "明道云"}.get(conn.db_type or conn.protocol, "外部系统")
        # 驱动异常可能携带凭证、DSN 和响应体，不直接返回给客户端。
        raise HTTPException(502, detail={
            "code": "connection_fields_unavailable",
            "message": f"无法获取{system}字段定义，请确认服务已启动、网络可达且连接配置正确。",
        }) from None


async def _load_connection_fields(conn: ApiConnection, table: str | None,
                                  worksheet_id: str | None):
    # DB 源
    if conn.db_type:
        from app.connectors.database import get_database_connector
        connector = get_database_connector(conn.db_type, conn.db_config or {})
        if not table:
            tables = await connector.get_table_list()
            return {"kind": "db", "tables": tables, "fields": None}
        schema = await connector.get_table_schema(table)
        return {"kind": "db", "table": table,
                "fields": [{"path": c["name"], "name": c["name"],
                            "type": c["type"], "nullable": c["nullable"],
                            "primary_key": c.get("primary_key", False)}
                           for c in schema]}

    # HAP 源：v2 控件定义（controlId + controlName + type）
    if conn.protocol == "hap":
        from app.adapters.hap import HapV3Adapter
        from app.services.adapter_config import build_adapter_config

        adapter_cfg = build_adapter_config(conn)
        adapter = HapV3Adapter(adapter_cfg)
        ws_id = worksheet_id
        if not ws_id:
            return {"kind": "hap", "fields": None,
                    "hint": "请在参数中提供 worksheet_id"}
        controls = await adapter.get_worksheet_controls(ws_id)
        return {"kind": "hap", "worksheet_id": ws_id,
                "fields": [{"path": c.get("alias") or c.get("controlId"),
                            "display_type": {2:"文本",6:"数值",8:"金额",9:"单选",10:"多选",11:"选项",14:"附件",15:"日期",16:"日期时间",26:"成员",27:"部门",29:"关联记录",33:"自动编号"}.get(c.get("type"), f"未知类型({c.get('type')})"),
                            "field_id": c.get('controlId'), "alias": c.get('alias'),
                            "write_path": None if c.get('readOnly') else c.get('controlId'),
                            "read_only": bool(c.get('readOnly')),
                            "name": c.get("controlName") or c.get("alias") or c.get("controlId"),
                            "type": {2: "string", 6: "number", 15: "datetime", 16: "datetime",
                                     32: "string"}.get(c.get("type"), "string"),
                            "nullable": not c.get("required", False),
                            "primary_key": bool(c.get('primaryKey'))}
                           for c in controls if c.get("type") != 22]}

    # REST 等其他协议：无自动 Schema
    return {"kind": "none", "fields": None,
            "hint": "该协议无自动字段定义，请在映射中手动填写源字段"}
