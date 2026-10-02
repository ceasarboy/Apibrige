"""测试模块 API 路由"""
import asyncio
from uuid import UUID
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.testing.sender import DataSender
from app.testing.receiver import DataReceiver
from app.testing.comparator import DataComparator
from app.testing.stress import StressRunner, StressConfig
from app.testing.reporter import ReportGenerator
from app.testing.case_manager import case_manager, TestCase

router = APIRouter(prefix="/testing", tags=["testing"])

# ============================================================
# 单例
# ============================================================
sender = DataSender()
receiver = DataReceiver()
comparator = DataComparator()
stress_runner = StressRunner()


# ============================================================
# 辅助函数
# ============================================================
async def _mock_fetch_fn(data=None):
    """模拟数据拉取/推送（用于演示，实际使用时会替换为真实 API 调用）"""
    await asyncio.sleep(0.01)
    return data or {"status": "ok"}


# ============================================================
# 测试用例管理
# ============================================================

class CaseCreate(BaseModel):
    name: str
    description: str = ""
    group: str = ""
    tags: list[str] = []
    source_connection_id: str = ""
    source_endpoint: str = ""
    request_data: dict = {}
    target_connection_id: str = ""
    target_endpoint: str = ""
    field_mapping: dict[str, str] = {}
    key_field: str = ""
    concurrency: int = 1
    duration_ms: int = 5000
    ramp_up: bool = False


class CaseUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    group: str | None = None
    tags: list[str] | None = None
    request_data: dict | None = None
    field_mapping: dict[str, str] | None = None
    key_field: str | None = None
    concurrency: int | None = None
    duration_ms: int | None = None
    ramp_up: bool | None = None


@router.post("/cases", summary="创建测试用例")
async def create_case(body: CaseCreate):
    case = case_manager.create(TestCase(**body.model_dump()))
    return case.to_dict()


@router.get("/cases", summary="查询测试用例列表")
async def list_cases(group: str | None = None, tag: str | None = None):
    return [c.to_dict() for c in case_manager.list(group=group, tag=tag)]


@router.get("/cases/groups", summary="获取用例分组列表")
async def list_groups():
    return case_manager.list_groups()


@router.get("/cases/export", summary="导出测试用例（JSON）")
async def export_cases(group: str | None = None):
    return case_manager.export_json(group=group)


@router.post("/cases/import", summary="导入测试用例（JSON）")
async def import_cases(body: str):
    cases = case_manager.import_json(body)
    return {"imported": len(cases), "cases": [c.to_dict() for c in cases]}


# ============================================================
# 数据发送
# ============================================================

class SendRequest(BaseModel):
    template: dict = {}
    count: int = 1
    concurrency: int = 1


@router.get("/cases/{case_id}", summary="获取测试用例详情")
async def get_case(case_id: str):
    case = case_manager.get(case_id)
    if case is None:
        raise HTTPException(404, "测试用例不存在")
    return case.to_dict()


@router.put("/cases/{case_id}", summary="更新测试用例")
async def update_case(case_id: str, body: CaseUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    case = case_manager.update(case_id, updates)
    if case is None:
        raise HTTPException(404, "测试用例不存在")
    return case.to_dict()


@router.delete("/cases/{case_id}", summary="删除测试用例")
async def delete_case(case_id: str):
    if not case_manager.delete(case_id):
        raise HTTPException(404, "测试用例不存在")
    return {"message": "已删除"}


@router.post("/send", summary="批量发送测试数据")
async def send_data(body: SendRequest):
    data_list = sender.generate_data(body.template, body.count)
    result = await sender.send_batch(_mock_fetch_fn, data_list, body.concurrency)
    return {
        "total": result.total,
        "success": result.success,
        "failed": result.failed,
        "success_rate": result.success_rate,
        "avg_latency_ms": result.avg_latency_ms,
        "min_latency_ms": result.min_latency_ms,
        "max_latency_ms": result.max_latency_ms,
    }


# ============================================================
# 压测
# ============================================================

class StressRequest(BaseModel):
    concurrency: int = 10
    duration_ms: int = 5000
    ramp_up: bool = True
    ramp_up_steps: int = 3


@router.post("/stress", summary="执行压测")
async def run_stress(body: StressRequest):
    config = StressConfig(
        target_concurrency=body.concurrency,
        duration_ms=body.duration_ms,
        ramp_up_steps=body.ramp_up_steps if body.ramp_up else 1,
    )
    metrics = await stress_runner.run(_mock_fetch_fn, config)
    return {
        "total_requests": metrics.total_requests,
        "successful": metrics.successful,
        "failed": metrics.failed,
        "success_rate": metrics.success_rate,
        "qps": metrics.qps,
        "avg_latency_ms": metrics.avg_latency_ms,
        "p50_latency_ms": metrics.p50_latency_ms,
        "p95_latency_ms": metrics.p95_latency_ms,
        "p99_latency_ms": metrics.p99_latency_ms,
        "elapsed_ms": metrics.elapsed_ms,
    }


# ============================================================
# 数据比对
# ============================================================

class CompareRequest(BaseModel):
    source_data: list[dict]
    target_data: list[dict]
    field_mapping: dict[str, str] | None = None
    key_field: str | None = None


@router.post("/compare", summary="数据比对")
async def compare_data(body: CompareRequest):
    results = comparator.compare_batch(
        body.source_data, body.target_data,
        field_mapping=body.field_mapping,
        key_field=body.key_field,
    )
    return {
        "total": len(results),
        "matched": sum(1 for r in results if r.matched),
        "mismatched": sum(1 for r in results if not r.matched),
        "details": [
            {
                "matched": r.matched,
                "match_rate": r.match_rate,
                "summary": r.summary(),
                "diffs": [{"field": d.field, "expected": str(d.expected),
                           "actual": str(d.actual), "reason": d.reason}
                          for d in r.diffs],
            }
            for r in results
        ],
    }


# ============================================================
# 报告
# ============================================================

@router.get("/report/html", summary="生成 HTML 测试报告")
async def html_report(title: str = "测试报告"):
    report = ReportGenerator.create(title=title)
    return report.to_html()


@router.get("/report/json", summary="生成 JSON 测试报告")
async def json_report(title: str = "测试报告"):
    report = ReportGenerator.create(title=title)
    return report.to_json()
