"""跨协议失败日志契约及源数据定位；只读本地缓冲，不触发外部请求。"""
from pathlib import Path
import re
import traceback
from fastapi import HTTPException
from app.adapters.errors import AdapterError
from app.models.sync import DataBuffer, SyncRound, SyncTask, FieldMapping
from app.security.desensitize import desensitize_record, redact_text
from app.services.incremental import unseal

MAX_ISSUES = 50


def safe_message(error):
    if isinstance(error, AdapterError):
        message = str(error)
    elif isinstance(error, HTTPException):
        message = error.detail.get('message', '请求被拒绝') if isinstance(error.detail, dict) else str(error.detail)
    else:
        # 驱动异常可能包含 SQL 参数、DSN、请求体；保留异常类型及代码位置即可排查。
        return f'{type(error).__name__}: 执行失败，请结合失败阶段、源数据和代码位置排查'
    message = re.sub(r'[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+', '[地址已隐藏]', message)
    message = re.sub(r'enc:[^\s,;]+', '***', message)
    return redact_text(message)[:1000]


def normalize_detail(raw=None, stage='execute'):
    raw = raw if isinstance(raw, dict) else {}
    out = {'schema_version':'1.0', 'code':raw.get('code') or 'execution_failed',
           'stage':raw.get('stage') or stage}
    for key in ('exception_type', 'target_field', 'target_field_name', 'target_field_alias', 'source_field'):
        if isinstance(raw.get(key), str):
            out[key] = raw[key][:200]
    if 'target_field' not in out and isinstance(raw.get('field'), str):
        out['target_field'] = raw['field'][:200]
    index = raw.get('row_index', raw.get('index'))
    if type(index) is int and index >= 0:
        out['row_index'] = index
    if type(raw.get('status_code')) is int:
        out['status_code'] = raw['status_code']
    if isinstance(raw.get('message'), str):
        message = re.sub(r'[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+', '[地址已隐藏]', raw['message'])
        out['message'] = redact_text(re.sub(r'enc:[^\s,;]+', '***', message))[:1000]
    issues = raw.get('issues', [])
    if isinstance(issues, list):
        out['issues'] = []
        for item in issues[:MAX_ISSUES]:
            if not isinstance(item, dict):
                continue
            issue = {k: item[k][:200] for k in ('target_field','target_field_name','target_field_alias','source_field','rule')
                     if isinstance(item.get(k), str)}
            i = item.get('row_index', item.get('index'))
            if type(i) is int and i >= 0:
                issue['row_index'] = i
            out['issues'].append(issue)
        out['issue_count'] = max(len(issues), raw.get('issue_count') if type(raw.get('issue_count')) is int else 0)
        out['issues_truncated'] = out['issue_count'] > len(out['issues'])
    frames = raw.get('frames')
    if isinstance(frames, list):
        out['frames'] = [{k: f[k] for k in ('file','line','function') if isinstance(f.get(k), (str,int))}
                         for f in frames[-8:] if isinstance(f, dict)]
    return desensitize_record(out)


def error_detail(error, stage='execute'):
    raw = {}
    if isinstance(error, AdapterError):
        raw = {**error.details, 'code':error.code, 'stage':error.stage}
    elif isinstance(error, HTTPException):
        raw = dict(error.detail) if isinstance(error.detail, dict) else {}
        raw.setdefault('code', 'request_rejected')
        raw['status_code'] = error.status_code
    raw.update(exception_type=type(error).__name__, message=safe_message(error))
    raw.setdefault('stage', stage)
    raw['frames'] = [{'file':Path(f.filename).name,'line':f.lineno,'function':f.name}
                     for f in traceback.extract_tb(error.__traceback__)[-8:]]
    return normalize_detail(raw, stage)


def validation_detail(result):
    issues = [{'row_index':e['index'], 'target_field':e['field'], 'rule':e['rule']}
              for e in result.get('errors', [])]
    # 不记录 validator.message，可能包含敏感字段的原始值。
    return normalize_detail({'code':'validation_failed', 'stage':'validate', 'issues':issues})


def source_records(data):
    # 与 transform_payload 相同的包装优先级；不递归猜测任意 JSON 路径。
    from app.services.transformer import _WRAPPER_KEYS
    if isinstance(data, list):
        return data, ''
    if isinstance(data, dict):
        for key in _WRAPPER_KEYS:
            if isinstance(data.get(key), list):
                return data[key], '/' + key
        return [data], ''
    return [], ''


def safe_source(data):
    data = desensitize_record(data)
    def scrub(value):
        if isinstance(value, dict):
            return {k:scrub(v) for k,v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, str):
            if value.startswith('enc:'):
                return '***'
            return redact_text(value)
        return value
    return scrub(data)


async def failure_context(session, log, page=1, page_size=20):
    context = {'records':[], 'exact_row':False, 'scope':'unavailable', 'total_records':0,
               'matched_records':0, 'page':page, 'page_size':page_size, 'source_fields':[]}
    detail = normalize_detail((log.response_data or {}).get('error_detail') or log.response_data, log.action)
    task = await session.get(SyncTask, log.task_id)
    mapping = await session.get(FieldMapping, task.mapping_id) if task else None
    context['task_name'] = mapping.name if mapping else None
    batch = await session.get(DataBuffer, log.buffer_id) if log.buffer_id else None
    if batch is None or batch.task_id != log.task_id:
        return {**context, 'notice':'本次错误没有可读取的源批次（尚未持久化或已清理），无法定位源行'}
    context['batch_number'] = batch.batch_no + 1 if batch.batch_no is not None else None
    context['buffer_id'] = str(batch.id)
    saved = {}
    snapshot_notice = None
    try:
        if batch.round_id:
            run = await session.get(SyncRound, batch.round_id)
            if run and run.task_id == log.task_id:
                saved = unseal(run.config_snapshot)
        elif batch.config_snapshot:
            saved = unseal(batch.config_snapshot)
    except Exception:
        snapshot_notice = '执行时映射快照不可读取，无法确认源/目标字段对应关系'
    try:
        data = batch.source_data
        if isinstance(data, dict) and 'encrypted' in data:
            wanted = (log.response_data or {}).get('source_revision', 0)
            if wanted != data.get('revision', 0):
                old = next((item for item in data.get('history', []) if item.get('revision') == wanted), None)
                if old is None:
                    return {**context, 'notice':'该日志对应的源数据版本不可读取'}
                data = old
            context['source_revision'] = wanted
            data = unseal(data['encrypted'])
        if isinstance(data, dict) and data.get('hap_preparing'):
            return {**context, 'notice':'源数据尚未封存，无法定位源行'}
        collection_is_list = isinstance(data, list) or (isinstance(data, dict) and any(
            isinstance(data.get(k), list) for k in ('data','items','results','list','records')))
        data, path = source_records(data)
        context['record_path'] = path or '/'
    except Exception:
        return {**context, 'notice':'原始批次不可读取，请检查加密密钥和数据是否仍保留'}
    cfg = saved.get('mapping_config') or {}
    fields = cfg.get('fields') or (log.response_data or {}).get('mapping_fields') or []
    has_mapping = bool(saved) or 'mapping_fields' in (log.response_data or {})
    issues = detail.get('issues') or []
    locators = [detail, *issues]
    for item in locators:
        source = item.get('source_field')
        if source and source not in context['source_fields']:
            context['source_fields'].append(source)
        target = item.get('target_field')
        for f in fields:
            if target and f.get('target') in (target, item.get('target_field_alias')):
                if f.get('source') not in context['source_fields']:
                    context['source_fields'].append(f['source'])
    indices = sorted({item['row_index'] for item in locators
                      if type(item.get('row_index')) is int and 0 <= item['row_index'] < len(data)})
    # 老版转换器会忽略非对象行；批次后续的目标位置不能等同于原始位置。
    identity_safe = all(isinstance(row, dict) for row in data) or detail['stage'] == 'transform'
    if not identity_safe:
        indices = []
    exact = bool(indices)
    selected_indices = indices if exact else list(range(len(data)))
    context.update(total_records=len(data), exact_row=exact, scope='rows' if exact else 'batch',
                   matched_records=len(selected_indices), mapping_snapshot_available=has_mapping)
    start = (page - 1) * page_size
    # 单条定位不受历史批次页码影响；多条错误行支持分页。
    page_indices = indices if len(indices) == 1 else selected_indices[start:start + page_size]
    context['records'] = [{'row_number':i+1, 'source':safe_source(data[i]),
                           'source_pointer':(path + '/' + str(i)) if collection_is_list else '/',
                           'issues':[issue for issue in issues if issue.get('row_index')==i]} for i in page_indices]
    context['notice'] = ('以下为已定位的错误源记录（敏感字段已脱敏）' if exact else
                         '无法确定精确错误行；以下为对应批次源数据，不代表每条记录都失败')
    if detail.get('issues_truncated'):
        context['notice'] += f'；错误明细仅保留前 {MAX_ISSUES} 项'
    if snapshot_notice or not has_mapping:
        context['notice'] += '；' + (snapshot_notice or '历史批次未保存映射快照，不使用当前映射推断历史字段关系')
    return desensitize_record(context)
