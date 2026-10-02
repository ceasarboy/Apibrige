"""适配器配置构造 — 连接配置 → adapter_config 的唯一构造点

运行时（sync_handler/async_handler/router）与将来的探针共用本函数，
落实"探针与运行时同源"原则：同一连接无论从哪条路径构造，
得到的 adapter_config 必须深度相等（凭据均已解密）。
"""
from app.models.sync import ApiConnection, FieldMapping
from app.security.crypto import decrypt_auth_config
from app.security.headers import decrypt_headers, merge_headers
from app.services.sync_config import side_config


def build_adapter_config(
    conn: ApiConnection,
    mapping: FieldMapping | None = None,
    side: str | None = None,
) -> dict:
    """构造协议适配器配置（凭据解密 + 映射级 Header 覆盖 + 协议参数合并）

    Args:
        conn: 连接 ORM 对象（auth_config 为加密态）
        mapping: 字段映射；提供时可做映射级 Header 覆盖与协议参数合并
        side: "source" 应用 mapping.source_headers，"target" 应用
              mapping.target_headers；None 则不应用映射级覆盖

    Returns:
        适配器配置 dict（可直接传给 ProtocolAdapterFactory.create）
    """
    config = {
        "base_url": conn.base_url or "",
        "auth_type": conn.auth_type,
        "auth_config": decrypt_auth_config(conn.auth_type, conn.auth_config),
        "headers": decrypt_headers(conn.headers),
        "timeout_ms": conn.timeout_ms,
        "retry_count": conn.retry_count,
    }
    from app.services.api_client import resolve_variables
    config['base_url'] = resolve_variables(config['base_url'])
    config['headers'] = resolve_variables(config['headers'])

    if mapping is not None:
        # 映射级 Header 覆盖（按源/目标侧取对应字段）
        if side:
            attr = "source_headers" if side == "source" else "target_headers"
            override = getattr(mapping, attr, None)
            if override:
                config["headers"] = merge_headers(config['headers'], decrypt_headers(override))

        # 协议特有参数：约定落在 mapping_config["protocol"]（task 1.10）。
        # REST 等不适用的协议会忽略多余键，行为不变。
        mapping_cfg = mapping.mapping_config or {}
        config['config_version'] = mapping_cfg.get('version', 1)
        config['connection_key'] = str(getattr(conn, 'id', '') or conn.base_url)
        if side == 'source':
            config['source_refs'] = [f['source_ref'] for f in mapping_cfg.get('fields', []) if f.get('source_ref')]
        protocol_cfg = side_config(mapping_cfg, side or 'source')
        if isinstance(protocol_cfg, dict):
            for key, value in protocol_cfg.items():
                if key not in ('source', 'target', 'base_url', 'auth_type', 'auth_config', 'headers', 'config_version'):
                    config.setdefault(key, value)
        if (side == 'target' and getattr(mapping, 'sync_mode', None) == 'incremental'
                and not getattr(conn, 'db_type', None) and not protocol_cfg.get('idempotency_header')):
            config['retry_count'] = 0

    config['headers'] = resolve_variables(config['headers'])
    # 仅对源读取请求解析运行时水位变量；目标请求继续使用既有变量规则。
    if side == 'source' and getattr(mapping, 'sync_mode', None) == 'incremental':
        config['_runtime_vars'] = {'last_watermark': getattr(getattr(mapping, '_runtime', None), 'last_watermark', None)}
    return config
