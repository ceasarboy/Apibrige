"""API 源先完整读取并验证，再按水位筛选；沿用既有轮次封存和提交。"""
import hashlib
import json

from app.adapters.factory import ProtocolAdapterFactory
from app.config import settings
from app.services.adapter_config import build_adapter_config
from app.services.incremental import validate, reject, api_typed, api_watermark, serialize, field_type


def _checked_rows(rows, value_of, key_field, kind):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        reject('incremental_records_invalid', '源接口必须返回记录对象数组，请检查数组路径')
    seen, size, ordered = set(), 0, []
    for row in rows:
        key = row.get(key_field)
        if type(key) not in (str, int) or key == '':
            reject('incremental_primary_key_required', '源记录缺少非空字符串或整数主键')
        identity = (type(key).__name__, str(key))
        if identity in seen:
            reject('incremental_source_duplicate', '源记录主键重复或分页出现重叠，请确认读取范围及分页顺序')
        seen.add(identity)
        value = api_typed(value_of(row), kind)
        try:
            size += len(serialize(row).encode('utf-8'))
        except (TypeError, ValueError):
            reject('incremental_records_invalid', '源记录含无法封存的数据类型')
        if len(seen) > settings.incremental_max_rows or size > settings.incremental_max_bytes:
            reject('incremental_round_too_large', '源完整读取超过条数或字节上限，请缩小接口返回范围')
        ordered.append((value, identity, row))
    ordered.sort(key=lambda item: (item[0], item[1]))
    return ordered


async def read_api_snapshot(source, mapping, last):
    validate(mapping, source)
    config = build_adapter_config(source, mapping, 'source')
    config['_scan_max_rows'] = settings.incremental_max_rows
    config['_scan_max_bytes'] = settings.incremental_max_bytes
    config['_runtime_vars'] = {'last_watermark': last, 'candidate_watermark': None}
    config.pop('sample_limit', None)  # 轮次必须完整读取，不能沿用探针样本上限。
    adapter = ProtocolAdapterFactory.create(source.protocol, config)
    endpoint = (config.get('worksheet_id') or mapping.source_endpoint) if source.protocol == 'hap' else mapping.source_endpoint
    if source.protocol == 'hap':
        config['_stable_scan'] = True
        controls = await adapter.get_worksheet_controls(endpoint)
        column = next((c for c in controls if mapping.incremental_field in (c.get('controlId'), c.get('alias'))), None)
        if not column:
            reject('incremental_field_invalid', 'HAP 工作表不存在配置的增量字段，请获取字段后重新选择')
        kind = field_type({6: 'number', 8: 'number', 15: 'date', 16: 'datetime'}.get(column.get('type'), 'string'))
        key_field = 'rowid'
        def value_of(row):
            for key in (column.get('controlId'), column.get('alias')):
                if key and key in row:
                    return row[key]
            return None
    else:
        kind, key_field = config['incremental']['field_type'], config['incremental']['key_field']
        value_of = lambda row: row.get(mapping.incremental_field)
    lower = api_typed(last, kind) if last is not None else None
    previous_digest = None
    stable_scan = source.protocol == 'hap' or ((config.get('read') or {}).get('pagination') or {}).get('mode', 'none') != 'none'
    for scan_index in range(2 if stable_scan else 1):
        records = await adapter.fetch_data(endpoint)
        ordered = _checked_rows(records, value_of, key_field, kind)
        if stable_scan:
            digest = hashlib.sha256()
            for _, identity, row in ordered:
                digest.update(json.dumps([identity, row], sort_keys=True, ensure_ascii=False,
                                         separators=(',', ':'), allow_nan=False).encode('utf-8'))
                digest.update(b'\n')
            current_digest = digest.digest()
            if previous_digest is not None and previous_digest != current_digest:
                reject('incremental_source_changed', '源的两次分页读取结果不同，本轮未写目标；请待源数据稳定后重试')
            previous_digest = current_digest
            if scan_index == 0:
                # 只保留第一遍摘要，不同时在内存保存两份完整源数据。
                del records, ordered
    selected = [row for value, _, row in ordered if lower is None or value > lower]
    candidate = api_watermark(ordered[-1][0], kind) if selected else last
    return selected, candidate, kind
