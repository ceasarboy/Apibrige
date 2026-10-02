"""Prometheus 指标采集模块

暴露 /metrics 端点供 Prometheus 抓取，包括：
- 同步任务计数器（成功/失败）
- 同步延迟直方图
- 队列深度
- HTTP 请求指标
- 进程指标
"""
import time
import os
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CollectorRegistry

registry = CollectorRegistry()

# 同一种指标
sync_counter = Counter(
    "apibridge_sync_total",
    "同步任务总数",
    ["status", "task_type"],
    registry=registry,
)

sync_duration = Histogram(
    "apibridge_sync_duration_seconds",
    "同步任务耗时（秒）",
    ["task_type"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
    registry=registry,
)

# 数据缓冲队列（原指标名，带 queue_name 标签；与 apibridge_mq_queue_depth 同值同步导出）
queue_depth = Gauge(
    "apibridge_queue_depth",
    "RabbitMQ 任务队列就绪消息数（不可达时不导出该序列）",
    ["queue_name"],
    registry=registry,
)

# 活跃任务数
active_tasks = Gauge(
    "apibridge_active_tasks",
    "活跃任务数",
    registry=registry,
)

# 连接数
active_connections = Gauge(
    "apibridge_active_connections",
    "活跃连接数",
    registry=registry,
)

# HTTP 请求指标
http_requests = Counter(
    "apibridge_http_requests_total",
    "HTTP 请求总数",
    ["method", "endpoint", "status"],
    registry=registry,
)

http_duration = Histogram(
    "apibridge_http_duration_seconds",
    "HTTP 请求耗时（秒）",
    ["method", "endpoint"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0],
    registry=registry,
)

# 数据库连接池
db_pool_connections = Gauge(
    "apibridge_db_pool_connections",
    "数据库连接池使用数",
    registry=registry,
)

db_pool_size = Gauge(
    "apibridge_db_pool_size",
    "数据库连接池大小",
    registry=registry,
)

# API 调用指标
api_calls = Counter(
    "apibridge_api_calls_total",
    "外部 API 调用总数",
    ["target", "status"],
    registry=registry,
)

api_call_duration = Histogram(
    "apibridge_api_call_duration_seconds",
    "外部 API 调用耗时（秒）",
    ["target"],
    buckets=[0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0],
    registry=registry,
)

# 测试模块指标
test_runs = Counter(
    "apibridge_test_runs_total",
    "测试运行总数",
    ["status"],
    registry=registry,
)

stress_qps = Gauge(
    "apibridge_stress_qps",
    "压测 QPS",
    registry=registry,
)


def record_api_call(target: str, status: str, duration_seconds: float) -> None:
    """上报一次外部 API 调用（task 7.2：探针与运行时统一走此入口）

    标签只保留 scheme://host:port——完整 base_url 会把路径/查询串带进
    Prometheus 标签（基数膨胀；查询串鉴权如 HAP v1 的 sign 会泄漏进 /metrics）。
    """
    from urllib.parse import urlsplit
    try:
        u = urlsplit(str(target or ""))
        label = f"{u.scheme}://{u.netloc}" if u.netloc else str(target or "unknown")
    except ValueError:
        label = "unknown"
    api_calls.labels(target=label, status=status).inc()
    api_call_duration.labels(target=label).observe(duration_seconds)


def record_sync(status: str, task_type: str, duration_seconds: float) -> None:
    """上报一次同步任务结果（task 7.1）"""
    sync_counter.labels(status=status, task_type=task_type).inc()
    sync_duration.labels(task_type=task_type).observe(duration_seconds)


def get_metrics():
    """获取 Prometheus 格式指标"""
    return generate_latest(registry)

# Separate names preserve the existing unlabeled queue_depth metric contract.
buffer_pending = Gauge("apibridge_buffer_pending", "Pending/processing buffer batches", registry=registry)
mq_queue_depth = Gauge("apibridge_mq_queue_depth", "Ready RabbitMQ messages", ["queue_name"], registry=registry)
