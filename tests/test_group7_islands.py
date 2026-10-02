"""组7 孤岛回收验证（task 7.1-7.7）"""
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.conflict import compute_record_hash, detect_hash_conflict


@pytest.fixture(autouse=True)
def _stub_redis_side_channels(monkeypatch):
    """合并后 sync_transfer 会走 Redis 侧通道（幂等检查/限流/完成标记）。

    本文件的用例只关心冲突跳过与指标上报，统一打桩——否则要依赖本机 Redis 可用
    且版本足够新（旧版 Redis 不支持 HELLO 握手会抛 ResponseError，
    表现为"与被测行为无关"的 failed）。
    """
    import app.services.sync_handler as sh

    monkeypatch.setattr(sh, "is_duplicate", AsyncMock(return_value=False))
    monkeypatch.setattr(sh, "rate_limit", AsyncMock(return_value=True))
    monkeypatch.setattr(sh, "mark_processed", AsyncMock())


# ============================================================
# task 7.3：alerts.yml 引用的指标均已声明（可求值）
# ============================================================

def test_alert_rules_reference_declared_metrics():
    from app import metrics_collector as mc

    declared = set()
    for name in dir(mc):
        obj = getattr(mc, name)
        if hasattr(obj, "_name"):
            declared.add(obj._name)
            declared.add(obj._name + "_total")  # Counter 暴露序列带 _total
    text = Path("configs/alerts.yml").read_text(encoding="utf-8")
    used = set(re.findall(r"apibridge_\w+", text))
    # histogram 的 _bucket/_count/_sum 序列归一到基础指标名
    bases = {u[:-7] if u.endswith("_bucket") else u for u in used}
    unknown = {u for u in bases
               if u not in declared and not u.endswith(("_count", "_sum"))}
    assert not unknown, f"alerts.yml 引用了未声明指标: {unknown}"
    # 哑火的两条核心规则所需指标必须在列
    assert "apibridge_sync_total" in used
    assert "apibridge_sync_duration_seconds" in bases


# ============================================================
# task 7.1/7.2：同步与 API 调用指标上报
# ============================================================

class TestMetricsReporting:
    @pytest.mark.asyncio
    async def test_sync_reports_metrics(self, mock_redis, mock_api_source,
                                        mock_api_target, mapping_config):
        """task 7.1：跑一次同步后 sync 计数器 +1"""
        from app.metrics_collector import sync_counter
        from app.services.sync_handler import sync_transfer

        before = sync_counter.labels(
            status="success", task_type="realtime")._value.get()

        task = MagicMock(id=uuid4(), fallback_to_async=False, timeout_ms=30000,
                         conflict_strategy=None, task_type="realtime")
        mapping = MagicMock(source_endpoint="/users", target_endpoint="/c",
                            source_headers=None, target_headers=None,
                            mapping_config=mapping_config)
        source_conn = MagicMock(db_type=None, auth_type=None, auth_config=None,
                                base_url="https://s", headers={}, timeout_ms=30000)
        target_conn = MagicMock(id=uuid4(), db_type=None, auth_type=None,
                                auth_config=None, base_url="https://t",
                                headers={}, timeout_ms=30000)
        session = AsyncMock()
        buffer_obj = MagicMock()
        buffer_obj.id = uuid4()
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))
        with patch("app.services.sync_handler.DataBuffer") as MockBuffer:
            MockBuffer.return_value = buffer_obj
            result = await sync_transfer(session, task, mapping, source_conn, target_conn)

        assert result["status"] == "success"
        after = sync_counter.labels(
            status="success", task_type="realtime")._value.get()
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_probe_reports_api_calls(self, monkeypatch):
        """task 7.2：探针 L1 触发后 api_calls 增长"""
        from app.config import settings
        from app.metrics_collector import api_calls
        from app.probe.prober import Side, run_l1

        monkeypatch.setattr(settings, "probe_allowed_hosts", "")
        before = api_calls.labels(target="https://x.example.com",
                                  status="ok")._value.get()
        conn = MagicMock(protocol="rest", base_url="https://x.example.com",
                         auth_type=None, auth_config=None, headers={},
                         timeout_ms=30000, retry_count=3, db_type=None, db_config=None)
        side = Side(conn=conn, endpoint="/u")
        with patch("app.probe.prober.ProtocolAdapterFactory") as factory:
            adapter = MagicMock()
            adapter.test_connection = AsyncMock(
                return_value={"status": "ok", "latency_ms": 3, "error": None})
            factory.create = MagicMock(return_value=adapter)
            await run_l1(side, side)
        after = api_calls.labels(target="https://x.example.com", status="ok")._value.get()
        assert after == before + 2  # 源 + 目标各一次


# ============================================================
# task 7.4：运行时 DB 拉取走 batch_fetch 分页
# ============================================================

@pytest.mark.asyncio
async def test_fetch_from_db_uses_pagination():
    """task 7.4：1000 行源表按 batch 分页拉取（多次 LIMIT/OFFSET 查询）"""
    pages_yielded = []

    class FakeConnector:
        async def fetch_data(self, table, offset=0, limit=None,
                             order_by=None, where_gt=None):
            pages_yielded.append((offset, limit))
            # 模拟 1000 行、每页 400：3 页（400/400/200）
            start = offset
            if start >= 1000:
                return []
            return [{"id": i} for i in range(start, min(start + limit, 1000))]

        async def disconnect(self):
            pass

    from app.services import db_client

    with patch("app.services.db_client.get_database_connector",
               return_value=FakeConnector()):
        data = await db_client.fetch_from_db("postgresql", {}, "t", batch_size=400)

    assert len(data) == 1000
    assert pages_yielded == [(0, 400), (400, 400), (800, 400)]


# ============================================================
# task 7.5：WriteBuffer 已删除
# ============================================================

def test_write_buffer_removed():
    from app.connectors import batch

    assert not hasattr(batch, "WriteBuffer")
    # 全仓无引用
    for py in Path("app").rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        if "WriteBuffer" in text:
            pytest.fail(f"残留 WriteBuffer 引用: {py}")


# ============================================================
# task 7.7：skip 冲突策略真实生效
# ============================================================

class TestConflictSkip:
    def test_detect_hash_conflict_skips_known(self):
        records = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
        known = {compute_record_hash(records[0], "id")}
        to_push, count = detect_hash_conflict(records, known, strategy="skip")
        assert count == 1
        assert to_push == [{"id": 2, "name": "b"}]

    @pytest.mark.asyncio
    async def test_sync_all_conflict_skips_push(self):
        """task 7.7：全部命中已知哈希时不推送，状态 skipped，日志可见跳过条数"""
        from app.services.sync_handler import sync_transfer

        records = [{"user_name": "张三", "age": 25}]
        known_hash = compute_record_hash({"customer_name": "张三"})

        task = MagicMock(id=uuid4(), fallback_to_async=False, timeout_ms=30000,
                         conflict_strategy="skip", task_type="realtime")
        mapping = MagicMock(source_endpoint="/users", target_endpoint="/c",
                            source_headers=None, target_headers=None,
                            mapping_config={"fields": [
                                {"source": "user_name", "target": "customer_name",
                                 "transform": "string"}]})
        source_conn = MagicMock(db_type=None, auth_type=None, auth_config=None,
                                base_url="https://s", headers={}, timeout_ms=30000)
        target_conn = MagicMock(id=uuid4(), db_type=None, auth_type=None,
                                auth_config=None, base_url="https://t",
                                headers={}, timeout_ms=30000)
        session = AsyncMock()
        buffer_obj = MagicMock()
        buffer_obj.id = uuid4()

        # 锁与幂等走通；冲突查询（含 data_hash 列）返回已知哈希
        empty_rows = MagicMock()
        empty_rows.scalars.return_value.all.return_value = []
        hash_rows = MagicMock()
        hash_rows.scalars.return_value.all.return_value = [known_hash]
        log_rows = MagicMock()

        async def _execute(stmt, *a, **k):
            return hash_rows if "data_hash" in str(stmt) else empty_rows

        session.execute = AsyncMock(side_effect=_execute)
        adapter = MagicMock()
        adapter.fetch_data = AsyncMock(return_value=records[0])  # dict 单条（运行时对 dict 不包 data 键）
        adapter.push_data = AsyncMock()
        factory = MagicMock()
        factory.create = MagicMock(return_value=adapter)

        from app.models.sync import DataBuffer as RealDataBuffer

        class FakeDataBuffer(RealDataBuffer):
            def __init__(self, **kwargs):
                self.id = uuid4()
                for k, v in kwargs.items():
                    setattr(self, k, v)

        with patch("app.services.sync_handler.ProtocolAdapterFactory", factory), \
             patch("app.services.sync_handler.DataBuffer", FakeDataBuffer), \
             patch("app.services.sync_handler.lock") as mock_lock:
            mock_lock.acquire_lock = AsyncMock(return_value="tok")
            mock_lock.release_lock = AsyncMock()
            with patch("app.services.sync_handler.log_execution", new=AsyncMock()) as log_exec:
                result = await sync_transfer(session, task, mapping,
                                             source_conn, target_conn)

        assert result["status"] == "skipped"
        assert result["skipped"] == 1
        adapter.push_data.assert_not_called()  # skip 策略下不推送
        assert log_exec.call_count >= 1  # 跳过条数已记录
