"""BI 聚合引擎数据模型

三张表：
- BiMetricDef：聚合指标定义（配置驱动，内置 10 组 + 用户自定义）
- BiMetricBucket：预聚合结果（按 指标+时间桶+维度组合 唯一）
- BiAggregationWatermark：聚合水位线（支持断点续跑与回填）

设计依据见 openspec/changes/add-bi-aggregation-engine/design.md（D1/D2/D3/D6/D11）。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Integer, Boolean, Index, JSON
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from app.probe.timestamps import UtcTimestamp
from sqlalchemy.orm import Mapped, mapped_column

from app.models.sync import Base

_utcnow = lambda: datetime.now(timezone.utc)  # noqa: E731


class BiMetricDef(Base):
    """聚合指标定义

    source_type: data_buffer | execution_logs | custom_sql
    dimensions:  [{"name": "category", "field": "category"}]
                 field 为 JSON 路径（仅 data_buffer 源需要），如 "category" / "info.region"
    measures:    [{"key": "amount_sum", "agg": "sum", "field": "amount"}]
                 field 为 JSON 路径或列名；count 无需 field
    filters:     {"status": "completed"} 形式的等值过滤
    """

    __tablename__ = "bi_metric_defs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    metric_key: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_type: Mapped[str] = mapped_column(String(30))
    source_ref: Mapped[str | None] = mapped_column(String(2000), nullable=True)  # custom_sql 语句
    time_field: Mapped[str] = mapped_column(String(100), default="created_at")
    granularity: Mapped[str] = mapped_column(String(10), default="1min")
    dimensions: Mapped[list] = mapped_column(JSON, default=list)
    measures: Mapped[list] = mapped_column(JSON, default=list)
    filters: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    chart_type: Mapped[str] = mapped_column(String(20), default="line")
    top_n: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 图表级样式（可空 JSON）：number=bg_color/font_color；gauge=gauge_min/gauge_max；
    # unit=单位（如 "%"）
    style: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 行级派生计算字段（add-bi-calc-fields）：
    # [{"key","name","expr","field_type"}]，expr 引用已规范化为字段键
    computed: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 展示层修饰（add-bi-calc-fields）：{"percent": bool, "cumulative": bool}
    display: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 内置定义标记：种子任务只写 builtin=False 之外的新项，且不覆盖用户修改
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)

    def to_def_dict(self) -> dict:
        """转为聚合引擎/校验器使用的普通字典

        引擎层（bi_aggregator）与校验层（bi_defs）均只接受 dict，
        避免把 ORM 对象泄漏进纯逻辑模块。
        """
        return {
            "metric_key": self.metric_key,
            "name": self.name,
            "description": self.description,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "time_field": self.time_field,
            "granularity": self.granularity,
            "dimensions": self.dimensions or [],
            "measures": self.measures or [],
            "filters": self.filters,
            "chart_type": self.chart_type,
            "top_n": self.top_n,
            "enabled": self.enabled,
            "style": self.style,
            "computed": self.computed or [],
            "display": self.display or {},
        }


class BiMetricBucket(Base):
    """预聚合结果桶

    `dim_hash` 为维度组合的稳定哈希，作为 UPSERT 冲突键（避免长 JSON 比较）。
    `values` 存该桶的度量结果，如 {"success": 120, "failed": 3}。
    """

    __tablename__ = "bi_metric_buckets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    metric_key: Mapped[str] = mapped_column(String(100))
    bucket_start: Mapped[datetime] = mapped_column(UtcTimestamp())
    dim_hash: Mapped[str] = mapped_column(String(40))
    dimensions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    values: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)

    __table_args__ = (
        Index("idx_bi_buckets_unique", "metric_key", "bucket_start", "dim_hash", unique=True),
        Index("idx_bi_buckets_lookup", "metric_key", "bucket_start"),
    )


class BiAggregationWatermark(Base):
    """聚合水位线：记录每个指标已计算到的最后一个完整窗口起点"""

    __tablename__ = "bi_aggregation_watermark"

    metric_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_bucket: Mapped[datetime] = mapped_column(UtcTimestamp())
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)


class BiScreenSettings(Base):
    """大屏看板设置（单行 key-value，id='default'）

    data JSON：{"layout": {"order": [metric_key...], "spans": {metric_key: 1|2}},
                "filter_dims": [维度名...]}
    布局与图表定义分离存储（DataEase 布局/绑定分离范式）：
    图表定义在 bi_metric_defs，此处只存排布信息。
    """

    __tablename__ = "bi_screen_settings"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)


class BiAccessProfile(Base):
    """BI 只读访问授权档案（add-bi-access-scope）

    一份档案 = 一个只读令牌 + 一块可见范围：
    - boards：可见看板 key 列表；**空数组 = 不限看板**（管理员显式选择）
    - row_filters：{字段: 值}，多字段 AND（字段口径 = 定义里的维度 field / 明细列名）
    - hidden_fields：不可见字段（度量 key / 维度 field / 明细列 path）
    令牌明文只在创建/轮换响应里出现一次，库内只存 sha256 哈希（与凭证加密同一纪律）。
    """

    __tablename__ = "bi_access_profiles"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(16), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    boards: Mapped[list] = mapped_column(JSON, default=list)
    row_filters: Mapped[dict] = mapped_column(JSON, default=dict)
    hidden_fields: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcTimestamp(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), default=_utcnow)
