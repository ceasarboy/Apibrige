"""数据转换器（声明式字段映射）"""
from typing import Any
from app.adapters.errors import AdapterError

_MISSING = object()


def transform_data(source_data: dict, mapping_config: dict) -> dict:
    """按映射配置转换数据

    Args:
        source_data: 源数据
        mapping_config: {"fields": [{"source": "a", "target": "b", "transform": "string"}, ...]}

    Returns:
        转换后的数据
    """
    fields = mapping_config.get("fields", [])
    result = {}
    for field_map in fields:
        source_path = field_map["source"]
        target_field = field_map["target"]
        transform_type = field_map.get("transform")

        options = mapping_config.get('mapping_options') or {}
        strict = mapping_config.get('version') == 2
        value = _get_nested(source_data, source_path, _MISSING)
        ref = field_map.get('source_ref') or {}
        key_mode = ((mapping_config.get('protocol') or {}).get('source') or {}).get('field_keys', 'raw')
        if key_mode == 'alias_or_id' and ref:
            cid, alias = ref.get('field_id'), ref.get('alias')
            if cid in source_data and alias in source_data and source_data[cid] != source_data[alias]:
                raise AdapterError('field_key_conflict', f'字段ID与别名值不同: {target_field}', 'field_resolve')
            if value is _MISSING and source_path in (cid, alias):
                alternate = alias if source_path == cid else cid
                if alternate:
                    value = source_data.get(alternate, _MISSING)
        if value is _MISSING:
            policy = field_map.get('on_missing') or options.get('missing_source') \
                or ('error' if strict else 'skip')
            if policy == 'error':
                raise AdapterError('source_field_missing', f'源字段不存在: {source_path}', 'field_resolve', details={'source_field': source_path, 'target_field': target_field})
            if policy == 'default':
                value = field_map.get('default_value')
            elif policy == 'null':
                value = None
            else:
                # skip（遗留映射默认）：**不产出目标键**——upsert 不会用 null 清空目标已有值。
                # 新映射经 sync_config 校验后默认 missing_source='error'（显式报错），
                # 需要写 null 时显式配 on_missing: null。
                continue
        if transform_type and value is not None:
            try:
                value = _apply_transform(value, transform_type, options.get('conversion_error', 'error' if strict else 'keep') == 'error')
            except AdapterError as exc:
                exc.details.update(source_field=source_path, target_field=target_field)
                raise
        result[target_field] = value
    return result


def _get_nested(data: dict, path: str, default=None) -> Any:
    """获取嵌套字段值（支持 a.b.c 路径）"""
    for key in path.split("."):
        if isinstance(data, dict):
            data = data.get(key, default)
        else:
            return default
    return data


def _apply_transform(value: Any, transform: str, strict: bool = False) -> Any:
    """应用类型转换"""
    transforms = {
        "string": str,
        "int": int,
        "float": float,
        "upper": lambda x: str(x).upper(),
        "lower": lambda x: str(x).lower(),
        "trim": lambda x: str(x).strip(),
    }
    func = transforms.get(transform)
    if func is None:
        return value
    try:
        return func(value)
    except (ValueError, TypeError):
        if strict:
            raise AdapterError('transform_failed', f'无法转换为 {transform}', 'transform') from None
        return value


_WRAPPER_KEYS = ("data", "items", "results", "list", "records", "rows")


def transform_payload(source_data, mapping_config: dict) -> list[dict]:
    """列表源（或含列表包装键的 dict）逐条转换，dict 源单条转换

    与探针 L2 的逐条转换语义一致：DB 拉取的行列表、API 返回的记录数组
    都按记录映射，产出记录列表。
    """
    if isinstance(source_data, list):
        result = []
        for index, record in enumerate(source_data):
            if not isinstance(record, dict):
                if mapping_config.get('version') == 2:
                    raise AdapterError('transform_failed', '源记录必须为对象', 'transform', details={'index': index})
                continue
            try:
                result.append(transform_data(record, mapping_config))
            except AdapterError as exc:
                exc.details['index'] = index
                raise
        return result
    if isinstance(source_data, dict):
        for key in _WRAPPER_KEYS:
            value = source_data.get(key)
            if isinstance(value, list):
                return transform_payload(value, mapping_config)
        try:
            return [transform_data(source_data, mapping_config)]
        except AdapterError as exc:
            exc.details['index'] = 0
            raise
    return []
