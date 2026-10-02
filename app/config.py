"""APIBridge 配置管理"""
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APIBRIDGE_")
    # PostgreSQL
    database_url: str = "postgresql+asyncpg://apibridge:apibridge@localhost:5432/apibridge"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # RabbitMQ
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"

    # 任务队列
    task_queue_name: str = "apibridge.sync"

    # auto 模式阈值
    auto_sync_threshold: int = 100

    # 缓存
    schema_cache_ttl: int = 3600  # 表结构缓存 TTL（秒）
    write_buffer_timeout: float = 5.0  # 写入缓冲超时（秒）

    # 限流默认值
    rate_limit_default: int = 100  # 每秒请求数

    # 认证加密：Fernet 密钥（32 字节 base64 编码字符串）
    # 生产环境必须通过 APIBRIDGE_ENCRYPT_KEY 环境变量配置
    encrypt_key: str = ""

    # Oracle 客户端模式（进程级，python-oracledb 限制：thin/thick 不可按连接混用）
    # thin: 纯 Python，连 Oracle 12.1+，无需任何客户端库
    # thick: 需 Instant Client 19+（21+ 已放弃 11g 服务端），可连 11g（目标系统版本）
    oracle_client_mode: Literal["thin", "thick"] = "thin"
    oracle_client_lib_dir: str = ""  # Instant Client 解压目录，空则用系统 PATH

    # 管理面鉴权（task 2.1-2.3）
    env: Literal["dev", "production"] = "dev"
    # Webhook：端点豁免管理令牌（外部系统不持有管理令牌），故默认强制要求 secret；
    # 置 false 即回到"仅带 secret 时校验"的宽松模式（需自备网络层防护）
    webhook_require_secret: bool = True
    webhook_max_body_bytes: int = 0  # 0 保持现有请求大小行为
    admin_token: str = ""  # APIBRIDGE_ADMIN_TOKEN；production 下为空拒绝启动

    # 探针安全边界（task 2.6-2.9）
    probe_allowed_hosts: str = ""  # 逗号分隔的显式白名单（私网目标需列入）
    probe_ssrf_enforce: bool = False  # 探针目标 SSRF 校验总开关；本地部署内网目标默认关
    probe_max_body_bytes: int = 1_000_000  # 响应体上限，超限截断
    probe_budget: int = 10  # 单会话对同一目标的调用预算
    probe_session_ttl_days: int = 7  # 会话超期未进清单自动过期

    # 连接池与调度器
    pool_idle_ttl_minutes: int = 30  # 连接池空闲超过该时长后回收
    scheduler_enabled: bool = True  # 进程内定时调度器（scheduled 任务）
    probe_l2_ttl_minutes: int = 30  # L2 结果时效，过期后批准前须重跑
    incremental_max_rows: int = 100000
    incremental_max_bytes: int = 50_000_000
    incremental_prepare_timeout: int = 120
    incremental_lease_seconds: int = 90
    incremental_max_attempts: int = 3

    # CORS 允许来源：逗号分隔；空则用开发默认（本机前端端口）
    cors_origins: str = ""

    # BI is opt-in; never changes synchronization or deletes its buffers.
    bi_enabled: bool = False
    bi_aggregation_enabled: bool = False
    bi_tick_interval_seconds: int = 60
    bi_query_timeout_seconds: int = 30
    bi_max_rows: int = 100000
    bi_cache_entries: int = 16
    bi_watermark_lag_warn_windows: int = 60
    # 明细保留清理参数：仅被 purge_expired_buffers() 使用。
    # BI 循环**默认不调用**该清理（DataBuffer 是轮次/租约/失败重试的工作集，
    # 按年龄删除会破坏在途轮次恢复）；需要时由运维显式调用或另接维护任务。
    bi_retention_days: int = 7
    bi_purge_batch_size: int = 5000
    bi_purge_max_batches: int = 100


settings = Settings()
