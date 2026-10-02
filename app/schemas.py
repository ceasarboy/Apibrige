"""API 请求/响应 Pydantic 模型"""
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field, field_validator, model_validator

# 创建/更新期的枚举校验（合并自审查整改：早失败优于运行期静默走错分支）
ALLOWED_MAPPING_SYNC_MODES = {"full", "incremental"}
ALLOWED_TASK_TYPES = {"realtime", "scheduled"}
ALLOWED_TASK_SYNC_MODES = {"sync", "async", "auto"}
ALLOWED_CONFLICT_STRATEGIES = {"skip", "overwrite"}


def _check_enum(value: str | None, allowed: set, label: str) -> str | None:
    if value is None:
        return value
    if value not in allowed:
        raise ValueError(f"{label} 必须是 {sorted(allowed)} 之一，收到: {value!r}")
    return value


def _allowed_protocols() -> set[str]:
    # 协议 = API 适配器注册表 ∪ 数据库连接器注册表（DB 连接的 protocol 记 db 类型）
    from app.adapters.factory import ProtocolAdapterFactory
    from app.connectors.database import _CONNECTOR_REGISTRY
    return ({p.lower() for p in ProtocolAdapterFactory.get_supported_protocols()}
            | set(_CONNECTOR_REGISTRY.keys()))


class ConnectionValidation(BaseModel):
    @field_validator('protocol', mode='before', check_fields=False)
    @classmethod
    def validate_protocol(cls, value):
        if not value:
            return value
        v = str(value).lower()
        if v not in _allowed_protocols():
            raise ValueError(f'不支持的协议: {value}')
        return v

    @field_validator('connection_type', mode='before', check_fields=False)
    @classmethod
    def validate_connection_type(cls, value):
        if value is None:
            return value
        if str(value) not in ('source', 'target'):
            raise ValueError("连接类型只支持 source/target")
        return value

    @model_validator(mode='after')
    def validate_auth_headers(self):
        from app.security.headers import validate_headers
        if getattr(self,'auth_type',None) == 'custom':
            validate_headers(getattr(self,'auth_config',None))
        if getattr(self,'protocol',None) in ('hap','hapv3') and getattr(self,'auth_type',None) not in (None,'custom'):
            raise ValueError('HAP 使用应用鉴权参数，请选择自定义认证')
        return self
    @field_validator('auth_type', mode='before', check_fields=False)
    @classmethod
    def normalize_auth(cls, value):
        if value in (None, '', 'none'):
            return None
        if value not in ('api_key', 'bearer', 'basic', 'oauth2', 'sign', 'custom'):
            raise ValueError('不支持的认证类型')
        return value

    @field_validator('headers', check_fields=False)
    @classmethod
    def check_headers(cls, value):
        from app.security.headers import validate_headers
        return validate_headers(value)


class MappingValidation(BaseModel):
    @field_validator('mapping_config', check_fields=False)
    @classmethod
    def check_config(cls, value):
        from app.services.sync_config import validate_mapping_config
        return validate_mapping_config(value) if value is not None else value

    @field_validator('source_headers', 'target_headers', check_fields=False)
    @classmethod
    def check_headers(cls, value):
        from app.security.headers import validate_headers
        return validate_headers(value)

    @field_validator('sync_mode', check_fields=False)
    @classmethod
    def check_sync_mode(cls, value):
        return _check_enum(value, ALLOWED_MAPPING_SYNC_MODES,
                           "sync_mode（映射语义为拉取方式，区别于任务的执行通道）")


class TaskValidation(BaseModel):
    """任务字段校验（TaskCreate/TaskUpdate 共用）"""

    @field_validator('task_type', check_fields=False)
    @classmethod
    def check_task_type(cls, value):
        return _check_enum(value, ALLOWED_TASK_TYPES, "task_type")

    @field_validator('sync_mode', check_fields=False)
    @classmethod
    def check_sync_mode(cls, value):
        return _check_enum(value, ALLOWED_TASK_SYNC_MODES,
                           "sync_mode（任务语义为执行通道，区别于映射的拉取方式）")

    @field_validator('conflict_strategy', check_fields=False)
    @classmethod
    def check_conflict_strategy(cls, value):
        return _check_enum(value, ALLOWED_CONFLICT_STRATEGIES, "conflict_strategy")


# ============================================================
# 连接管理
# ============================================================

class ConnectionCreate(ConnectionValidation):
    """创建连接请求"""
    name: str = Field(..., max_length=100, description="连接名称")
    connection_type: str = Field("source", description="连接类型: source/target")
    protocol: str = Field(..., description="协议: rest/http/soap/graphql/websocket/grpc/dubbo/mysql/oracle/sqlite/postgresql")
    base_url: str | None = Field(None, max_length=500, description="API 根 URL 或 WSDL/网关地址")
    auth_type: str | None = Field(None, description="认证类型: api_key/bearer/basic/oauth2/sign/custom")
    auth_config: dict | None = Field(None, description="认证配置（明文，存储前加密）。custom 类型可自由定义键值对")
    headers: dict | None = Field(None, description="自定义请求头（支持 {{timestamp}} {{uuid}} 等变量）")
    timeout_ms: int = Field(30000, ge=1000, le=300000)
    retry_count: int = Field(3, ge=0, le=10)
    db_type: str | None = Field(None, description="数据库类型: mysql/oracle/sqlite/postgresql")
    db_config: dict | None = Field(None, description="数据库连接配置")


class ConnectionUpdate(ConnectionValidation):
    """更新连接请求（所有字段可选）"""
    name: str | None = Field(None, max_length=100)
    connection_type: str | None = None
    protocol: str | None = None
    base_url: str | None = Field(None, max_length=500)
    auth_type: str | None = None
    auth_config: dict | None = None
    headers: dict | None = None
    timeout_ms: int | None = Field(None, ge=1000, le=300000)
    retry_count: int | None = Field(None, ge=0, le=10)
    db_type: str | None = None
    db_config: dict | None = None
    status: str | None = Field(None, description="状态: active/inactive")


class ConnectionResponse(BaseModel):
    """连接响应（敏感字段已脱敏）"""
    id: UUID
    name: str
    connection_type: str
    protocol: str
    base_url: str | None
    auth_type: str | None
    auth_config: dict | None
    headers: dict | None
    timeout_ms: int
    retry_count: int
    db_type: str | None
    db_config: dict | None
    status: str
    created_at: datetime
    updated_at: datetime


class ConnectionTestResult(BaseModel):
    """连接测试结果"""
    status: str = Field(..., description="ok/failed")
    latency_ms: int
    error: str | None = None


class ConnectionTestConfig(ConnectionValidation):
    """未入库连接配置的测试请求（先测试后保存）"""
    name: str | None = None
    connection_type: str = "source"
    protocol: str = "rest"
    base_url: str | None = None
    auth_type: str | None = None
    auth_config: dict | None = None
    headers: dict | None = None
    timeout_ms: int = 30000
    retry_count: int = 3
    db_type: str | None = None
    db_config: dict | None = None


class ConnectionTestLogLine(BaseModel):
    """测试运行日志行"""
    time: str
    level: str = "info"
    message: str


class ConnectionTestRun(BaseModel):
    """测试运行结果 + 运行日志"""
    result: ConnectionTestResult
    log: list[ConnectionTestLogLine]


# ============================================================
# 字段映射
# ============================================================

class MappingCreate(MappingValidation):
    """创建字段映射请求"""
    name: str = Field(..., max_length=100)
    source_connection_id: UUID
    target_connection_id: UUID
    source_endpoint: str | None = Field(None, max_length=200)
    target_endpoint: str | None = Field(None, max_length=200)
    source_table: str | None = Field(None, max_length=100)
    target_table: str | None = Field(None, max_length=100)
    source_headers: dict | None = Field(None, description="映射级源请求头（覆盖连接级，支持 {{变量}}）")
    target_headers: dict | None = Field(None, description="映射级目标请求头（覆盖连接级，支持 {{变量}}）")
    mapping_config: dict = Field(..., description="字段映射配置")
    transform_script: str | None = None
    sync_mode: str = Field("full", description="full/incremental")
    incremental_field: str | None = Field(None, max_length=100)
    batch_size: int = Field(1000, ge=1, le=100000)


class MappingUpdate(MappingValidation):
    """更新字段映射请求"""
    name: str | None = Field(None, max_length=100)
    source_connection_id: UUID | None = None
    target_connection_id: UUID | None = None
    source_endpoint: str | None = None
    target_endpoint: str | None = None
    source_table: str | None = None
    target_table: str | None = None
    source_headers: dict | None = None
    target_headers: dict | None = None
    mapping_config: dict | None = None
    transform_script: str | None = None
    sync_mode: str | None = None
    incremental_field: str | None = Field(None, max_length=100)
    batch_size: int | None = Field(None, ge=1, le=100000)
    status: str | None = None


class MappingResponse(BaseModel):
    id: UUID
    name: str
    source_connection_id: UUID
    target_connection_id: UUID
    source_endpoint: str | None
    target_endpoint: str | None
    source_table: str | None
    target_table: str | None
    source_headers: dict | None
    target_headers: dict | None
    mapping_config: dict
    transform_script: str | None
    sync_mode: str
    incremental_field: str | None
    batch_size: int
    status: str
    created_at: datetime
    updated_at: datetime


class MappingPreviewRequest(BaseModel):
    """映射转换预览请求"""
    test_data: dict = Field(..., description="测试源数据")


class MappingPreviewResponse(BaseModel):
    """映射转换预览响应"""
    success: bool
    transformed_data: dict | None = None
    error: str | None = None


# ============================================================
# 同步任务
# ============================================================

class TaskCreate(TaskValidation):
    """创建任务请求"""
    mapping_id: UUID
    task_type: str = Field("scheduled", description="realtime/scheduled")
    schedule_cron: str | None = Field(None, max_length=50)
    webhook_config: dict | None = None
    sync_mode: str = Field("async", description="sync/async/auto")
    fallback_to_async: bool = False
    auto_threshold: int = Field(100, ge=1, le=100000)
    # P1: 任务编排 + 冲突检测
    depends_on_task_id: UUID | None = Field(None, description="前置依赖任务ID")
    conflict_strategy: str | None = Field(None, description="冲突策略: skip/overwrite")


class TaskUpdate(TaskValidation):
    """更新任务请求"""
    mapping_id: UUID | None = None
    task_type: str | None = None
    schedule_cron: str | None = None
    webhook_config: dict | None = None
    sync_mode: str | None = None
    fallback_to_async: bool | None = None
    auto_threshold: int | None = Field(None, ge=1, le=100000)  # 与 TaskCreate 同边界
    depends_on_task_id: UUID | None = None
    conflict_strategy: str | None = None


class TaskResponse(BaseModel):
    id: UUID
    mapping_id: UUID
    task_type: str
    schedule_cron: str | None
    webhook_config: dict | None
    status: str
    sync_mode: str
    fallback_to_async: bool
    auto_threshold: int
    last_run_at: datetime | None
    next_run_at: datetime | None
    last_sync_value: str | None
    active_round_id: UUID | None = None
    checkpoint_version: int = 0
    depends_on_task_id: UUID | None = None
    conflict_strategy: str | None = None
    created_at: datetime
    updated_at: datetime


# ============================================================
# 日志查询
# ============================================================

class LogResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    task_id: UUID
    buffer_id: UUID | None
    action: str
    status: str
    request_data: dict | None
    response_data: dict | None
    error_message: str | None
    duration_ms: int | None
    created_at: datetime


class PaginatedLogs(BaseModel):
    """分页日志响应"""
    items: list[LogResponse]
    total: int
    page: int
    page_size: int


# ============================================================
# 监控统计
# ============================================================

class MetricsOverview(BaseModel):
    """监控概览"""
    sync_count: int
    success_rate: float
    p50_latency: float
    p95_latency: float
    p99_latency: float
    queue_depth: int
    task_status_distribution: dict[str, int]


class MetricsTrendItem(BaseModel):
    """趋势数据项"""
    timestamp: str
    success: int
    failed: int


class MetricsTrend(BaseModel):
    """趋势数据"""
    items: list[MetricsTrendItem]
    interval: str


# ============================================================
# 通用响应
# ============================================================

class MessageResponse(BaseModel):
    """通用消息响应"""
    message: str
    id: UUID | None = None


class ConnectionFieldItem(BaseModel):
    field_id: str | None = Field(None, max_length=200)
    """字段字典项（读写属性默认双可读写）"""
    path: str = Field(..., max_length=200, description="字段 ID / 列名")
    name: str | None = Field(None, max_length=200, description="可修改的显示名")
    readable: bool = Field(True, description="是否可读")
    writable: bool = Field(True, description="是否可写")


class ConnectionFieldSave(BaseModel):
    """保存连接字段字典请求"""
    fields: list[ConnectionFieldItem] = Field(default_factory=list)


# ============================================================
# 表单注册表（ConnectionForm）
# ============================================================

class FormCreate(BaseModel):
    """手动登记表单请求"""
    connection_id: UUID
    form_key: str = Field(..., min_length=1, max_length=200,
                          description="HAP=worksheet_id；DB=表名；REST=自定义标识")
    display_name: str | None = Field(None, max_length=200, description="本地显示名")
    remark: str | None = Field(None, max_length=500, description="备注：说明这是什么表单")


class FormUpdate(BaseModel):
    """编辑表单请求（仅显示名与备注；original_name/source/form_key 不变）"""
    display_name: str | None = Field(None, max_length=200)
    remark: str | None = Field(None, max_length=500)


class FormBridgeRef(BaseModel):
    """表单被映射引用的明细项"""
    mapping_id: str
    mapping_name: str
    side: str = Field(..., description="本表单在桥中的方向: source|target")
    peer_connection_id: str
    peer_connection_name: str
    peer_form: str = Field(..., description="对端表单技术标识（ws id / 表名 / endpoint）")


class FormResponse(BaseModel):
    """表单注册表响应项（含桥引用统计）"""
    id: UUID
    connection_id: UUID
    connection_name: str = ""
    form_key: str
    original_name: str | None
    display_name: str | None
    remark: str | None
    source: str
    bridge_count: int = 0
    bridges: list[FormBridgeRef] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class FormSyncResponse(BaseModel):
    """从源端拉取表单合并结果"""
    added: int
    refreshed: int
    total: int


# ============================================================
# 字段映射
# ============================================================

