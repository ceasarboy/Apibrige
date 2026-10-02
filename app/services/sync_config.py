"""同步配置的版本边界；协议参数不跨源/目标传播。"""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class RequestConfig(StrictModel):
    mode: Literal['raw', 'array', 'object', 'wrapped_array'] = 'raw'
    records_path: str | None = None
    extra_body: dict = Field(default_factory=dict)
    max_records: int | None = Field(None, gt=0)
    on_empty: Literal['skip', 'error'] = 'skip'

    @model_validator(mode='after')
    def valid_wrapper(self):
        if self.mode == 'wrapped_array':
            from app.services.rest_contract import wrap_records
            wrap_records([], self.records_path, self.extra_body)
        elif self.records_path or self.extra_body:
            raise ValueError('records_path/extra_body 仅用于 wrapped_array')
        return self


class Condition(StrictModel):
    path: str
    operator: Literal['eq', 'ne', 'in', 'exists'] = 'eq'
    value: Any = None

    @model_validator(mode='after')
    def valid_condition(self):
        from app.services.rest_contract import pointer_parts
        pointer_parts(self.path)
        if self.operator == 'in' and not isinstance(self.value, list):
            raise ValueError('in 条件的 value 必须是数组')
        return self


class ResponseConfig(StrictModel):
    success_mode: Literal['http_only', 'http_and_body'] = 'http_only'
    body_format: Literal['json', 'text'] = 'json'
    http_statuses: list[int] | None = None
    match: Literal['all', 'any'] = 'all'
    conditions: list[Condition] = Field(default_factory=list)
    message_path: str | None = None

    @model_validator(mode='after')
    def valid_conditions(self):
        if self.http_statuses is not None and (not self.http_statuses or any(type(v) is not int or not 200 <= v < 300 for v in self.http_statuses)):
            raise ValueError('http_statuses 必须为非空 2xx 状态集合')
        if self.success_mode == 'http_and_body' and (self.body_format != 'json' or not self.conditions):
            raise ValueError('http_and_body 需要 JSON 响应和至少一个判断条件')
        if self.message_path:
            from app.services.rest_contract import pointer_parts
            pointer_parts(self.message_path)
        return self


class SourcePaginationConfig(StrictModel):
    mode: Literal['none', 'page', 'cursor'] = 'none'
    location: Literal['query', 'body'] = 'query'
    page_param: str = Field('page', min_length=1, max_length=100)
    size_param: str | None = Field('pageSize', min_length=1, max_length=100)
    cursor_param: str = Field('cursor', min_length=1, max_length=100)
    page_start: int = Field(1, ge=0, strict=True)
    cursor_start: str | int | None = None
    page_size: int = Field(1000, gt=0, le=100000, strict=True)
    max_pages: int = Field(1000, gt=0, le=100000, strict=True)
    end_condition: Literal['short_page', 'total', 'has_more'] = 'short_page'
    total_path: str | None = None
    has_more_path: str | None = None
    next_cursor_path: str | None = None

    def parameter_names(self):
        if self.mode == 'none':
            return set()
        return {self.page_param if self.mode == 'page' else self.cursor_param} | ({self.size_param} if self.size_param else set())

    @model_validator(mode='before')
    @classmethod
    def valid_cursor_type(cls, values):
        if isinstance(values, dict) and values.get('cursor_start') is not None and type(values['cursor_start']) not in (str, int):
            raise ValueError('起始游标必须是字符串、整数或 null')
        return values

    @model_validator(mode='after')
    def valid_pagination(self):
        from app.services.rest_contract import pointer_parts
        for path in (self.total_path, self.has_more_path, self.next_cursor_path):
            if path is not None:
                pointer_parts(path)
        if self.mode == 'none':
            return self
        main = self.page_param if self.mode == 'page' else self.cursor_param
        if main == self.size_param:
            raise ValueError('页码/游标参数名不能与每页条数参数名重复')
        if self.mode == 'cursor' and not self.next_cursor_path:
            raise ValueError('游标分页必须配置 next_cursor_path')
        if self.mode == 'page' and ((self.end_condition == 'total' and not self.total_path) or (
                self.end_condition == 'has_more' and not self.has_more_path)):
            raise ValueError('所选分页结束条件必须配置对应的响应路径')
        return self


class SourceReadConfig(StrictModel):
    method: Literal['GET', 'POST'] = 'GET'
    query: dict = Field(default_factory=dict)
    body: dict | None = None
    records_path: str = ''
    pagination: SourcePaginationConfig = Field(default_factory=SourcePaginationConfig)

    @model_validator(mode='after')
    def valid_read(self):
        if self.method == 'GET' and self.body is not None:
            raise ValueError('GET 源读取不能配置 JSON 请求体')
        if self.records_path:
            from app.services.rest_contract import pointer_parts
            pointer_parts(self.records_path)
        if self.pagination.mode != 'none':
            if self.pagination.location == 'body' and self.method != 'POST':
                raise ValueError('JSON 请求体分页参数仅支持 POST')
            fixed = self.query if self.pagination.location == 'query' else (self.body or {})
            if self.pagination.parameter_names() & fixed.keys():
                raise ValueError('固定参数不能重复配置页码、游标或每页条数参数')
        return self


class SourceIncrementalConfig(StrictModel):
    key_field: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_$#]+$')
    field_type: Literal['number', 'date', 'datetime', 'datetime_tz']


class HapBulkConfig(StrictModel):
    # 项目保守默认；并非声称 HAP 官方写入上限。
    create_batch_size: int = Field(100, ge=1, le=1000, strict=True)
    update_batch_size: int = Field(100, ge=1, le=1000, strict=True)
    trigger_workflow: bool = False
    null_policy: Literal['omit', 'clear'] = 'omit'


class ProtocolSideConfig(StrictModel):
    hap_bulk: HapBulkConfig | None = None
    idempotency_header: str | None = Field(None, pattern=r'^[A-Za-z0-9-]+$', max_length=100)
    batch_atomic: bool = False
    field_keys: Literal['raw', 'alias', 'id', 'alias_or_id'] = 'raw'
    worksheet_id: str | None = None
    view_id: str | None = None
    # HAP V2 原生 filters 数组；字段/条件编码由明道云工作表配置决定。
    filters: list[dict] | None = None
    method: Literal['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'] = 'POST'
    request: RequestConfig | None = None
    response: ResponseConfig | None = None
    read: SourceReadConfig | None = None
    incremental: SourceIncrementalConfig | None = None


class ProtocolConfig(StrictModel):
    source: ProtocolSideConfig = Field(default_factory=ProtocolSideConfig)
    target: ProtocolSideConfig = Field(default_factory=ProtocolSideConfig)


class MappingOptions(StrictModel):
    missing_source: Literal['null', 'error'] = 'error'
    conversion_error: Literal['keep', 'error'] = 'error'


class ValidationRule(StrictModel):
    required: bool = False
    type: Literal['int', 'float', 'str', 'string', 'bool'] | None = None
    min: float | None = None
    max: float | None = None
    min_length: int | None = Field(None, ge=0)
    max_length: int | None = Field(None, ge=0)
    pattern: str | None = None
    enum: list[Any] | None = None


class ContractField(StrictModel):
    name: str = Field(min_length=1)
    type: str
    required: bool = False
    nullable: bool | None = None
    is_array: bool = False


class FieldRule(StrictModel):
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    transform: Literal['string', 'int', 'float', 'upper', 'lower', 'trim'] | None = None
    on_missing: Literal['null', 'error', 'default'] | None = None
    default_value: Any = None
    source_ref: dict[str, str] | None = None

    @model_validator(mode='after')
    def default_present(self):
        if self.on_missing == 'default' and 'default_value' not in self.model_fields_set:
            raise ValueError('on_missing=default 需要 default_value')
        return self


def validate_mapping_config(config: dict) -> dict:
    # 页面“filters 数组”输入中常见的 {"filters": [...]} 包装形式，统一展开保存。
    protocol = config.get('protocol') if isinstance(config, dict) else None
    source = protocol.get('source') if isinstance(protocol, dict) else None
    filters = source.get('filters') if isinstance(source, dict) else None
    if isinstance(filters, dict) and set(filters) == {'filters'} and isinstance(filters['filters'], list):
        config = dict(config)
        config['protocol'] = dict(protocol)
        config['protocol']['source'] = dict(source)
        config['protocol']['source']['filters'] = filters['filters']
    version = config.get('version', 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError('不支持的 mapping_config.version')
    if config.get('field_policy', 'off') not in ('off', 'inherit'):
        raise ValueError('field_policy 必须为 off/inherit')
    if config.get('hap_write_mode', 'append') not in ('append', 'deduplicate', 'upsert'):
        raise ValueError('hap_write_mode 必须为 append/deduplicate/upsert')
    if type(config.get('hap_delete_sync', False)) is not bool:
        raise ValueError('hap_delete_sync 必须为布尔值')
    if version == 2:
        unknown = set(config) - {'version', 'fields', 'mapping_options', 'protocol', 'validations', 'target_contract', 'key_field', 'delete_sync', 'field_policy', 'hap_write_mode', 'hap_delete_sync'}
        if unknown:
            raise ValueError(f'mapping_config 未知配置: {sorted(unknown)}')
        ProtocolConfig.model_validate(config.get('protocol') or {})
        MappingOptions.model_validate(config.get('mapping_options') or {})
        fields = [FieldRule.model_validate(f) for f in config.get('fields', [])]
        for item in config.get('target_contract') or []:
            ContractField.model_validate(item)
        if len({f.target for f in fields}) != len(fields):
            raise ValueError('fields 中存在重复目标字段')
        validate_rules(config.get('validations') or {})
    return config


def validate_rules(rules: dict) -> None:
    import re
    allowed = {'required', 'type', 'min', 'max', 'min_length', 'max_length', 'pattern', 'enum'}
    if not isinstance(rules, dict):
        raise ValueError('validations 必须是字段规则对象')
    for name, rule in rules.items():
        ValidationRule.model_validate(rule)
        if not isinstance(rule, dict) or set(rule) - allowed:
            raise ValueError(f'validations.{name} 存在无效规则')
        if 'type' in rule and rule['type'] not in ('int', 'float', 'str', 'string', 'bool'):
            raise ValueError(f'validations.{name}.type 不支持')
        if 'pattern' in rule:
            try:
                re.compile(rule['pattern'])
            except (re.error, TypeError):
                raise ValueError(f'validations.{name}.pattern 无效') from None


def side_config(config: dict, side: str) -> dict:
    validate_mapping_config(config)
    if config.get('version', 1) == 1:
        return dict(config.get('protocol') or {})
    return dict((config.get('protocol') or {}).get(side) or {})
