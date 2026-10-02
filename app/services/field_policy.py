"""可选字段字典：只限制映射输出；在执行/提交/建轮时解析并固定规则。"""
from copy import deepcopy
from types import SimpleNamespace
from sqlalchemy import select
from app.models.sync import ConnectionField, ApiConnection
from app.services.sync_config import side_config
from app.adapters.errors import AdapterError


def execution_key(task_id, config):
    """启用策略后，输出规则变化不能被旧整批幂等标记短路。"""
    import hashlib
    import json
    digest = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return f'{task_id}:fields:{digest}'


def scope_for(conn, mapping, side):
    if conn.db_type:
        return getattr(mapping, f'{side}_table', None) or ''
    if conn.protocol == 'hap':
        return side_config(mapping.mapping_config or {}, side).get('worksheet_id') or getattr(mapping, f'{side}_endpoint', None) or ''
    return getattr(mapping, f'{side}_endpoint', None) or ''


def identity(conn, value):
    value = str(value or '').strip()
    return value.lower() if conn.db_type in ('oracle', 'sqlite') else value


async def definitions(session, conn, scope):
    if not getattr(conn, 'id', None):
        return {}
    rows = (await session.scalars(select(ConnectionField).where(ConnectionField.connection_id == conn.id))).all()
    result = {}
    for row in rows:
        if identity(conn, row.scope) != identity(conn, scope):
            continue
        for key in (row.field_id, row.path):
            if not key:
                continue
            k = identity(conn, key)
            if k in result and result[k].id != row.id:
                raise AdapterError('field_policy_ambiguous', '字段字典标识存在歧义，请刷新字段', 'field_resolve')
            result[k] = row
    return result


def lookup(defs, conn, *keys):
    for key in keys:
        if key is not None and identity(conn, key) in defs:
            return defs[identity(conn, key)]
    return None


async def effective_config(session, mapping, source=None, target=None):
    cfg = mapping.mapping_config or {}
    if cfg.get('field_policy', 'off') != 'inherit':
        return deepcopy(cfg)
    source = source or await session.get(ApiConnection, mapping.source_connection_id)
    target = target or await session.get(ApiConnection, mapping.target_connection_id)
    if source is None or target is None:
        raise AdapterError('field_policy_connection_missing', '字段规则引用的连接不存在', 'field_resolve')
    src = await definitions(session, source, scope_for(source, mapping, 'source'))
    tgt = await definitions(session, target, scope_for(target, mapping, 'target'))
    # 水位和排序键即便不在输出映射里也不能禁用。
    required_source = [getattr(mapping, 'incremental_field', None)]
    if getattr(mapping, 'sync_mode', None) == 'incremental':
        required_source.append('rowid' if source.protocol == 'hap' else side_config(cfg, 'source').get('incremental', {}).get('key_field'))
    for key in required_source:
        d = lookup(src, source, key)
        if d is not None and not d.readable:
            raise AdapterError('field_policy_key_conflict', '字段字典不能禁用增量水位或源主键', 'field_resolve')
    required = {str(cfg['key_field'])} if cfg.get('key_field') else set()
    required.update(k for k, rule in (cfg.get('validations') or {}).items() if rule.get('required'))
    required.update(c['name'] for c in cfg.get('target_contract') or [] if c.get('required'))
    kept = []
    for field in cfg.get('fields', []):
        ref = field.get('source_ref') or {}
        sd = lookup(src, source, ref.get('field_id'), field['source'], ref.get('alias'))
        td = lookup(tgt, target, field['target'])
        if (sd is not None and not sd.readable) or (td is not None and not td.writable):
            if any(identity(target, field['target']) == identity(target, k) for k in required):
                raise AdapterError('field_policy_required_conflict', '字段字典不能剔除业务键或必填目标字段', 'field_resolve')
            continue
        kept.append(field)
    if not kept:
        raise AdapterError('field_policy_empty', '字段字典过滤后无可同步字段', 'field_resolve')
    result = deepcopy(cfg)
    result['fields'] = deepcopy(kept)
    result['field_policy'] = 'off'  # 已解析，消费及重试不再读取可变字典
    return result


def with_config(mapping, config):
    names = ('id', 'name', 'source_connection_id', 'target_connection_id', 'source_table', 'target_table',
             'source_endpoint', 'target_endpoint', 'source_headers', 'target_headers', 'sync_mode',
             'incremental_field', 'batch_size')
    values = {name: getattr(mapping, name, None) for name in names}
    return SimpleNamespace(**values, mapping_config=config)


async def freeze(session, mapping, source=None, target=None):
    if (mapping.mapping_config or {}).get('field_policy', 'off') != 'inherit':
        return mapping
    return with_config(mapping, await effective_config(session, mapping, source, target))


async def enrich_fields(session, conn, result):
    if not isinstance(result.get('fields'), list):
        return result
    scope = result.get('table') or result.get('worksheet_id') or ''
    defs = await definitions(session, conn, scope)
    for field in result['fields']:
        d = lookup(defs, conn, field.get('field_id'), field['path'], field.get('alias'))
        field.update(saved=d is not None, readable=d.readable if d else True,
                     writable=(d.writable if d else True) and not field.get('read_only', False))
        if d and d.name:
            field['name'] = d.name
    return result
