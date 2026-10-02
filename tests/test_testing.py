"""测试比对模块 (TestBridge) 单元测试"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from app.testing.comparator import DataComparator, CompareResult, FieldDiff
from app.testing.sender import DataSender, SendResult, BatchSendResult
from app.testing.receiver import DataReceiver, ReceiveResult, SnapshotRecord
from app.testing.stress import StressRunner, StressConfig, StressMetrics
from app.testing.reporter import ReportGenerator, TestReport
from app.testing.case_manager import case_manager, TestCase


# ============================================================
# 数据比对引擎测试
# ============================================================

class TestDataComparator:
    """数据比对引擎"""

    def test_exact_match(self):
        comp = DataComparator()
        result = comp.compare({"name": "张三", "age": 25}, {"name": "张三", "age": 25})
        assert result.matched is True
        assert result.match_rate == 1.0

    def test_value_mismatch(self):
        comp = DataComparator()
        result = comp.compare({"name": "张三"}, {"name": "李四"})
        assert result.matched is False
        assert len(result.diffs) == 1
        assert result.diffs[0].expected == "张三"
        assert result.diffs[0].actual == "李四"

    def test_missing_field_in_target(self):
        comp = DataComparator()
        result = comp.compare({"name": "张三", "age": 25}, {"name": "张三"})
        assert result.matched is False
        assert result.missing_fields == 1
        assert any("缺失" in d.reason for d in result.diffs)

    def test_extra_field_in_target(self):
        comp = DataComparator()
        result = comp.compare({"name": "张三"}, {"name": "张三", "extra": "x"})
        assert result.matched is False
        assert result.extra_fields == 1

    def test_with_field_mapping(self):
        comp = DataComparator()
        result = comp.compare(
            {"user_name": "张三", "user_age": 25},
            {"name": "张三", "age": 25},
            field_mapping={"user_name": "name", "user_age": "age"},
        )
        assert result.matched is True

    def test_type_compatible_str_to_int(self):
        """字符串数字 vs 整数应该兼容"""
        comp = DataComparator()
        result = comp.compare({"count": "100"}, {"count": 100})
        assert result.matched is True

    def test_type_compatible_int_to_str(self):
        comp = DataComparator()
        result = comp.compare({"count": 100}, {"count": "100"})
        assert result.matched is True

    def test_type_compatible_float_tolerance(self):
        comp = DataComparator(tolerance=0.01)
        result = comp.compare({"price": 3.14159}, {"price": 3.1416})
        assert result.matched is True  # 容差内

    def test_type_compatible_float_mismatch(self):
        comp = DataComparator(tolerance=1e-9)
        result = comp.compare({"price": 3.14}, {"price": 3.15})
        assert result.matched is False

    def test_both_null_values_match(self):
        comp = DataComparator()
        result = comp.compare({"x": None}, {"x": None})
        assert result.matched is True

    def test_str_bool_compatible(self):
        comp = DataComparator()
        result = comp.compare({"flag": "true"}, {"flag": True})
        assert result.matched is True
        result2 = comp.compare({"flag": "false"}, {"flag": False})
        assert result2.matched is False  # "false" != False

    def test_compare_batch_by_position(self):
        comp = DataComparator()
        sources = [{"name": "A"}, {"name": "B"}]
        targets = [{"name": "A"}, {"name": "C"}]
        results = comp.compare_batch(sources, targets)
        assert results[0].matched is True
        assert results[1].matched is False

    def test_compare_batch_by_key_field(self):
        comp = DataComparator()
        sources = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
        targets = [{"id": 2, "name": "B"}, {"id": 1, "name": "A"}]
        results = comp.compare_batch(sources, targets, key_field="id")
        assert all(r.matched for r in results)

    def test_compare_batch_missing_target(self):
        comp = DataComparator()
        sources = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
        targets = [{"id": 1, "name": "A"}]
        results = comp.compare_batch(sources, targets, key_field="id")
        assert results[1].matched is False
        assert "未找到匹配" in results[1].diffs[0].reason

    def test_compare_batch_targets_shorter(self):
        comp = DataComparator()
        sources = [{"name": "A"}, {"name": "B"}, {"name": "C"}]
        targets = [{"name": "A"}]
        results = comp.compare_batch(sources, targets)
        assert len(results) == 3
        assert results[0].matched is True
        assert results[1].matched is False
        assert "目标记录不足" in results[1].diffs[0].reason

    def test_summary_output(self):
        comp = DataComparator()
        result = comp.compare({"a": 1, "b": 2}, {"a": 1, "b": 3})
        summary = result.summary()
        assert "匹配: 否" in summary
        assert "不匹配: 1" in summary


# ============================================================
# 数据发送器测试
# ============================================================

class TestDataSender:
    """数据发送器"""

    def test_generate_data_with_index(self):
        sender = DataSender()
        data = sender.generate_data({"name": "user{{index}}", "type": "test"}, 3)
        assert len(data) == 3
        assert data[0]["name"] == "user0"
        assert data[1]["name"] == "user1"
        assert data[2]["name"] == "user2"

    def test_generate_data_with_timestamp(self):
        sender = DataSender()
        data = sender.generate_data({"ts": "{{timestamp}}"}, 1)
        assert data[0]["ts"].isdigit()

    def test_generate_data_with_uuid(self):
        sender = DataSender()
        data = sender.generate_data({"uid": "{{uuid}}"}, 2)
        assert data[0]["uid"] != data[1]["uid"]
        assert len(data[0]["uid"]) == 36  # UUID格式

    def test_generate_data_non_string_values(self):
        sender = DataSender()
        data = sender.generate_data({"name": "test", "count": 5}, 2)
        assert data[0]["name"] == "test"
        assert data[0]["count"] == 5  # 非字符串类型保持不变

    @pytest.mark.asyncio
    async def test_send_single_success(self):
        sender = DataSender()
        mock_fn = AsyncMock(return_value={"status": "ok"})
        result = await sender.send_single(mock_fn, {"name": "test"})
        assert result.success is True
        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_send_single_failure(self):
        sender = DataSender()
        mock_fn = AsyncMock(side_effect=Exception("连接失败"))
        result = await sender.send_single(mock_fn, {"name": "test"})
        assert result.success is False
        assert "连接失败" in result.error

    @pytest.mark.asyncio
    async def test_send_batch_concurrency(self):
        sender = DataSender()
        data_list = [{"name": f"user{i}"} for i in range(5)]
        mock_fn = AsyncMock(return_value={"status": "ok"})
        result = await sender.send_batch(mock_fn, data_list, concurrency=2)
        assert result.total == 5
        assert result.success == 5
        assert result.success_rate == 1.0

    @pytest.mark.asyncio
    async def test_send_batch_partial_failure(self):
        sender = DataSender()
        data_list = [{"name": f"user{i}"} for i in range(3)]
        call_count = 0

        async def flaky_fn(data=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("偶发失败")
            return {"status": "ok"}

        result = await sender.send_batch(flaky_fn, data_list, concurrency=1)
        assert result.success == 2
        assert result.failed == 1
        assert result.success_rate == 2 / 3

    def test_batch_result_latency_stats(self):
        """验证延迟统计正确"""
        result = BatchSendResult(
            total=3, success=3, failed=0,
            results=[
                SendResult(success=True, latency_ms=10, request_data=None),
                SendResult(success=True, latency_ms=20, request_data=None),
                SendResult(success=True, latency_ms=30, request_data=None),
            ],
        )
        assert result.min_latency_ms == 10
        assert result.max_latency_ms == 30
        assert result.avg_latency_ms == 20


# ============================================================
# 数据接收器测试
# ============================================================

class TestDataReceiver:
    """数据接收器"""

    @pytest.mark.asyncio
    async def test_poll_first_success(self):
        receiver = DataReceiver()
        mock_data = [{"id": 1}]
        result = await receiver.poll(AsyncMock(return_value=mock_data))
        assert result.success is True
        assert result.total_count == 1
        assert result.snapshots[0].data == {"id": 1}

    @pytest.mark.asyncio
    async def test_poll_empty_result(self):
        receiver = DataReceiver()
        mock_fn = AsyncMock(return_value=None)
        # 3次重试都返回None
        result = await receiver.poll(mock_fn, max_retries=3, interval_ms=50)
        assert result.success is True  # 无错误即为成功，只是没有数据
        assert result.total_count == 0

    @pytest.mark.asyncio
    async def test_poll_all_retries_fail(self):
        receiver = DataReceiver()
        mock_fn = AsyncMock(side_effect=Exception("网络错误"))
        result = await receiver.poll(mock_fn, max_retries=2, interval_ms=50)
        assert result.success is False
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_wait_for_data_success(self):
        receiver = DataReceiver()
        call_count = 0

        async def delayed_data():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                return [{"id": 1}]
            return []

        result = await receiver.wait_for_data(delayed_data, expected_count=1,
                                              timeout_ms=5000, poll_interval_ms=50)
        assert result.success is True
        assert not result.timeout

    @pytest.mark.asyncio
    async def test_wait_for_data_timeout(self):
        receiver = DataReceiver()
        mock_fn = AsyncMock(return_value=[])
        result = await receiver.wait_for_data(mock_fn, expected_count=5,
                                              timeout_ms=200, poll_interval_ms=50)
        assert result.success is False
        assert result.timeout is True
        assert "超时" in result.error

    @pytest.mark.asyncio
    async def test_poll_with_sync_function(self):
        """验证同步函数也能被正确处理"""
        receiver = DataReceiver()
        data = [{"id": 1}]
        result = await receiver.poll(lambda: data)
        assert result.success is True
        assert result.total_count == 1


# ============================================================
# 压测引擎测试
# ============================================================

class TestStressRunner:
    """压测引擎"""

    @pytest.mark.asyncio
    async def test_small_stress_run(self):
        runner = StressRunner()
        config = StressConfig(target_concurrency=2, duration_ms=500,
                              ramp_up_steps=1, ramp_up_interval_ms=100)
        async def mock_fn(data=None):
            return {"ok": True}

        metrics = await runner.run(mock_fn, config)
        assert metrics.total_requests > 0
        assert metrics.successful == metrics.total_requests
        assert metrics.success_rate == 1.0
        assert metrics.qps > 0

    @pytest.mark.asyncio
    async def test_stress_with_errors(self):
        runner = StressRunner()
        config = StressConfig(target_concurrency=2, duration_ms=300,
                              ramp_up_steps=1, ramp_up_interval_ms=50)
        async def flaky_fn(data=None):
            raise Exception("失败")
            return {"ok": True}  # never reached

        metrics = await runner.run(flaky_fn, config)
        assert metrics.failed > 0
        assert metrics.success_rate < 1.0

    @pytest.mark.asyncio
    async def test_percentile_calculation(self):
        runner = StressRunner()
        # 测试百分位数计算
        data = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        assert runner._percentile(data, 50) == 5.5  # median
        assert runner._percentile(data, 95) == 9.55  # P95
        assert runner._percentile([1], 50) == 1

    @pytest.mark.asyncio
    async def test_stress_with_ramp_up(self):
        runner = StressRunner()
        config = StressConfig(target_concurrency=4, duration_ms=300,
                              ramp_up_steps=2, ramp_up_interval_ms=100)
        async def mock_fn(data=None):
            return {"ok": True}

        metrics = await runner.run(mock_fn, config)
        assert metrics.successful > 0
        assert metrics.qps > 0

    def test_stress_metrics_summary(self):
        metrics = StressMetrics(total_requests=100, successful=95, failed=5,
                                min_latency_ms=1, max_latency_ms=100,
                                avg_latency_ms=10, p50_latency_ms=8,
                                p95_latency_ms=50, p99_latency_ms=90,
                                qps=200, elapsed_ms=500)
        s = metrics.summary()
        assert "95.00%" in s
        assert "QPS: 200.0" in s


# ============================================================
# 报告生成测试
# ============================================================

class TestReporter:
    """报告生成器"""

    def test_create_empty_report(self):
        report = ReportGenerator.create("测试报告")
        assert report.title == "测试报告"
        assert report.created_at != ""

    def test_html_report(self):
        comp = DataComparator()
        result = comp.compare({"name": "张三"}, {"name": "李四"})
        report = ReportGenerator.create("比对测试", compare_results=[result])
        html = report.to_html()
        assert "<title>比对测试</title>" in html
        assert "数据比对结果" in html
        assert "张三" in html
        assert "李四" in html

    def test_html_with_stress(self):
        metrics = StressMetrics(total_requests=100, successful=100, failed=0,
                                qps=200, avg_latency_ms=10, p50_latency_ms=8,
                                p95_latency_ms=30, p99_latency_ms=50,
                                min_latency_ms=1, max_latency_ms=100, elapsed_ms=500)
        report = ReportGenerator.create("压测报告", stress_metrics=metrics)
        html = report.to_html()
        assert "压测结果" in html
        assert "100" in html
        assert "200.0" in html

    def test_json_report(self):
        report = ReportGenerator.create("测试", errors=["错误1"])
        data = json.loads(report.to_json())
        assert data["title"] == "测试"
        assert "错误1" in data["errors"]

    def test_html_styling_present(self):
        report = ReportGenerator.create("x")
        html = report.to_html()
        assert "<style>" in html
        assert "pass" in html
        assert "fail" in html


# ============================================================
# 测试用例管理测试
# ============================================================

class TestCaseManager:
    """测试用例管理"""

    def setup_method(self):
        # 清空全局单例
        case_manager._cases.clear()

    def test_create_case(self):
        case = TestCase(name="测试用例1", group="登录", tags=["smoke"])
        case_manager.create(case)
        assert case_manager.count == 1

    def test_get_case(self):
        case = TestCase(name="测试用例1")
        case_manager.create(case)
        found = case_manager.get(case.id)
        assert found is not None
        assert found.name == "测试用例1"

    def test_update_case(self):
        case = TestCase(name="旧名称")
        case_manager.create(case)
        updated = case_manager.update(case.id, {"name": "新名称"})
        assert updated.name == "新名称"

    def test_delete_case(self):
        case = TestCase(name="用例")
        case_manager.create(case)
        assert case_manager.delete(case.id) is True
        assert case_manager.count == 0

    def test_list_cases(self):
        for i in range(3):
            case_manager.create(TestCase(name=f"用例{i}", group="A" if i < 2 else "B"))
        assert case_manager.count == 3
        assert len(case_manager.list(group="A")) == 2
        assert len(case_manager.list(group="B")) == 1

    def test_list_groups(self):
        case_manager.create(TestCase(name="a", group="登录"))
        case_manager.create(TestCase(name="b", group="支付"))
        groups = case_manager.list_groups()
        assert "登录" in groups
        assert "支付" in groups

    def test_filter_by_tag(self):
        case_manager.create(TestCase(name="a", tags=["smoke", "regression"]))
        case_manager.create(TestCase(name="b", tags=["regression"]))
        assert len(case_manager.list(tag="smoke")) == 1
        assert len(case_manager.list(tag="regression")) == 2

    def test_export_json(self):
        case_manager.create(TestCase(name="用例1", group="测试"))
        json_str = case_manager.export_json()
        data = json.loads(json_str)
        assert len(data) == 1
        assert data[0]["name"] == "用例1"

    def test_import_json(self):
        data = [
            {"name": "导入用例1", "group": "导入组",
             "request_data": {"key": "value"}},
            {"name": "导入用例2"},
        ]
        imported = case_manager.import_json(json.dumps(data))
        assert len(imported) == 2
        assert case_manager.count == 2
        # ID 应该被重新生成
        assert imported[0].id != imported[1].id

    def test_case_to_from_dict(self):
        case = TestCase(name="测试", group="grp", tags=["t1"],
                        request_data={"a": 1}, field_mapping={"src": "tgt"})
        d = case.to_dict()
        restored = TestCase.from_dict(d)
        assert restored.name == "测试"
        assert restored.group == "grp"
        assert restored.request_data == {"a": 1}


# ============================================================
# 端到端：发送→接收→比对
# ============================================================

class TestE2ESendReceiveCompare:
    """端到端：发送→接收→比对"""

    @pytest.mark.asyncio
    async def test_e2e_flow(self):
        """完整流程：构造数据→发送→接收→比对"""
        # 1. 构造测试数据
        sender = DataSender()
        template = {"name": "user-{{index}}", "email": "{{index}}@test.com"}
        test_data = sender.generate_data(template, 3)

        # 2. 模拟发送
        send_results = []
        for d in test_data:
            send_results.append(d)
        assert len(send_results) == 3

        # 3. 模拟接收（发送的数据直接作为接收快照）
        receiver = DataReceiver()
        snapshots = [SnapshotRecord(data=d, timestamp_ms=0) for d in test_data]

        # 4. 比对
        comp = DataComparator()
        results = comp.compare_batch(test_data, [s.data for s in snapshots])

        assert len(results) == 3
        assert all(r.matched for r in results)

    @pytest.mark.asyncio
    async def test_e2e_with_transform_and_compare(self):
        """模拟字段映射后的比对"""
        # 源端数据格式
        sources = [
            {"user_name": "张三", "user_id": "001"},
            {"user_name": "李四", "user_id": "002"},
        ]
        # 目标端数据格式（经过字段映射后）
        targets = [
            {"name": "张三", "uid": "001"},
            {"name": "李四", "uid": "002"},
        ]

        comp = DataComparator()
        mapping = {"user_name": "name", "user_id": "uid"}
        results = comp.compare_batch(sources, targets, field_mapping=mapping)

        assert len(results) == 2
        assert all(r.matched for r in results)

    @pytest.mark.asyncio
    async def test_e2e_report_generation(self):
        """端到端生成报告"""
        comp = DataComparator()
        result1 = comp.compare({"a": 1}, {"a": 1})
        result2 = comp.compare({"b": "x"}, {"b": "y"})

        metrics = StressMetrics(total_requests=100, successful=95, failed=5,
                                qps=200, avg_latency_ms=10, p50_latency_ms=8,
                                p95_latency_ms=30, p99_latency_ms=50,
                                min_latency_ms=1, max_latency_ms=100, elapsed_ms=500)

        report = ReportGenerator.create(
            "端到端测试报告",
            stress_metrics=metrics,
            compare_results=[result1, result2],
            summary={"测试环境": "开发", "测试数据量": "100条"},
        )

        json_str = report.to_json()
        data = json.loads(json_str)
        assert data["summary"]["测试数据量"] == "100条"
        assert data["stress"]["total_requests"] == 100
        assert data["compare"]["matched"] == 1
        assert data["compare"]["mismatched"] == 1

        html = report.to_html()
        assert "端到端测试报告" in html
