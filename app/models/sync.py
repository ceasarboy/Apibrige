"""SQLAlchemy 模型：sync_tasks, data_buffer, execution_logs"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Integer, Boolean, Text, ForeignKey, Index, JSON
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from app.probe.timestamps import ProbeTimestamp, UtcTimestamp


_utcnow = lambda: datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ApiConnection(Base):
    __tablename__ = "api_connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    connection_type: Mapped[str] = mapped_column(String(20))
    protocol: Mapped[str] = mapped_column(String(20))
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    auth_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    auth_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    headers: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    timeout_ms: Mapped[int] = mapped_column(Integer, default=30000)
    retry_count: Mapped[int] = mapped_column(Integer, default=3)
    db_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    db_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)


class ConnectionField(Base):
    """连接字段字典 — 连接 + 范围(表/工作表) + 字段 的显示名与读写属性

    默认建立时既可读又可写（readable/writable 均为 True）；
    同步时按字典过滤：源 readable=false 不参与映射输出，目标 writable=false 不推送。
    scope 区分同一连接下多套字段定义（DB 按表名、HAP 按工作表 ID）。
    """
    __tablename__ = "connection_fields"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_connections.id"))
    scope: Mapped[str] = mapped_column(String(200), default="")  # DB 表名 / HAP 工作表 ID
    path: Mapped[str] = mapped_column(String(200))  # 字段 ID / 列名
    field_id: Mapped[str | None] = mapped_column(String(200), nullable=True)  # HAP 稳定控件引用
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)  # 可修改的显示名
    readable: Mapped[bool] = mapped_column(Boolean, default=True)
    writable: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)

    __table_args__ = (
        Index("idx_connection_fields_conn_scope_path", "connection_id", "scope", "path", unique=True),
    )


class ConnectionForm(Base):
    """连接表单注册表 — 连接下"可调用表单"的登记与本地命名

    桥是表单到表单的，本表让用户先知道"哪些表单可用"再建桥：
    - form_key: HAP=worksheet_id；DB=表名；REST 手动登记=用户自定义标识
    - original_name: 源端原始名（拉取时写入，刷新时更新）
    - display_name: 本地显示名（用户可改，默认=original_name，拉取不覆盖）
    - source: auto（从源端拉取）/ manual（手动补充）
    删除注册记录不影响已建映射（注册表不参与同步执行链路）。
    """
    __tablename__ = "connection_forms"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_connections.id"))
    form_key: Mapped[str] = mapped_column(String(200))
    original_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    remark: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source: Mapped[str] = mapped_column(String(10), default="auto")  # auto|manual
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)

    __table_args__ = (
        Index("idx_connection_forms_conn_key", "connection_id", "form_key", unique=True),
    )


class FieldMapping(Base):
    __tablename__ = "field_mappings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    source_connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_connections.id"))
    target_connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_connections.id"))
    source_endpoint: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_endpoint: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_table: Mapped[str | None] = mapped_column(String(100), nullable=True)
    target_table: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_headers: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 映射级源请求头覆盖
    target_headers: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 映射级目标请求头覆盖
    mapping_config: Mapped[dict] = mapped_column(JSON)
    transform_script: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_mode: Mapped[str] = mapped_column(String(20), default="full")
    incremental_field: Mapped[str | None] = mapped_column(String(100), nullable=True)
    batch_size: Mapped[int] = mapped_column(Integer, default=1000)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)


class SyncTask(Base):
    __tablename__ = "sync_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mapping_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("field_mappings.id"))
    task_type: Mapped[str] = mapped_column(String(20))
    schedule_cron: Mapped[str | None] = mapped_column(String(50), nullable=True)
    webhook_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    # 传输模式配置
    sync_mode: Mapped[str] = mapped_column(String(20), default="async")  # sync|async|auto
    fallback_to_async: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_threshold: Mapped[int] = mapped_column(Integer, default=100)
    last_run_at: Mapped[datetime | None] = mapped_column(UtcTimestamp(), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(UtcTimestamp(), nullable=True)
    last_sync_value: Mapped[str | None] = mapped_column(String(200), nullable=True)
    active_round_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    checkpoint_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 任务编排
    depends_on_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sync_tasks.id"), nullable=True)
    conflict_strategy: Mapped[str | None] = mapped_column(String(20), nullable=True)  # skip|overwrite|merge
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)


class SyncRound(Base):
    __tablename__ = "sync_rounds"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sync_tasks.id"), index=True)
    from_watermark: Mapped[str | None] = mapped_column(String(200), nullable=True)
    candidate_watermark: Mapped[str | None] = mapped_column(String(200), nullable=True)
    watermark_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    checkpoint_version: Mapped[int] = mapped_column(Integer)
    config_snapshot: Mapped[str] = mapped_column(Text)  # 加密快照，不包含连接凭证
    status: Mapped[str] = mapped_column(String(30), default="preparing", index=True)
    batch_count: Mapped[int] = mapped_column(Integer, default=0)
    sealed_at: Mapped[datetime | None] = mapped_column(UtcTimestamp(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UtcTimestamp(), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attempt_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(UtcTimestamp(), nullable=True)


class DataBuffer(Base):
    __tablename__ = "data_buffer"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sync_tasks.id"))
    round_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sync_rounds.id"), nullable=True)
    batch_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(UtcTimestamp(), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(UtcTimestamp(), nullable=True)
    config_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)  # 普通异步的加密策略快照
    source_data: Mapped[dict] = mapped_column(JSON)
    transformed_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    data_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(UtcTimestamp(), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UtcTimestamp(), nullable=True)

    __table_args__ = (
        Index("idx_data_buffer_status", "status"),
        Index("idx_data_buffer_task", "task_id"),
        Index("idx_data_buffer_hash", "data_hash"),
        Index("idx_round_batch", "round_id", "batch_no", unique=True),
    )


class HapWritePlan(Base):
    """发送前持久化的加密写入计划；active_target 保证同一目标仅一个未决计划。"""
    __tablename__ = 'hap_write_plans'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    buffer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('data_buffer.id'), unique=True, nullable=True)
    probe_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('probe_sessions.id'), unique=True, nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('sync_tasks.id'), nullable=True, index=True)
    target_key: Mapped[str] = mapped_column(String(64), index=True)
    active_target: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    payload: Mapped[str] = mapped_column(Text)  # 输入、控件定义、请求及行结果均加密
    status: Mapped[str] = mapped_column(String(20), default='pending')
    attempt_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(UtcTimestamp(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)


class ExecutionLog(Base):
    __tablename__ = "execution_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sync_tasks.id"))
    buffer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("data_buffer.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(50))  # fetch|transform|push
    status: Mapped[str] = mapped_column(String(20))  # success|failed
    request_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)

    __table_args__ = (
        Index("idx_execution_logs_task", "task_id"),
        Index("idx_execution_logs_created", "created_at"),
    )


class ProbeSession(Base):
    """联调探针会话（task 3.1）—— 一次联调的全部产物与状态，真相源在 PostgreSQL

    状态机: draft → probing → L1_reachable → transform_verified → pending_review
            → approved → committed；任一阶段可转 failed；人可转 rejected；
            超期自动 expired；API 目标无契约降级 L2_partial_no_contract（不得进清单）。
    """
    __tablename__ = "probe_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    # 触发来源: cli|ui|api|mcp（审计用）
    created_via: Mapped[str] = mapped_column(String(10), default="api")
    # 输入形态：内联配置只存脱敏摘要（明文凭证不落库）
    source_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    target_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_connections.id"), nullable=True)
    target_connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_connections.id"), nullable=True)
    source_endpoint: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_endpoint: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 产物（样本落库前已脱敏）
    source_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    target_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    mapping_draft: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    draft_issues: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_sample: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    l1_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    l2_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    l3_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # L3 人工授权闸门
    e2e_authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    authorized_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 审计
    probe_calls: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {"source": n, "target": n}
    audit_log: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # [{"at","from","to","via","summary"}]
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 批准产物：{"connection_ids": [...], "mapping_id": ..., "task_id": ...}
    bridge_ids: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(ProbeTimestamp(), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(ProbeTimestamp(), default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(ProbeTimestamp(), nullable=True)

    __table_args__ = (
        Index("idx_probe_sessions_status", "status"),
        Index("idx_probe_sessions_expires", "expires_at"),
    )


# 探针会话状态机的合法迁移表（task 3.1：非法迁移被拒并保持当前状态）
PROBE_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"probing", "failed"},
    "probing": {"L1_reachable", "transform_verified", "L2_partial_no_contract", "failed"},
    "L1_reachable": {"transform_verified", "L2_partial_no_contract", "probing", "failed"},
    "L2_partial_no_contract": {"probing", "failed"},
    "transform_verified": {"pending_review", "probing", "e2e_verified", "L3_partial", "failed"},
    "e2e_verified": {"pending_review", "failed"},
    "L3_partial": {"pending_review", "probing", "failed"},
    "pending_review": {"approved", "rejected", "probing", "failed", "expired"},
    "approved": {"committed", "failed"},
    # 终态
    "committed": set(),
    "failed": set(),
    "rejected": set(),
    "expired": set(),
}
