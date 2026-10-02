"""显式启用的 HAP V2 全量策略；旧追加模式不改变。"""
from app.adapters.errors import AdapterError
from app.services.sync_config import side_config


def enabled(mapping):
    """映射是否显式启用 HAP 目标写策略（不含目标连接判断）"""
    cfg = mapping.mapping_config or {}
    return (cfg.get('hap_write_mode', 'append') in ('deduplicate', 'upsert')
            or cfg.get('hap_delete_sync', False) is True)


def validate_policy(mapping, target, task=None, injected=False):
    cfg = mapping.mapping_config or {}
    # 删除对账（任一键）必须读取**完整源集合**：过滤/视图源只返回子集，
    # 删差会把"子集之外"的目标行误删——对 DB 目标同样成立（DB 删差在
    # sync_handler 按源键差集物理删除；下方两条是 HAP 目标的同款守卫）。
    # 此检查必须放在 enabled() 早退之前：DB 目标 + 仅 delete_sync 不满足
    # enabled()，早退会让全部范围守卫失效（审查 P0）。
    if (cfg.get('hap_delete_sync') or cfg.get('delete_sync')) and (
            side_config(cfg, 'source').get('filters') or side_config(cfg, 'source').get('view_id')):
        raise AdapterError('delete_sync_filtered_source',
                           '删除对账不能使用视图或过滤后的源集合（会误删源集合之外的目标行）', 'validate')
    # 遗留键 delete_sync 在 **HAP 目标**下同样受本策略约束（数据库目标走 DB 删差路径）
    legacy_hap_delete = (cfg.get('delete_sync') is True
                         and target is not None
                         and getattr(target, 'protocol', None) == 'hap'
                         and not getattr(target, 'db_type', None))
    if not enabled(mapping) and not legacy_hap_delete:
        return
    if cfg.get('hap_write_mode') == 'upsert':
        from app.services.incremental import reject
        if target is None or target.protocol != 'hap' or target.db_type:
            reject('hap_policy_target', '新增更新需要 HAP 目标')
        if not cfg.get('key_field') or not any(f.get('target') == cfg['key_field'] for f in cfg.get('fields', [])):
            reject('hap_policy_key', '新增更新必须映射目标业务键字段')
        if injected or (task is not None and (task.sync_mode not in ('sync', 'async') or task.fallback_to_async or task.webhook_config or getattr(task, 'conflict_strategy', None) not in (None, '', 'overwrite'))):
            reject('hap_policy_mode', '新增更新支持 sync/async，不支持 auto、Webhook、失败转异步或冲突跳过')
        if cfg.get('delete_sync') or cfg.get('hap_delete_sync'):
            if mapping.sync_mode != 'full' or (task is not None and task.sync_mode != 'sync'):
                reject('hap_delete_mode', '删除对账仅支持全量 sync')
            if side_config(cfg, 'source').get('filters') or side_config(cfg, 'source').get('view_id'):
                reject('hap_delete_scope', '删除对账必须读取完整源集合')
        return
    if target is None or target.protocol != 'hap' or target.db_type or mapping.sync_mode != 'full':
        raise AdapterError('hap_policy_mode', 'HAP 目标策略仅支持 HAP 目标全量同步', 'validate')
    if not cfg.get('key_field'):
        raise AdapterError('hap_policy_key', 'HAP 目标策略必须配置目标业务键', 'validate')
    if injected or (task is not None and (task.sync_mode != 'sync' or task.fallback_to_async or task.webhook_config)):
        raise AdapterError('hap_policy_mode', 'HAP 目标策略仅支持 sync，不支持 Webhook、auto 或失败转异步', 'validate')
    if (cfg.get('hap_delete_sync') or cfg.get('delete_sync')) and (
            side_config(cfg, 'source').get('filters') or side_config(cfg, 'source').get('view_id')):
        raise AdapterError('hap_delete_filtered_source', '删除对账不能使用视图或过滤后的 HAP 源集合', 'validate')


def value(record, key):
    matches = [v for k, v in record.items() if k.lower() == key.lower()]
    if len(matches) > 1:
        raise AdapterError('hap_key_ambiguous', '业务键大小写存在歧义', 'field_resolve')
    return str(matches[0]).strip() if matches and matches[0] is not None else ''


async def existing_rows(adapter, worksheet, key):
    from app.config import settings
    if isinstance(getattr(adapter, 'config', None), dict):
        adapter.config.update(_stable_scan=True, _scan_max_rows=settings.incremental_max_rows,
                              _scan_max_bytes=settings.incremental_max_bytes)
    controls = await adapter.get_worksheet_controls(worksheet)
    matches = [c for c in controls if key in (c.get('controlId'), c.get('alias'))]
    if len(matches) != 1:
        raise AdapterError('hap_key_ambiguous', '目标业务键不存在或不唯一，请刷新字段', 'field_resolve')
    control = matches[0]
    keys = list(dict.fromkeys(k for k in (control.get('alias'), control.get('controlId')) if k))
    rows = await adapter.get_worksheet_rows(worksheet)
    result = {}
    for row in rows:
        values = {value(row, k) for k in keys if any(rk.lower() == k.lower() for rk in row)}
        if len(values) != 1 or '' in values or not row.get('rowid'):
            raise AdapterError('hap_key_invalid', '目标记录缺少业务键/rowid 或别名值冲突', 'field_resolve')
        v = values.pop()
        if v in result:
            raise AdapterError('hap_key_duplicate', '目标存在重复业务键，不能自动对账', 'field_resolve')
        result[v] = str(row['rowid'])
    return result


async def push(adapter, config, mapping, records):
    cfg = mapping.mapping_config
    key = cfg['key_field']
    worksheet = config.get('worksheet_id') or mapping.target_endpoint
    if not worksheet:
        raise AdapterError('hap_worksheet_required', '请选择目标工作表', 'validate')
    records = records if isinstance(records, list) else [records]
    seen = set()
    for record in records:
        if not isinstance(record, dict) or not (v := value(record, key)) or v in seen:
            raise AdapterError('hap_source_key_invalid', '源记录业务键为空或重复，停止写入', 'validate')
        seen.add(v)
    existing = await existing_rows(adapter, worksheet, key)
    # 删除对账开启时（任一键）强制按业务键去重写入：append（默认追加）每轮把全量
    # 源记录重复插入 → 第二轮起目标键重复（existing_rows 抛 hap_key_duplicate），
    # 且重试持续放大重复行（P0，审查实测）。这也与既有 delete_sync 语义一致——
    # 原实现本就按键去重推送。
    dedup = (cfg.get('hap_write_mode') == 'deduplicate'
             or cfg.get('hap_delete_sync') or cfg.get('delete_sync'))
    selected = [r for r in records if not dedup or value(r, key) not in existing]
    ids = []
    for record in selected:
        try:
            row_id = await adapter.add_worksheet_row(worksheet, record)
            if not row_id:
                raise ValueError('missing row id')
            ids.append(row_id)
        except Exception:
            raise AdapterError('hap_write_unconfirmed', '目标写入失败或结果不确定；请核对目标，未执行删除对账', 'push', details={'confirmed_count': len(ids)}) from None
    return {'status_code': 200, 'body': {'row_ids': ids, 'skipped': len(records) - len(selected)}}


async def reconcile(adapter, config, mapping, source_records):
    if not source_records:
        return {'deleted': 0, 'skipped': '源为空，不清空目标'}
    key = mapping.mapping_config.get('key_field')
    keys = [value(r, key) for r in source_records]
    if not key or any(not k for k in keys) or len(set(keys)) != len(keys):
        raise AdapterError('hap_source_key_invalid', '源键集合无效，未执行删除', 'delete')
    worksheet = config.get('worksheet_id') or mapping.target_endpoint
    existing = await existing_rows(adapter, worksheet, key)
    stale = sorted(set(existing) - set(keys))
    deleted = 0
    for key in stale:
        try:
            await adapter.delete_worksheet_row(worksheet, existing[key])
            deleted += 1
        except Exception:
            raise AdapterError('hap_delete_unconfirmed', '删除结果不确定，请核对目标记录', 'delete', details={'confirmed_count': deleted}) from None
    return {'deleted': deleted}
