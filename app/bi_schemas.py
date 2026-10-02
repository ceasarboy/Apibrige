"""BI and operational monitoring DTOs."""
from pydantic import BaseModel

class BiMetricDefOut(BaseModel):
    """聚合指标定义（响应）"""
    metric_key: str
    name: str
    description: str | None = None
    source_type: str
    source_ref: str | None = None
    time_field: str = "created_at"
    granularity: str
    dimensions: list[dict] = []
    measures: list[dict] = []
    filters: dict | list | None = None
    chart_type: str
    top_n: int
    enabled: bool
    style: dict | None = None
    computed: list[dict] = []
    display: dict = {}
    builtin: bool = False

class BiMetricDefIn(BaseModel):
    """聚合指标定义（创建/更新请求）"""
    metric_key: str | None = None
    name: str = ""
    description: str | None = None
    source_type: str = ""
    source_ref: str | None = None
    time_field: str = "created_at"
    granularity: str = "1min"
    dimensions: list[dict] = []
    measures: list[dict] = []
    filters: dict | list | None = None
    chart_type: str = "line"
    top_n: int = 100
    enabled: bool = True
    style: dict | None = None
    computed: list[dict] = []
    display: dict = {}

class BiDimensionOut(BaseModel):
    """候选维度与基数分级"""
    name: str
    code: str
    cardinality: str
    usable_for_group: bool
    hint: str

class BiAdhocQueryIn(BaseModel):
    """即席分析查询请求（add-bi-adhoc-analysis）

    字段与指标定义同形（dimensions/measures/filters/computed/chart_type），
    另加分析页专用项：`sort`（排序，不写进定义）与 `format`（输出格式，缺省按图表类型）。
    """
    connection_id: str
    form_key: str
    chart_type: str = "bar"
    dimensions: list[dict] = []
    measures: list[dict] = []
    filters: dict | list | None = None
    computed: list[dict] = []
    top_n: int = 100
    sort: dict | None = None
    format: str | None = None
    metric_key: str | None = None
    name: str | None = None
    style: dict | None = None

class TaskMetricItem(BaseModel):
    """按任务维度的执行指标"""
    task_id: str
    task_name: str
    success: int
    failed: int
    last_run_at: str | None = None
    avg_duration_ms: float | None = None
    buffered_batches: int = 0

class ConnectionMetricItem(BaseModel):
    """按连接维度的调用指标（经 field_mappings 关联推导）"""
    connection_id: str
    connection_name: str
    side: str          # source | target
    success_rate: float
    avg_latency_ms: float | None = None
    failed: int = 0

class ErrorMetricItem(BaseModel):
    """归一化错误分组"""
    error_type: str
    count: int
    last_seen: str | None = None
    samples: list[str] = []

class TaskRunItem(BaseModel):
    """单任务一次执行明细"""
    created_at: str | None = None
    action: str | None = None
    status: str | None = None
    duration_ms: int | None = None
    error_message: str | None = None
