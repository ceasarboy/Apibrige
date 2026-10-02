"""保存入口与运行入口共享的增量配置保护。"""
from types import SimpleNamespace
from sqlalchemy import select, update
from app.models.sync import SyncTask, ApiConnection, FieldMapping
from app.services.incremental import validate, reject


async def validate_mapping_change(session, data, old=None):
    values = {c.name: getattr(old, c.name) for c in FieldMapping.__table__.columns} if old is not None else {}
    values.update(data)
    mode = values.get('sync_mode', 'full')
    values['sync_mode'] = mode
    values.setdefault('incremental_field', None)
    if isinstance(values['incremental_field'], str):
        values['incremental_field'] = values['incremental_field'].strip() or None
        data['incremental_field'] = values['incremental_field']
    if mode == 'full':
        data['incremental_field'] = None
        values['incremental_field'] = None
    if mode not in ('full', 'incremental'):
        reject('incremental_mode_invalid', '同步模式必须是 full 或 incremental')
    tasks = []
    if old is not None:
        changed = {key for key, value in data.items() if value != getattr(old, key)}
        if not changed - {'name'}:
            return
        # 与执行占用同一任务行锁，关闭配置检查与轮次启动之间的竞态。
        await session.execute(update(SyncTask).where(SyncTask.mapping_id == old.id).values(checkpoint_version=SyncTask.checkpoint_version))
        tasks = (await session.scalars(select(SyncTask).where(SyncTask.mapping_id == old.id).execution_options(populate_existing=True))).all()
        from app.services.hap_execution import guard_pending
        await guard_pending(session, [t.id for t in tasks])
        if changed - {'name'} and any(t.active_round_id for t in tasks):
            reject('incremental_round_busy', '存在未完成轮次，暂不能修改映射配置', 409)
        identity = {'source_connection_id', 'source_table', 'source_endpoint', 'incremental_field', 'sync_mode'}
        if changed & identity and any(t.last_sync_value is not None for t in tasks):
            reject('incremental_checkpoint_conflict', '相关任务已有水位，请为新的增量来源创建新映射和任务', 409)
        if 'mapping_config' in changed and any(t.last_sync_value is not None for t in tasks):
            from app.services.sync_config import side_config
            source_keys = {'worksheet_id', 'view_id', 'field_keys', 'read', 'incremental'}
            before = side_config(old.mapping_config or {}, 'source')
            after = side_config(values.get('mapping_config') or {}, 'source')
            if any(before.get(key) != after.get(key) for key in source_keys):
                reject('incremental_checkpoint_conflict', '已有水位，不能改变源读取范围或增量定义；请创建新映射和任务', 409)
    from app.services.field_policy import effective_config
    from app.services.hap_target import validate_policy
    candidate = SimpleNamespace(**values)
    await effective_config(session, candidate)
    if (values.get('mapping_config') or {}).get('hap_write_mode', 'append') != 'append' or (values.get('mapping_config') or {}).get('hap_delete_sync'):
        target = await session.get(ApiConnection, values['target_connection_id'])
        validate_policy(candidate, target)
        for task in tasks:
            validate_policy(candidate, target, task)
    if mode == 'incremental':
        source = await session.get(ApiConnection, values['source_connection_id'])
        target = await session.get(ApiConnection, values['target_connection_id'])
        mapping = SimpleNamespace(**values)
        validate(mapping, source)
        for task in tasks:
            validate(mapping, source, task, target)


async def validate_task_change(session, values, old=None):
    if old is not None:
        from app.services.incremental_rounds import lock_task
        old = await lock_task(session, old.id)
        from app.services.hap_execution import guard_pending
        await guard_pending(session, [old.id])
        if old.active_round_id and any(value != getattr(old, key) for key, value in values.items()):
            reject('incremental_round_busy', '存在未完成轮次，暂不能修改任务配置', 409)
        if old.last_sync_value is not None and values.get('mapping_id', old.mapping_id) != old.mapping_id:
            reject('incremental_checkpoint_conflict', '已有水位的任务不能切换映射', 409)
    mapping_id = values.get('mapping_id', old.mapping_id if old else None)
    mapping = await session.get(FieldMapping, mapping_id)
    if mapping is not None:
        from app.services.hap_target import validate_policy
        data = {c.name: getattr(old, c.name) for c in SyncTask.__table__.columns} if old else {'sync_mode': 'async', 'fallback_to_async': False, 'webhook_config': None}
        data.update(values)
        cfg = mapping.mapping_config or {}
        if cfg.get('hap_write_mode', 'append') != 'append' or cfg.get('hap_delete_sync'):
            target = await session.get(ApiConnection, mapping.target_connection_id)
            validate_policy(mapping, target, SimpleNamespace(**data))
    if mapping is not None and mapping.sync_mode == 'incremental':
        data = {c.name: getattr(old, c.name) for c in SyncTask.__table__.columns} if old else {
            'sync_mode': 'async', 'fallback_to_async': False, 'webhook_config': None, 'conflict_strategy': None}
        data.update(values)
        source = await session.get(ApiConnection, mapping.source_connection_id)
        target = await session.get(ApiConnection, mapping.target_connection_id)
        validate(mapping, source, SimpleNamespace(**data), target)


async def guard_connection_change(session, connection, changes):
    from app.security.crypto import sensitive_auth_fields
    from sqlalchemy import or_
    changed = {key for key, value in changes.items() if value != getattr(connection, key)}
    if not changed - {'name', 'auth_config'}:
        old_auth, new_auth = connection.auth_config or {}, changes.get('auth_config', connection.auth_config) or {}
        sensitive = sensitive_auth_fields(connection.auth_type, new_auth)
        if all(new_auth.get(k) == old_auth.get(k) for k in set(old_auth) | set(new_auth) if k not in sensitive):
            return
    mappings = (await session.scalars(select(FieldMapping).where(or_(
        FieldMapping.source_connection_id == connection.id,
        FieldMapping.target_connection_id == connection.id)))).all()
    if not mappings:
        return
    await session.execute(update(SyncTask).where(SyncTask.mapping_id.in_([m.id for m in mappings])).values(checkpoint_version=SyncTask.checkpoint_version))
    tasks = (await session.scalars(select(SyncTask).where(SyncTask.mapping_id.in_([m.id for m in mappings])))).all()
    changed = {key for key, value in changes.items() if value != getattr(connection, key)}
    if changed - {'name', 'auth_config'}:
        from app.services.hap_execution import guard_pending
        await guard_pending(session, [t.id for t in tasks])
    if not any(t.active_round_id for t in tasks):
        if any(t.last_sync_value is not None for t in tasks) and changed & {'protocol', 'base_url', 'db_type', 'db_config'}:
            old_db, new_db = connection.db_config or {}, changes.get('db_config') or {}
            if changed - {'db_config', 'name', 'auth_config'} or any(new_db.get(k) != old_db.get(k) for k in set(old_db) | set(new_db) if k != 'password'):
                reject('incremental_checkpoint_conflict', '已有水位的连接不能更换数据来源，请创建新连接', 409)
        return
    allowed = {'name', 'auth_config', 'db_config'}
    if changed - allowed:
        reject('incremental_round_busy', '存在活动轮次，只允许轮换同一连接的凭证', 409)
    if 'db_config' in changed:
        old_db, new_db = connection.db_config or {}, changes['db_config'] or {}
        if any(new_db.get(k) != old_db.get(k) for k in set(old_db) | set(new_db) if k != 'password'):
            reject('incremental_round_busy', '活动轮次期间不能改变数据库身份', 409)
    if 'auth_config' in changed:
        old_auth, new_auth = connection.auth_config or {}, changes['auth_config'] or {}
        sensitive = sensitive_auth_fields(connection.auth_type, new_auth)
        if any(new_auth.get(k) != old_auth.get(k) for k in set(old_auth) | set(new_auth) if k not in sensitive):
            reject('incremental_round_busy', '活动轮次期间仅允许更新认证凭据值', 409)
