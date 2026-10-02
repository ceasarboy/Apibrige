# -*- coding: utf-8 -*-
"""运维可定位指标端点测试（tasks 4.1 – 4.6）

覆盖：
- /metrics/tasks        按任务聚合（窗口生效、无记录任务不报错、搬运行数）
- /metrics/connections  经 field_mappings 推导，源/目标两端分别统计
- /metrics/errors       归一化 Top N（排序、limit、样本上限 50）
- /metrics/tasks/{id}/runs  最近执行明细 + 404
- 脱敏复核：不含 enc: / token
- 高基数防护：task_id / connection_id 不进 Prometheus 标签
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import get_session
from app.main import app
from app.models.sync import (
    ApiConnection, Base, DataBuffer, ExecutionLog, FieldMapping, SyncTask,
)
from tests.conftest import TEST_ADMIN_TOKEN

UTC = timezone.utc
NOW = datetime.now(UTC)


def _dt(minutes_ago: int) -> datetime:
    return NOW - timedelta(minutes=minutes_ago)


@pytest.fixture
def obs_api(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/obs.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_session():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    client = TestClient(app, headers={"X-Admin-Token": TEST_ADMIN_TOKEN})
    yield client, maker
    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


async def _seed_topology(maker):
    """建：源连接 MES / 目标连接 明道云 / 映射 / 两个任务

    返回 (task_ok_id, task_bad_id, src_id, tgt_id, idle_task_id)
    """
    src_id, tgt_id, map_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    task_ok, task_bad, task_idle = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with maker() as s:
        s.add(ApiConnection(id=src_id, name="MES库", connection_type="source",
                            protocol="oracle", db_type="oracle", status="active"))
        s.add(ApiConnection(id=tgt_id, name="明道云", connection_type="target",
                            protocol="hap", status="active"))
        s.add(FieldMapping(id=map_id, name="MES→明道云",
                           source_connection_id=src_id,
                           target_connection_id=tgt_id, mapping_config={}))
        for tid in (task_ok, task_bad, task_idle):
            s.add(SyncTask(id=tid, mapping_id=map_id, task_type="manual",
                           status="active"))
        await s.commit()
    return task_ok, task_bad, src_id, tgt_id, task_idle


# ============================================================
# 4.1 按任务维度
# ============================================================

class TestTaskMetrics:

    def test_aggregates_and_includes_idle_tasks(self, obs_api):
        client, maker = obs_api
        ok, bad, _, _, idle = asyncio.run(_seed_topology(maker))

        async def _logs():
            async with maker() as s:
                s.add_all([
                    ExecutionLog(task_id=ok, action="push", status="success",
                                 duration_ms=100, created_at=_dt(10)),
                    ExecutionLog(task_id=ok, action="push", status="success",
                                 duration_ms=300, created_at=_dt(5)),
                    ExecutionLog(task_id=bad, action="push", status="failed",
                                 duration_ms=900, error_message="连接超时 timeout 30s",
                                 created_at=_dt(3)),
                    # 窗口外（2 天前）不应计入 1h 窗口
                    ExecutionLog(task_id=ok, action="push", status="failed",
                                 duration_ms=999, created_at=_dt(60 * 48)),
                ])
                s.add_all([
                    DataBuffer(task_id=ok, source_data={}, status="completed",
                               created_at=_dt(8)),
                    DataBuffer(task_id=ok, source_data={}, status="completed",
                               created_at=_dt(7)),
                ])
                await s.commit()
        asyncio.run(_logs())

        r = client.get("/api/v1/metrics/tasks", params={"period": "24h"})
        assert r.status_code == 200
        by = {i["task_id"]: i for i in r.json()}

        assert by[str(ok)]["success"] == 2
        assert by[str(ok)]["failed"] == 0, "窗口外的失败不应计入"
        assert by[str(ok)]["avg_duration_ms"] == 200
        # 合并后字段名为 buffered_batches（口径澄清：DataBuffer 批次数，不是业务行数）
        assert by[str(ok)]["buffered_batches"] == 2
        assert by[str(ok)]["task_name"] == "MES→明道云"
        assert by[str(bad)]["failed"] == 1
        # 无执行记录的任务也必须出现，且计数为 0（不报错、不缺失）
        assert str(idle) in by
        assert by[str(idle)]["success"] == 0 and by[str(idle)]["failed"] == 0
        assert by[str(idle)]["last_run_at"] is None

    def test_window_narrows_results(self, obs_api):
        client, maker = obs_api
        ok, _, _, _, _ = asyncio.run(_seed_topology(maker))

        async def _logs():
            async with maker() as s:
                s.add(ExecutionLog(task_id=ok, action="push", status="success",
                                   duration_ms=10, created_at=_dt(3 * 60)))  # 3 小时前
                await s.commit()
        asyncio.run(_logs())

        assert client.get("/api/v1/metrics/tasks",
                          params={"period": "1h"}).json()[0]["success"] == 0
        assert client.get("/api/v1/metrics/tasks",
                          params={"period": "6h"}).json()[0]["success"] == 1

    def test_failed_sorted_first(self, obs_api):
        client, maker = obs_api
        ok, bad, _, _, _ = asyncio.run(_seed_topology(maker))

        async def _logs():
            async with maker() as s:
                s.add_all([
                    ExecutionLog(task_id=ok, action="push", status="success",
                                 duration_ms=10, created_at=_dt(5)),
                    ExecutionLog(task_id=bad, action="push", status="failed",
                                 duration_ms=10, error_message="x", created_at=_dt(4)),
                ])
                await s.commit()
        asyncio.run(_logs())

        items = client.get("/api/v1/metrics/tasks").json()
        assert items[0]["task_id"] == str(bad), "失败多的任务应排在前面"


# ============================================================
# 4.2 按连接维度（经映射推导，两端分别统计）
# ============================================================

class TestConnectionMetrics:

    def test_both_sides_counted_once(self, obs_api):
        client, maker = obs_api
        ok, _, src, tgt, _ = asyncio.run(_seed_topology(maker))

        async def _logs():
            async with maker() as s:
                s.add_all([
                    ExecutionLog(task_id=ok, action="push", status="success",
                                 duration_ms=100, created_at=_dt(5)),
                    ExecutionLog(task_id=ok, action="push", status="failed",
                                 duration_ms=300, error_message="x", created_at=_dt(4)),
                ])
                await s.commit()
        asyncio.run(_logs())

        items = client.get("/api/v1/metrics/connections").json()
        by = {(i["connection_id"], i["side"]): i for i in items}
        assert set(by) == {(str(src), "source"), (str(tgt), "target")}
        for key in by:
            assert by[key]["success_rate"] == 50.0, "一次成功一次失败 → 50%"
            assert by[key]["failed"] == 1
            assert by[key]["avg_latency_ms"] == 200
        assert by[(str(src), "source")]["connection_name"] == "MES库"
        assert by[(str(tgt), "target")]["connection_name"] == "明道云"

    def test_empty_when_no_logs(self, obs_api):
        client, maker = obs_api
        asyncio.run(_seed_topology(maker))
        assert client.get("/api/v1/metrics/connections").json() == []


# ============================================================
# 4.3 错误 Top N（归一化）
# ============================================================

class TestErrorMetrics:

    def test_normalized_grouping_and_ordering(self, obs_api):
        client, maker = obs_api
        ok, _, _, _, _ = asyncio.run(_seed_topology(maker))

        async def _logs():
            async with maker() as s:
                # 同一形态不同数字 → 归一化为同一组（3 条）
                for i, secs in enumerate((30, 45, 60)):
                    s.add(ExecutionLog(task_id=ok, action="push", status="failed",
                                       duration_ms=10, created_at=_dt(10 - i),
                                       error_message=f"timeout after {secs}s"))
                s.add(ExecutionLog(task_id=ok, action="push", status="failed",
                                   duration_ms=10, created_at=_dt(1),
                                   error_message="认证失败 invalid token"))
                # 成功记录不应计入错误聚合
                s.add(ExecutionLog(task_id=ok, action="push", status="success",
                                   duration_ms=10, created_at=_dt(1),
                                   error_message=None))
                await s.commit()
        asyncio.run(_logs())

        items = client.get("/api/v1/metrics/errors").json()
        assert items[0]["count"] == 3, f"同形态错误应归一为一组: {items}"
        assert "timeout" in items[0]["error_type"]
        assert len(items[0]["samples"]) == 3
        assert items[1]["count"] == 1

    def test_limit_applied(self, obs_api):
        client, maker = obs_api
        ok, _, _, _, _ = asyncio.run(_seed_topology(maker))
        # 用形态**互不相同**的错误（带可变数字会被归一化成同一组）
        distinct = ["认证失败", "连接超时", "数据格式错误", "权限不足", "目标不可达"]

        async def _logs():
            async with maker() as s:
                for i, msg in enumerate(distinct):
                    s.add(ExecutionLog(task_id=ok, action="push", status="failed",
                                       duration_ms=1, created_at=_dt(i + 1),
                                       error_message=msg))
                await s.commit()
        asyncio.run(_logs())

        assert len(client.get("/api/v1/metrics/errors").json()) == 5
        assert len(client.get("/api/v1/metrics/errors",
                              params={"limit": 2}).json()) == 2

    def test_samples_capped_at_50(self, obs_api):
        client, maker = obs_api
        ok, _, _, _, _ = asyncio.run(_seed_topology(maker))

        async def _logs():
            async with maker() as s:
                for i in range(60):
                    s.add(ExecutionLog(task_id=ok, action="push", status="failed",
                                       duration_ms=1, created_at=_dt(i + 1),
                                       error_message=f"unique-msg-{i}"))
                await s.commit()
        asyncio.run(_logs())

        items = client.get("/api/v1/metrics/errors").json()
        assert items[0]["count"] == 60
        assert len(items[0]["samples"]) <= 50, "原样本上限 50"


# ============================================================
# 4.4 单任务执行明细
# ============================================================

class TestTaskRuns:

    def test_returns_recent_runs_desc(self, obs_api):
        client, maker = obs_api
        ok, _, _, _, _ = asyncio.run(_seed_topology(maker))

        async def _logs():
            async with maker() as s:
                for i in range(3):
                    s.add(ExecutionLog(task_id=ok, action="push", status="success",
                                       duration_ms=100 + i, created_at=_dt(10 - i)))
                await s.commit()
        asyncio.run(_logs())

        runs = client.get(f"/api/v1/metrics/tasks/{ok}/runs").json()
        assert len(runs) == 3
        # 最新在前
        assert runs[0]["created_at"] > runs[-1]["created_at"]

    def test_limit_applied(self, obs_api):
        client, maker = obs_api
        ok, _, _, _, _ = asyncio.run(_seed_topology(maker))

        async def _logs():
            async with maker() as s:
                for i in range(5):
                    s.add(ExecutionLog(task_id=ok, action="push", status="success",
                                       duration_ms=1, created_at=_dt(i + 1)))
                await s.commit()
        asyncio.run(_logs())

        assert len(client.get(f"/api/v1/metrics/tasks/{ok}/runs",
                              params={"limit": 2}).json()) == 2

    def test_unknown_task_404(self, obs_api):
        client, _ = obs_api
        r = client.get(f"/api/v1/metrics/tasks/{uuid.uuid4()}/runs")
        assert r.status_code == 404


# ============================================================
# 4.5 脱敏复核
# ============================================================

class TestDesensitization:

    def test_no_credential_leak(self, obs_api):
        client, maker = obs_api
        ok, _, _, _, _ = asyncio.run(_seed_topology(maker))

        async def _logs():
            async with maker() as s:
                s.add(ExecutionLog(task_id=ok, action="push", status="failed",
                                   duration_ms=1, created_at=_dt(1),
                                   error_message="auth failed"))
                await s.commit()
        asyncio.run(_logs())

        for path in ("/api/v1/metrics/tasks", "/api/v1/metrics/connections",
                     "/api/v1/metrics/errors"):
            text = client.get(path).text.lower()
            assert "enc:" not in text, f"{path} 泄漏了加密前缀"
            assert "password" not in text, f"{path} 出现 password 字样"
            assert "secret" not in text, f"{path} 出现 secret 字样"


# ============================================================
# 4.6 高基数防护
# ============================================================

class TestHighCardinalityGuard:

    def test_no_high_cardinality_labels_in_prometheus(self):
        """task_id / connection_id 等高基数维度只走 REST，不进 Prometheus 标签"""
        from app.metrics_collector import generate_latest, registry

        text = generate_latest(registry).decode("utf-8")
        for forbidden in ("task_id=", "connection_id=", "metric_key=", "error_type="):
            assert forbidden not in text, f"Prometheus 指标不应含高基数标签 {forbidden}"
