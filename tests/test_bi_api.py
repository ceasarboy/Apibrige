# -*- coding: utf-8 -*-
"""BI API 测试（tasks 4.1 / 4.2 / 4.3 / 4.6 / 4.8 / 4.9）

覆盖：
- 候选维度端点（8 项 + 基数分级）
- 指标定义列表 / 创建（校验、409）/ 更新 / 删除
- 聚合结果查询（时间范围、维度过滤、格式、404、非法入参）
- 手动触发与回填
- 响应脱敏复核
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import get_session
from app.main import app
from app.models.sync import Base, DataBuffer
from app.services.bi_defs import BUILTIN_METRICS
from app.services.bi_seed import seed_builtin_metrics
from tests.conftest import TEST_ADMIN_TOKEN

UTC = timezone.utc


def _dt(*args):
    return datetime(*args, tzinfo=UTC)


@pytest.fixture
def bi_api(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/bi_api.db")

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


def _seed_defs(maker):
    async def _run():
        async with maker() as s:
            await seed_builtin_metrics(s)
    asyncio.run(_run())


def _seed_buffers(maker, items):
    """items: [(created_at, data_dict)]"""
    async def _run():
        async with maker() as s:
            for ts, data in items:
                s.add(DataBuffer(task_id=uuid.uuid4(), source_data=data,
                                 transformed_data=data, status="completed",
                                 created_at=ts))
            await s.commit()
    asyncio.run(_run())


# ============================================================
# 4.9 候选维度
# ============================================================

class TestDimensions:

    def test_returns_eight_candidates_with_grading(self, bi_api):
        client, _ = bi_api
        r = client.get("/api/v1/bi/dimensions")
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 8
        by = {i["name"]: i for i in items}
        assert by["类别"]["cardinality"] == "low"
        assert by["物料"]["usable_for_group"] is False
        assert "hint" in by["计划号"]


# ============================================================
# 4.1 / 4.8 定义管理
# ============================================================

class TestMetricDefinitions:

    def test_list_seeded(self, bi_api):
        client, maker = bi_api
        _seed_defs(maker)
        r = client.get("/api/v1/bi/metrics")
        assert r.status_code == 200
        assert len(r.json()) == len(BUILTIN_METRICS)

    def test_list_filter_enabled(self, bi_api):
        client, maker = bi_api
        _seed_defs(maker)
        enabled = client.get("/api/v1/bi/metrics", params={"enabled": True}).json()
        disabled = client.get("/api/v1/bi/metrics", params={"enabled": False}).json()
        assert len(enabled) + len(disabled) == len(BUILTIN_METRICS)
        assert all(m["enabled"] for m in enabled)

    def test_create_valid_definition(self, bi_api):
        client, _ = bi_api
        payload = {
            "metric_key": "my_metric", "name": "自定义", "source_type": "data_buffer",
            "granularity": "1h", "chart_type": "pie",
            "dimensions": [{"name": "category", "field": "category", "code": "category"}],
            "measures": [{"key": "n", "agg": "count"}],
            "enabled": False,
        }
        r = client.post("/api/v1/bi/metrics", json=payload)
        assert r.status_code == 201, r.text
        assert r.json()["metric_key"] == "my_metric"

    def test_create_rejects_unsupported_agg(self, bi_api):
        client, _ = bi_api
        payload = {
            "metric_key": "bad", "name": "bad", "source_type": "execution_logs",
            "measures": [{"key": "x", "agg": "median", "field": "duration_ms"}],
        }
        r = client.post("/api/v1/bi/metrics", json=payload)
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["code"] == "invalid_definition"
        codes = [i["code"] for i in detail["issues"]]
        assert "unsupported_agg" in codes

    def test_create_rejects_four_dimensions(self, bi_api):
        client, _ = bi_api
        payload = {
            "metric_key": "wide", "name": "wide", "source_type": "data_buffer",
            "dimensions": [{"name": f"d{i}", "field": f"f{i}"} for i in range(4)],
            "measures": [{"key": "n", "agg": "count"}],
        }
        r = client.post("/api/v1/bi/metrics", json=payload)
        assert r.status_code == 400
        codes = [i["code"] for i in r.json()["detail"]["issues"]]
        assert "too_many_dimensions" in codes

    def test_create_rejects_point_in_time_sum(self, bi_api):
        client, _ = bi_api
        payload = {
            "metric_key": "bad_time", "name": "t", "source_type": "data_buffer",
            "measures": [{"key": "t", "agg": "sum", "field": "order_time",
                          "field_type": "point_in_time"}],
        }
        r = client.post("/api/v1/bi/metrics", json=payload)
        assert r.status_code == 400
        codes = [i["code"] for i in r.json()["detail"]["issues"]]
        assert "point_in_time_agg_forbidden" in codes

    def test_create_rejects_non_readonly_custom_sql(self, bi_api):
        client, _ = bi_api
        payload = {
            "metric_key": "sqli", "name": "s", "source_type": "custom_sql",
            "source_ref": "DELETE FROM data_buffer",
            "measures": [{"key": "n", "agg": "count"}],
        }
        r = client.post("/api/v1/bi/metrics", json=payload)
        assert r.status_code == 400
        codes = [i["code"] for i in r.json()["detail"]["issues"]]
        assert "sql_not_read_only" in codes

    def test_create_duplicate_returns_409(self, bi_api):
        client, _ = bi_api
        payload = {"metric_key": "dup", "name": "d", "source_type": "execution_logs",
                   "measures": [{"key": "n", "agg": "count"}]}
        assert client.post("/api/v1/bi/metrics", json=payload).status_code == 201
        r = client.post("/api/v1/bi/metrics", json=payload)
        assert r.status_code == 409

    def test_update_definition(self, bi_api):
        client, _ = bi_api
        client.post("/api/v1/bi/metrics", json={
            "metric_key": "u1", "name": "原名", "source_type": "execution_logs",
            "measures": [{"key": "n", "agg": "count"}]})
        r = client.put("/api/v1/bi/metrics/u1", json={
            "name": "新名", "source_type": "execution_logs", "enabled": False,
            "measures": [{"key": "n", "agg": "count"}]})
        assert r.status_code == 200
        assert r.json()["name"] == "新名"
        assert r.json()["enabled"] is False

    def test_update_unknown_returns_404(self, bi_api):
        client, _ = bi_api
        r = client.put("/api/v1/bi/metrics/nope", json={
            "name": "x", "source_type": "execution_logs",
            "measures": [{"key": "n", "agg": "count"}]})
        assert r.status_code == 404

    def test_delete_removes_definition_and_buckets(self, bi_api):
        client, maker = bi_api
        client.post("/api/v1/bi/metrics", json={
            "metric_key": "del", "name": "d", "source_type": "data_buffer",
            "measures": [{"key": "n", "agg": "count"}]})
        _seed_buffers(maker, [(_dt(2026, 9, 25, 10, 0, 5), {"c": "A"})])
        client.post("/api/v1/bi/aggregate/run", params={
            "metric": "del", "from": "2026-09-25T10:00:00Z", "to": "2026-09-25T10:01:00Z"})
        assert client.get("/api/v1/bi/metrics/del").json()["bucket_count"] == 1

        assert client.delete("/api/v1/bi/metrics/del").status_code == 200
        assert client.get("/api/v1/bi/metrics/del").status_code == 404


# ============================================================
# 4.2 结果查询
# ============================================================

class TestQuery:

    DEF = {
        "metric_key": "q", "name": "查询", "source_type": "data_buffer",
        "granularity": "1min", "chart_type": "line",
        "dimensions": [{"name": "category", "field": "category", "code": "category"}],
        "measures": [{"key": "n", "agg": "count"}],
        "enabled": True,
    }

    def _prepare(self, client, maker):
        client.post("/api/v1/bi/metrics", json=self.DEF)
        _seed_buffers(maker, [
            (_dt(2026, 9, 25, 10, 0, 5), {"category": "A"}),
            (_dt(2026, 9, 25, 10, 0, 10), {"category": "A"}),
            (_dt(2026, 9, 25, 10, 0, 20), {"category": "B"}),
            (_dt(2026, 9, 25, 10, 1, 5), {"category": "A"}),
        ])
        client.post("/api/v1/bi/aggregate/run", params={
            "metric": "q", "from": "2026-09-25T10:00:00Z", "to": "2026-09-25T10:02:00Z"})

    def test_query_raw(self, bi_api):
        client, maker = bi_api
        self._prepare(client, maker)
        r = client.get("/api/v1/bi/metrics/q", params={
            "from": "2026-09-25T10:00:00Z", "to": "2026-09-25T10:02:00Z"})
        assert r.status_code == 200
        body = r.json()
        assert body["bucket_count"] == 3      # 10:00-A, 10:00-B, 10:01-A
        assert body["data"]["type"] == "timeseries"
        assert len(body["data"]["series"]) == 2

    def test_query_time_range_narrows(self, bi_api):
        client, maker = bi_api
        self._prepare(client, maker)
        r = client.get("/api/v1/bi/metrics/q", params={
            "from": "2026-09-25T10:01:00Z", "to": "2026-09-25T10:02:00Z"})
        assert r.json()["bucket_count"] == 1

    def test_query_dimension_filter(self, bi_api):
        client, maker = bi_api
        self._prepare(client, maker)
        r = client.get("/api/v1/bi/metrics/q", params={
            "from": "2026-09-25T10:00:00Z", "to": "2026-09-25T10:02:00Z",
            "dim": "category:A"})
        assert r.json()["bucket_count"] == 2

    def test_query_echarts_format(self, bi_api):
        client, maker = bi_api
        self._prepare(client, maker)
        r = client.get("/api/v1/bi/metrics/q", params={
            "format": "echarts-line"})
        assert r.status_code == 200
        assert "xAxis" in r.json()["data"]
        assert r.json()["data"]["series"][0]["type"] == "line"

    def test_query_unknown_metric_404(self, bi_api):
        client, _ = bi_api
        assert client.get("/api/v1/bi/metrics/nope").status_code == 404

    def test_query_unaligned_from_still_finds_bucket(self, bi_api):
        """回归：from 未对齐到桶边界时仍应包含所在桶

        实测踩到：1h 粒度指标 + from=10:20（桶起点 10:00）时桶被排除，
        查询恒空——粗粒度指标的数据“看不到”。
        """
        client, maker = bi_api
        client.post("/api/v1/bi/metrics", json={
            "metric_key": "hourly", "name": "小时", "source_type": "data_buffer",
            "granularity": "1h", "dimensions": [],
            "measures": [{"key": "n", "agg": "count"}], "enabled": True})
        _seed_buffers(maker, [(_dt(2026, 9, 25, 10, 25), {"a": 1})])
        client.post("/api/v1/bi/aggregate/run", params={
            "metric": "hourly",
            "from": "2026-09-25T10:10:00Z", "to": "2026-09-25T10:40:00Z"})

        r = client.get("/api/v1/bi/metrics/hourly", params={
            "from": "2026-09-25T10:20:00Z", "to": "2026-09-25T10:40:00Z"})
        assert r.status_code == 200
        assert r.json()["bucket_count"] == 1, "未对齐的 from 不应把所在桶排除"

    def test_backfill_same_bucket_range(self, bi_api):
        """回归：区间落在同一个粗粒度桶内时，回填必须仍产生桶"""
        client, maker = bi_api
        client.post("/api/v1/bi/metrics", json={
            "metric_key": "hourly2", "name": "小时2", "source_type": "data_buffer",
            "granularity": "1h", "dimensions": [],
            "measures": [{"key": "n", "agg": "count"}], "enabled": True})
        _seed_buffers(maker, [(_dt(2026, 9, 25, 10, 20), {"a": 1})])
        r = client.post("/api/v1/bi/aggregate/run", params={
            "metric": "hourly2",
            "from": "2026-09-25T10:10:00Z", "to": "2026-09-25T10:40:00Z"})
        assert r.json()["results"][0]["windows"] == 1, \
            "30 分钟区间 × 1h 粒度应产生 1 个窗口（曾经为 0）"

    def test_query_bad_format_400(self, bi_api):
        client, maker = bi_api
        self._prepare(client, maker)
        r = client.get("/api/v1/bi/metrics/q", params={"format": "echarts-nope"})
        assert r.status_code == 400

    def test_query_bad_time_400(self, bi_api):
        client, maker = bi_api
        self._prepare(client, maker)
        r = client.get("/api/v1/bi/metrics/q", params={"from": "not-a-date"})
        assert r.status_code == 400

    def test_query_bad_dim_filter_400(self, bi_api):
        client, maker = bi_api
        self._prepare(client, maker)
        r = client.get("/api/v1/bi/metrics/q", params={"dim": "no-colon"})
        assert r.status_code == 400

    def test_response_has_no_credential_leak(self, bi_api):
        """4.6 脱敏复核：响应中不得出现 enc: 或凭证字样"""
        client, maker = bi_api
        self._prepare(client, maker)
        text = client.get("/api/v1/bi/metrics/q",
                          params={"format": "raw"}).text.lower()
        assert "enc:" not in text
        assert "password" not in text
        assert "secret" not in text


# ============================================================
# 4.3 手动触发 / 回填
# ============================================================

class TestAggregateRun:

    def test_incremental_run(self, bi_api):
        client, maker = bi_api
        client.post("/api/v1/bi/metrics", json={
            "metric_key": "inc", "name": "i", "source_type": "data_buffer",
            "granularity": "1min", "dimensions": [],
            "measures": [{"key": "n", "agg": "count"}], "enabled": True})
        _seed_buffers(maker, [(_dt(2026, 9, 25, 10, 0, 5), {"a": 1})])
        r = client.post("/api/v1/bi/aggregate/run", params={"metric": "inc"})
        assert r.status_code == 200
        assert r.json()["results"][0]["mode"] == "incremental"

    def test_backfill_mode(self, bi_api):
        client, maker = bi_api
        client.post("/api/v1/bi/metrics", json={
            "metric_key": "bf", "name": "b", "source_type": "data_buffer",
            "granularity": "1min", "dimensions": [],
            "measures": [{"key": "n", "agg": "count"}], "enabled": True})
        _seed_buffers(maker, [(_dt(2026, 9, 25, 10, 0, 5), {"a": 1})])
        r = client.post("/api/v1/bi/aggregate/run", params={
            "metric": "bf", "from": "2026-09-25T10:00:00Z",
            "to": "2026-09-25T10:03:00Z"})
        body = r.json()["results"][0]
        assert body["mode"] == "backfill"
        assert body["windows"] == 3

    def test_run_all_enabled_when_no_metric(self, bi_api):
        """内置指标已全部默认停用（2026-09-25 重定位）：
        无 metric 参数时只聚合启用的定义——建一个启用定义验证聚合范围"""
        client, maker = bi_api
        _seed_defs(maker)
        client.post("/api/v1/bi/metrics", json={
            "metric_key": "runall", "name": "r", "source_type": "data_buffer",
            "granularity": "1min", "dimensions": [],
            "measures": [{"key": "n", "agg": "count"}], "enabled": True})
        r = client.post("/api/v1/bi/aggregate/run")
        assert r.status_code == 200
        assert r.json()["metrics"] == 1

    def test_run_unknown_metric_404(self, bi_api):
        client, _ = bi_api
        r = client.post("/api/v1/bi/aggregate/run", params={"metric": "nope"})
        assert r.status_code == 404

    def test_estimate_cardinality(self, bi_api):
        client, maker = bi_api
        client.post("/api/v1/bi/metrics", json={
            "metric_key": "est", "name": "e", "source_type": "data_buffer",
            "granularity": "1min",
            "dimensions": [{"name": "c", "field": "c"}],
            "measures": [{"key": "n", "agg": "count"}], "enabled": True})
        _seed_buffers(maker, [(_dt(2026, 9, 25, 10, 0, 5), {"c": "A"})])
        r = client.post("/api/v1/bi/metrics/est/estimate")
        assert r.status_code == 200
        assert "combinations" in r.json()


class TestScreenSettings:
    """看板设置（布局/筛选元素）读写"""

    def test_get_empty_returns_object(self, bi_api):
        client, _ = bi_api
        r = client.get("/api/v1/bi/screen-settings")
        assert r.status_code == 200
        assert r.json() == {}

    def test_put_then_get_roundtrip(self, bi_api):
        client, _ = bi_api
        payload = {
            "layout": {"order": ["a", "b"], "spans": {"a": 2}},
            "filter_dims": ["人名"],
        }
        r = client.put("/api/v1/bi/screen-settings", json=payload)
        assert r.status_code == 200
        r2 = client.get("/api/v1/bi/screen-settings")
        assert r2.json() == payload

    def test_put_overwrites(self, bi_api):
        client, _ = bi_api
        client.put("/api/v1/bi/screen-settings",
                   json={"layout": {"order": ["a"]}})
        client.put("/api/v1/bi/screen-settings",
                   json={"filter_dims": ["日期"]})
        r = client.get("/api/v1/bi/screen-settings")
        assert r.json() == {"filter_dims": ["日期"]}  # 整体覆盖非合并

    def test_put_shape_guard(self, bi_api):
        """P1 回归：不合形状的设置直接 422 不落库（曾整块覆盖默认看板）"""
        client, _ = bi_api
        bad_bodies = [
            {"name": 123},
            {"cells": ["a", "b"]},
            {"cells": {"k": [0, 0, 36, 8]}},
            {"cells": {"k": {"x": 0, "y": "top", "w": 36, "h": 8}}},
            {"cells": {"k": {"x": True, "y": 0, "w": 36, "h": 8}}},
            {"filter_dims": ["x", 1]},
        ]
        for body in bad_bodies:
            r = client.put("/api/v1/bi/screen-settings", json=body)
            assert r.status_code == 422, body
        r = client.get("/api/v1/bi/screen-settings")
        assert r.json() == {}                     # 坏数据未落库
        ok = client.put("/api/v1/bi/screen-settings", json={
            "name": "生产看板",
            "cells": {"m1": {"x": 0, "y": 0, "w": 36.5, "h": 8}}})
        assert ok.status_code == 200


class TestBoards:
    """多看板：/bi/screens CRUD + settings 按 board 隔离"""

    def test_default_board_in_list(self, bi_api):
        client, _ = bi_api
        r = client.get("/api/v1/bi/screens")
        assert r.status_code == 200
        keys = [s["key"] for s in r.json()]
        assert "default" in keys

    def test_create_list_rename_delete(self, bi_api):
        client, _ = bi_api
        r = client.post("/api/v1/bi/screens",
                        json={"key": "prod", "name": "生产运行管理"})
        assert r.status_code == 201
        assert r.json() == {"key": "prod", "name": "生产运行管理"}
        keys = {s["key"]: s["name"]
                for s in client.get("/api/v1/bi/screens").json()}
        assert keys["prod"] == "生产运行管理"
        # 新建看板自带空布局
        s = client.get("/api/v1/bi/screen-settings",
                       params={"board": "prod"}).json()
        assert s["cells"] == {} and s["filter_dims"] == []
        # 重命名落回 settings
        assert client.put("/api/v1/bi/screens/prod",
                          json={"name": "生产看板"}).status_code == 200
        assert client.get("/api/v1/bi/screen-settings",
                          params={"board": "prod"}).json()["name"] == "生产看板"
        # 删除连带设置
        assert client.delete("/api/v1/bi/screens/prod").json() == {"deleted": "prod"}
        assert client.get("/api/v1/bi/screen-settings",
                          params={"board": "prod"}).json() == {}
        keys = [s["key"] for s in client.get("/api/v1/bi/screens").json()]
        assert "prod" not in keys

    def test_settings_isolated_per_board(self, bi_api):
        client, _ = bi_api
        client.post("/api/v1/bi/screens", json={"key": "b1", "name": "B1"})
        client.put("/api/v1/bi/screen-settings?board=b1",
                   json={"cells": {"k": {"x": 0, "y": 0, "w": 36, "h": 12}},
                         "filter_dims": ["产线"]})
        client.put("/api/v1/bi/screen-settings",  # 默认看板互不影响
                   json={"cells": {}, "filter_dims": []})
        d = client.get("/api/v1/bi/screen-settings").json()
        b1 = client.get("/api/v1/bi/screen-settings",
                        params={"board": "b1"}).json()
        assert d["cells"] == {}
        assert b1["cells"]["k"]["w"] == 36
        assert b1["filter_dims"] == ["产线"]

    def test_create_rejects_duplicate_and_default_and_bad_key(self, bi_api):
        client, _ = bi_api
        assert client.post("/api/v1/bi/screens",
                           json={"key": "x1", "name": "X"}).status_code == 201
        assert client.post("/api/v1/bi/screens",
                           json={"key": "x1", "name": "X"}).status_code == 409
        assert client.post("/api/v1/bi/screens",
                           json={"key": "default", "name": "X"}).status_code == 400
        assert client.post("/api/v1/bi/screens",
                           json={"key": "非法 key!", "name": "X"}).status_code == 400

    def test_delete_default_rejected_and_missing_404(self, bi_api):
        client, _ = bi_api
        assert client.delete("/api/v1/bi/screens/default").status_code == 400
        assert client.delete("/api/v1/bi/screens/nope").status_code == 404
        assert client.put("/api/v1/bi/screens/nope",
                          json={"name": "N"}).status_code == 404
