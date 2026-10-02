"""表单注册表服务 — 源端拉取合并 + 桥引用统计

注册表（ConnectionForm）回答"哪些表单可用/被哪些桥用了"：
- 拉取：DB 连接列全部表（get_table_list），HAP 走 app/get 列全部工作表
  （adapters/hap.list_worksheets）；REST 等无结构协议不支持自动拉取
- 合并：新 form_key 插入（source=auto，display_name 默认=源端名）；
  已存在仅刷新 original_name（不覆盖用户改过的 display_name/remark）；
  源端已消失的 auto 项保留（防误删断桥）
- 引用统计：对 FieldMapping 全量计算（映射量级小，无需缓存表），
  匹配大小写互认（Oracle 大写表名 × 映射小写的既有惯例）
"""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sync import ApiConnection, ConnectionForm, FieldMapping


async def fetch_source_forms(conn: ApiConnection) -> list[dict]:
    """从源端拉取表单清单 → [{"form_key", "original_name", "remark"}]

    不支持的协议抛 FormSyncUnsupportedError（API 层转 400）。
    """
    if conn.db_type:
        from app.connectors.database import get_database_connector
        from app.security.crypto import decrypt_db_config
        connector = get_database_connector(conn.db_type, decrypt_db_config(conn.db_config) or {})
        try:
            tables = await connector.get_table_list()
        finally:
            await connector.disconnect()
        return [{"form_key": t, "original_name": t, "remark": ""} for t in tables]

    if conn.protocol == "hap":
        from app.adapters.hap import HapV3Adapter
        from app.services.adapter_config import build_adapter_config
        adapter = HapV3Adapter(build_adapter_config(conn))
        return await adapter.list_worksheets()

    raise FormSyncUnsupportedError(
        f"协议 {conn.protocol} 不支持自动拉取表单，请手动登记")


class FormSyncUnsupportedError(Exception):
    """该连接协议不支持自动拉取表单"""


def _ci_equal(a: str, b: str) -> bool:
    """大小写不敏感相等（Oracle 大写表名 × 映射小写互认）"""
    return a.strip().lower() == b.strip().lower()


async def merge_forms(session: AsyncSession, conn: ApiConnection) -> dict:
    """从源端拉取并合并入注册表 → {"added", "refreshed", "total"}

    合并规则见模块 docstring；调用方负责 commit。
    """
    incoming = await fetch_source_forms(conn)
    existing = (await session.execute(
        select(ConnectionForm).where(ConnectionForm.connection_id == conn.id)
    )).scalars().all()
    by_key: dict[str, ConnectionForm] = {}
    for f in existing:
        # 同连接内 form_key 唯一约束已保证无重复
        by_key[f.form_key.strip().lower()] = f

    added = refreshed = 0
    for item in incoming:
        key = item["form_key"]
        kl = key.strip().lower()
        if kl not in by_key:
            session.add(ConnectionForm(
                connection_id=conn.id,
                form_key=key,
                original_name=item.get("original_name") or key,
                display_name=item.get("original_name") or key,
                remark=item.get("remark") or "",
                source="auto",
            ))
            by_key[kl] = None  # 占位：同批重复键不再插入
            added += 1
        elif by_key[kl] is not None:
            by_key[kl].original_name = item.get("original_name") or by_key[kl].original_name
            refreshed += 1
    total = len(existing) + added
    return {"added": added, "refreshed": refreshed, "total": total}


def _mapping_form_refs(mapping: FieldMapping) -> list[tuple[str, str]]:
    """取一条映射两端的技术表单标识 [(side, key), ...]

    与运行时实际取用口径一致（AGENTS.md #14）：
    - 源侧：DB 用 source_table，API/HAP 用 source_endpoint（HAP 源的 ws id）
    - 目标侧：HAP 用 mapping_config.protocol.worksheet_id（回退 target_endpoint），
      DB 用 target_table
    """
    protocol_cfg = (mapping.mapping_config or {}).get("protocol") or {}
    if not isinstance(protocol_cfg, dict):
        protocol_cfg = {}
    refs: list[tuple[str, str]] = []
    from app.services.sync_config import side_config
    cfg = mapping.mapping_config or {}
    src_cfg = side_config(cfg, 'source') if cfg.get('version') == 2 else cfg
    src = src_cfg.get('worksheet_id') or mapping.source_table or mapping.source_endpoint
    if src:
        refs.append(("source", str(src)))
    tgt = (side_config(mapping.mapping_config or {}, 'target').get('worksheet_id')
           or mapping.target_table or mapping.target_endpoint)
    if tgt:
        refs.append(("target", str(tgt)))
    return refs


async def attach_bridge_refs(session: AsyncSession,
                             forms: list[ConnectionForm]) -> None:
    """为表单列表就地附加 bridge_count / bridges 引用统计

    bridges 项: {mapping_id, mapping_name, side, peer_connection_id,
                 peer_connection_name, peer_form}
    """
    mappings = (await session.execute(select(FieldMapping))).scalars().all()
    conn_ids: set[UUID] = set()
    for m in mappings:
        conn_ids.update({m.source_connection_id, m.target_connection_id})
    conn_names: dict[UUID, str] = {}
    if conn_ids:
        rows = (await session.execute(
            select(ApiConnection).where(ApiConnection.id.in_(conn_ids))
        )).scalars().all()
        conn_names = {c.id: c.name for c in rows}

    # 每条映射两端各算一次引用；peer 取对端标识
    for form in forms:
        form.bridge_count = 0
        form.bridges = []
    by_conn: dict[UUID, list[ConnectionForm]] = {}
    for form in forms:
        by_conn.setdefault(form.connection_id, []).append(form)

    for m in mappings:
        refs = _mapping_form_refs(m)
        for side, key in refs:
            conn_id = m.source_connection_id if side == "source" else m.target_connection_id
            peer_side = "target" if side == "source" else "source"
            peer_key = ""
            for s2, k2 in refs:
                if s2 == peer_side:
                    peer_key = k2
                    break
            peer_conn_id = m.target_connection_id if side == "source" else m.source_connection_id
            for form in by_conn.get(conn_id, []):
                if not _ci_equal(form.form_key, key):
                    continue
                form.bridge_count += 1
                form.bridges.append({
                    "mapping_id": str(m.id),
                    "mapping_name": m.name,
                    "side": side,
                    "peer_connection_id": str(peer_conn_id),
                    "peer_connection_name": conn_names.get(peer_conn_id, ""),
                    "peer_form": peer_key,
                })
