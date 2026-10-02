"""增量配置、无损水位类型和数据快照序列化。"""
import json
import re
from datetime import date, datetime
from decimal import Decimal
from fastapi import HTTPException
from app.security.crypto import encrypt, decrypt


def reject(code, message, status=422):
    raise HTTPException(status, detail={'code': code, 'message': message})


def validate(mapping, source, task=None, target=None):
    if mapping.sync_mode not in ('full', 'incremental'):
        reject('incremental_mode_invalid', '同步模式必须是 full 或 incremental')
    if mapping.sync_mode != 'incremental':
        return
    field = (mapping.incremental_field or '').strip()
    if not field:
        reject('incremental_field_required', '增量模式必须配置源端增量字段')
    is_db = source is not None and bool(source.db_type)
    pattern = r'[A-Za-z_][A-Za-z0-9_$#]*' if is_db else r'[A-Za-z0-9_$#]+'
    if len(field) > 100 or not re.fullmatch(pattern, field):
        reject('incremental_field_invalid', '增量字段必须是合法列名，不支持 SQL 表达式')
    if source is None or (is_db and source.db_type not in ('sqlite', 'oracle', 'postgresql', 'mysql')) or (
            not is_db and source.protocol not in ('hap', 'rest', 'http')):
        reject('incremental_source_unsupported', '增量支持数据库、HAP 和 REST 源')
    if is_db and not mapping.source_table:
        reject('incremental_table_required', '增量模式必须配置源表')
    if not is_db:
        from app.services.sync_config import side_config, SourceReadConfig, SourceIncrementalConfig
        cfg = side_config(mapping.mapping_config or {}, 'source')
        if not (mapping.source_endpoint or (source.protocol == 'hap' and cfg.get('worksheet_id'))):
            reject('incremental_endpoint_required', '增量模式必须配置源接口端点或 HAP 工作表 ID')
        if source.protocol in ('rest', 'http'):
            if cfg.get('read') is None or cfg.get('incremental') is None:
                reject('incremental_read_config_required', 'REST 增量需配置源读取、源主键和水位类型')
            try:
                SourceReadConfig.model_validate(cfg['read'])
                SourceIncrementalConfig.model_validate(cfg['incremental'])
            except ValueError:
                reject('incremental_read_config_invalid', 'REST 源读取或增量配置无效，请检查请求方式、数组路径、主键与水位类型')
    if (mapping.mapping_config or {}).get('delete_sync'):
        reject('incremental_delete_conflict', '增量模式不能开启删除同步')
    if target is not None and target.protocol == 'hap' and (mapping.mapping_config or {}).get('hap_write_mode') == 'upsert':
        from app.services.hap_target import validate_policy
        validate_policy(mapping, target, task)
        return
    if task is not None:
        if target is not None and target.db_type and not (mapping.mapping_config or {}).get('key_field'):
            reject('incremental_idempotency_required', '数据库增量目标必须配置主键，以支持失败后的安全重试')
        if task.sync_mode not in ('sync', 'async') or task.fallback_to_async or task.webhook_config:
            reject('incremental_mode_unsupported', '增量支持 sync/async，不支持 auto、失败转异步或 Webhook')
        if task.conflict_strategy not in (None, '', 'overwrite'):
            reject('incremental_conflict_unsupported', '增量任务仅支持覆盖策略，不能跳过未完成记录')
        if task.sync_mode == 'async' or (target is not None and not target.db_type):
            if target is None:
                reject('incremental_target_required', '目标连接不存在')
            if target.db_type:
                if not (mapping.mapping_config or {}).get('key_field'):
                    reject('incremental_idempotency_required', '异步增量的数据库目标必须配置主键以支持 upsert 重试')
            else:
                from app.services.sync_config import side_config
                cfg = side_config(mapping.mapping_config or {}, 'target')
                if target.protocol not in ('rest', 'http') or not cfg.get('batch_atomic'):
                    reject('incremental_atomicity_required', '增量 API 目标须为 REST，并声明整批原子写入契约；幂等请求头可留空，留空时重试次数为 0')
                if cfg.get('idempotency_header') and cfg['idempotency_header'].lower() in ('authorization', 'cookie', 'host', 'content-type', 'content-length', 'x-admin-token'):
                    reject('incremental_idempotency_required', '幂等请求头不能覆盖认证或传输协议头')


def target_retry_limit(mapping, target):
    """额外重试次数；0 仍允许首次尝试，不修改共享连接设置。"""
    from app.config import settings
    from app.services.sync_config import side_config
    if target is not None and target.protocol == 'hap' and (mapping.mapping_config or {}).get('hap_write_mode') == 'upsert':
        return max(1, settings.incremental_max_attempts - 1)  # 恢复只回查已发送请求
    if target is None or (not target.db_type and not side_config(mapping.mapping_config or {}, 'target').get('idempotency_header')):
        return 0
    return max(0, settings.incremental_max_attempts - 1)


def field_type(raw):
    value = raw.lower().split('(')[0].strip()
    if any(x in value for x in ('int', 'number', 'numeric', 'decimal', 'real', 'double', 'float')):
        return 'number'
    if value == 'date':
        return 'date'
    if 'timestamp' in value or 'datetime' in value:
        return 'datetime_tz' if 'with time zone' in value else 'datetime'
    reject('incremental_field_invalid', '增量字段仅支持数字或日期时间类型')


def typed(value, kind):
    try:
        if value is None:
            raise ValueError()
        if kind == 'number':
            result = Decimal(str(value))
            if not result.is_finite():
                raise ValueError()
            return result
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if kind == 'datetime_tz' and result.tzinfo is None:
            raise ValueError()
        if kind != 'datetime_tz' and result.tzinfo is not None:
            raise ValueError()
        return result
    except (ValueError, TypeError, ArithmeticError):
        reject('incremental_watermark_invalid', '增量水位为空或与字段类型不兼容，请检查源数据及历史水位')


def watermark(value, kind):
    value = typed(value, kind)
    result = str(value) if kind == 'number' else value.isoformat()
    if len(result) > 200:
        reject('incremental_watermark_invalid', '水位值超出存储长度')
    return result



# API 无时区时间沿用 MES/HAP 的北京时间口径，不依赖 Windows/Linux 主机时区。
def api_typed(value, kind):
    """API 时间兼容解析；DB 类型绑定仍使用严格 typed。"""
    if kind not in ('datetime', 'datetime_tz'):
        return typed(value, kind)
    from datetime import timezone, timedelta
    beijing = timezone(timedelta(hours=8))
    try:
        if value is None:
            raise ValueError()
        text = str(value).strip()
        # Python datetime 精度为微秒，拒绝更高精度以免静默截断水位。
        if re.search(r'\.\d{7,}', text):
            raise ValueError()
        result = value if isinstance(value, datetime) else datetime.fromisoformat(text)
        if result.tzinfo is None:
            result = result.replace(tzinfo=beijing)
        if kind == 'datetime':
            result = result.astimezone(beijing).replace(tzinfo=None)
        return result
    except (ValueError, TypeError, ArithmeticError):
        reject('incremental_watermark_invalid', 'API 增量时间为空或格式无效，请使用 ISO 日期时间（支持毫秒、Z 或时区偏移）')


def api_watermark(value, kind):
    return watermark(api_typed(value, kind), kind)


def get_value(row, field):
    return next((value for key, value in row.items() if key.lower() == field.lower()), None)


def _encode(value):
    # 对所有容器标记类型，避免业务字典与特殊类型标记碰撞。
    if isinstance(value, dict):
        return ['dict', [[key, _encode(item)] for key, item in value.items()]]
    if isinstance(value, list):
        return ['list', [_encode(item) for item in value]]
    if isinstance(value, datetime):
        return ['datetime', value.isoformat()]
    if isinstance(value, date):
        return ['date', value.isoformat()]
    if isinstance(value, Decimal):
        return ['decimal', str(value)]
    if isinstance(value, bytes):
        return ['bytes', value.hex()]
    return ['scalar', value]


def _decode(value):
    kind, item = value
    if kind == 'dict':
        return {key: _decode(v) for key, v in item}
    if kind == 'list':
        return [_decode(v) for v in item]
    return {'datetime': datetime.fromisoformat, 'date': date.fromisoformat,
            'decimal': Decimal, 'bytes': bytes.fromhex}.get(kind, lambda x: x)(item)


def serialize(value):
    return json.dumps(_encode(value), ensure_ascii=False, allow_nan=False)


def seal(value):
    return encrypt(serialize(value))


def unseal(value):
    return _decode(json.loads(decrypt(value)))
