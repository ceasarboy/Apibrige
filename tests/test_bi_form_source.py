# -*- coding: utf-8 -*-
"""BI form 数据源测试：类型归一 / source_ref 校验 / 取数与缓存 / 快照语义 / 端点"""
import asyncio
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import get_session
from app.main import app
from app.models.bi import BiAggregationWatermark, BiMetricBucket, BiMetricDef
from app.models.sync import ApiConnection, Base
from app.services.bi_aggregator import backfill, run_metric
from app.services.bi_defs import (
    SOURCE_TYPES,
    classify_bi_type,
    is_valid_form_ref,
    validate_metric_def,
)
from app.services.bi_sources import (
    MAX_FORM_ROWS,
    SourceError,
    _scan_form,
    fetch_form_rows,
    parse_form_ref,
)
from tests.conftest import TEST_ADMIN_TOKEN

UTC = timezone.utc
REF = json.dumps({"connection_id": "11111111-1111-1111-1111-111111111111",
                  "form_key": "skill_test_items"})


@pytest.fixture(autouse=True)
def _clear_form_cache():
    from app.services import bi_sources

    bi_sources._form_cache.clear()
    yield
    bi_sources._form_cache.clear()


@pytest.fixture
def bi_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/form.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    asyncio.run(engine.dispose())


async def _add_connection(maker, **kw):
    defaults = dict(
        name="测试库连接", connection_type="target", protocol="hap",
        status="active", base_url="https://hap.example.com",
        auth_type="custom", auth_config={},
    )
    defaults.update(kw)
    async with maker() as s:
        conn = ApiConnection(**defaults)
        s.add(conn)
        await s.commit()
        await s.refresh(conn)
        return conn.id


# ============================================================
# 类型归一（借鉴 DataEase deType+groupType）
# ============================================================

class TestClassifyBiType:

    def test_hap_names(self):
        assert classify_bi_type("数值") == "number"
        assert classify_bi_type("金额") == "number"
        assert classify_bi_type("自动编号") == "number"
        assert classify_bi_type("日期") == "time"
        assert classify_bi_type("文本") == "dimension"
        assert classify_bi_type("单选") == "dimension"
        assert classify_bi_type("成员") == "dimension"

    def test_db_types(self):
        assert classify_bi_type("INTEGER") == "number"
        assert classify_bi_type("numeric(10,2)") == "number"
        assert classify_bi_type("TIMESTAMP") == "time"
        assert classify_bi_type("datetime") == "time"
        assert classify_bi_type("varchar(100)") == "dimension"
        assert classify_bi_type("") == "dimension"
        assert classify_bi_type(None) == "dimension"


# ============================================================
# source_ref 校验
# ============================================================

class TestFormRef:

    def test_valid(self):
        assert is_valid_form_ref(REF) is True

    def test_invalid(self):
        assert is_valid_form_ref('{"connection_id": "x"}') is False
        assert is_valid_form_ref("not json") is False
        assert is_valid_form_ref("") is False
        assert is_valid_form_ref(None) is False
        assert is_valid_form_ref('{"connection_id":"x","form_key":""}') is False

    def test_source_type_registered(self):
        assert "form" in SOURCE_TYPES

    def test_validate_form_def(self):
        defn = {"metric_key": "m1", "source_type": "form", "source_ref": REF,
                "dimensions": [{"name": "类别", "field": "category"}],
                "measures": [{"key": "总额", "agg": "sum", "field": "amount"}]}
        codes = {i["code"] for i in validate_metric_def(defn)}
        assert "invalid_form_source" not in codes

    def test_validate_form_def_bad_ref(self):
        defn = {"metric_key": "m1", "source_type": "form",
                "source_ref": "broken", "measures": []}
        codes = {i["code"] for i in validate_metric_def(defn)}
        assert "invalid_form_source" in codes

    def test_validate_detail_requires_form(self):
        base = {"metric_key": "m1", "chart_type": "detail", "measures": []}
        d1 = dict(base, source_type="form", source_ref=REF)
        assert "detail_requires_form_source" not in {
            i["code"] for i in validate_metric_def(d1)}
        d2 = dict(base, source_type="data_buffer")
        assert "detail_requires_form_source" in {
            i["code"] for i in validate_metric_def(d2)}


class TestParseFormRef:

    def test_roundtrip(self):
        assert parse_form_ref({"source_ref": REF}) == (
            "11111111-1111-1111-1111-111111111111", "skill_test_items")

    def test_invalid_raises(self):
        with pytest.raises(SourceError):
            parse_form_ref({"source_ref": "{"})


# ============================================================
# 取数与缓存
# ============================================================

_ROWS = [
    {"category": "联调", "amount": "10"},
    {"category": "联调", "amount": "20"},
    {"category": "其他", "amount": "30"},
]


class TestFetchFormRows:

    @staticmethod
    def _fake_fields(*paths: str):
        """合并后取数会经字段端点取元数据（行键归一到字段字典 path + 脱敏），
        单元测试里替身掉这一层，避免构造真实适配器。"""
        return AsyncMock(return_value={
            "kind": "hap", "worksheet_id": "ws1",
            "fields": [{"path": p, "name": p, "type": "string", "readable": True}
                       for p in paths]})

    async def test_hap_path_and_cache(self, bi_db):
        conn_id = await _add_connection(bi_db, protocol="hap")
        fake_adapter = MagicMock()
        # 全量拉取带 return_total=True（add-bi-hap-incremental 的规模判定）→ 返回 (rows, total)
        fake_adapter.get_worksheet_rows = AsyncMock(
            return_value=(list(_ROWS), len(_ROWS)))
        with patch("app.services.adapter_config.build_adapter_config",
                   return_value={}), \
             patch("app.adapters.factory.ProtocolAdapterFactory.create",
                   return_value=fake_adapter), \
             patch("app.api.connections.get_connection_fields",
                   self._fake_fields("category", "amount", "region")):
            async with bi_db() as s:
                rows1 = await fetch_form_rows(s, str(conn_id), "ws1")
                rows2 = await fetch_form_rows(s, str(conn_id), "ws1")
        assert rows1 == _ROWS and rows2 == _ROWS
        assert fake_adapter.get_worksheet_rows.await_count == 1  # 缓存命中

    async def test_db_path(self, bi_db):
        conn_id = await _add_connection(
            bi_db, protocol="sqlite", db_type="sqlite",
            db_config={"path": "/tmp/x.db"})
        fake_connector = MagicMock()
        fake_connector.fetch_data = AsyncMock(side_effect=[list(_ROWS), []])
        fake_connector.disconnect = AsyncMock()
        with patch("app.connectors.database.get_database_connector",
                   return_value=fake_connector), \
             patch("app.api.connections.get_connection_fields",
                   self._fake_fields("category", "amount", "region")):
            async with bi_db() as s:
                rows = await fetch_form_rows(s, str(conn_id), "skill_test_items")
        assert rows == _ROWS
        assert fake_connector.fetch_data.await_count == 1   # 首页短于批大小 → 判表尾，不再翻页

    async def test_row_cap_rejects(self, bi_db):
        """超上限**拒绝**（不截断）——目标方语义：不把部分数据当完整结果返回"""
        conn_id = await _add_connection(bi_db, protocol="hap")
        fake_adapter = MagicMock()
        many = [{"i": i} for i in range(MAX_FORM_ROWS + 5)]
        fake_adapter.get_worksheet_rows = AsyncMock(return_value=(many, len(many)))
        with patch("app.services.adapter_config.build_adapter_config",
                   return_value={}), \
             patch("app.adapters.factory.ProtocolAdapterFactory.create",
                   return_value=fake_adapter):
            async with bi_db() as s:
                with pytest.raises(SourceError) as ei:
                    await fetch_form_rows(s, str(conn_id), "ws1")
        assert "上限" in str(ei.value)

    async def test_upstream_error_sanitized(self, bi_db):
        """上游异常对外只给可操作提示（驱动原文可能带 DSN/凭证，只进日志）"""
        conn_id = await _add_connection(bi_db, protocol="hap")
        fake_adapter = MagicMock()
        fake_adapter.get_worksheet_rows = AsyncMock(
            side_effect=RuntimeError("鉴权失败"))
        with patch("app.services.adapter_config.build_adapter_config",
                   return_value={}), \
             patch("app.adapters.factory.ProtocolAdapterFactory.create",
                   return_value=fake_adapter):
            async with bi_db() as s:
                with pytest.raises(SourceError) as ei:
                    await fetch_form_rows(s, str(conn_id), "ws1")
        assert "BI 表单读取失败" in str(ei.value)
        assert "鉴权失败" not in str(ei.value)

    async def test_missing_connection(self, bi_db):
        async with bi_db() as s:
            with pytest.raises(SourceError):
                await fetch_form_rows(s, str(uuid.uuid4()), "t")


class TestScanForm:

    async def test_row_shape(self, bi_db):
        defn = {"source_type": "form", "source_ref": REF}
        with patch("app.services.bi_sources.fetch_form_rows",
                   AsyncMock(return_value=list(_ROWS))):
            async with bi_db() as s:
                out = await _scan_form(s, defn)
        assert len(out) == 3
        assert out[0]["data"] == _ROWS[0]
        assert out[0]["fetched_at"].tzinfo is not None


# ============================================================
# 快照聚合语义
# ============================================================

_FORM_DEFN = {
    "metric_key": "form_summary",
    "name": "表单汇总",
    "source_type": "form",
    "source_ref": REF,
    "time_field": "created_at",
    "granularity": "1min",
    "dimensions": [{"name": "类别", "field": "category"}],
    "measures": [{"key": "总额", "agg": "sum", "field": "amount"}],
    "chart_type": "bar",
    "top_n": 100,
    "enabled": True,
}


class TestSnapshotRunMetric:

    async def test_single_latest_window(self, bi_db):
        async with bi_db() as s:
            s.add(BiMetricDef(**_FORM_DEFN))
            await s.commit()
        with patch("app.services.bi_sources.fetch_form_rows",
                   AsyncMock(return_value=list(_ROWS))):
            now = datetime(2026, 9, 25, 10, 2, 30, tzinfo=UTC)
            async with bi_db() as s:
                r = await run_metric(s, _FORM_DEFN, now=now)
        assert r["windows"] == 1 and r["lagged"] is False

        # 最新完整窗 = 10:01；2 个维度组合各一条桶；数值字段经 str→float 归一
        async with bi_db() as s:
            buckets = (await s.execute(
                __import__("sqlalchemy").select(BiMetricBucket))).scalars().all()
            wm = await s.get(BiAggregationWatermark, "form_summary")
        assert len(buckets) == 2
        # SQLite TIMESTAMP 读回 naive UTC
        assert buckets[0].bucket_start == datetime(2026, 9, 25, 10, 1)
        vals = {(b.dimensions or {}).get("类别"): (b.values or {}).get("总额")
                for b in buckets}
        assert vals["联调"] == 30.0
        assert vals["其他"] == 30.0
        assert wm.last_bucket == datetime(2026, 9, 25, 10, 1)

    async def test_no_recompute_same_window(self, bi_db):
        async with bi_db() as s:
            s.add(BiMetricDef(**_FORM_DEFN))
            await s.commit()
        with patch("app.services.bi_sources.fetch_form_rows",
                   AsyncMock(return_value=list(_ROWS))) as fake:
            now = datetime(2026, 9, 25, 10, 2, 30, tzinfo=UTC)
            async with bi_db() as s:
                await run_metric(s, _FORM_DEFN, now=now)
                r2 = await run_metric(s, _FORM_DEFN, now=now)
        assert r2["windows"] == 0
        assert fake.await_count == 1

    async def test_backfill_rejected(self):
        with pytest.raises(SourceError):
            await backfill(MagicMock(), _FORM_DEFN,
                           datetime(2026, 9, 25, 10, 0, tzinfo=UTC),
                           datetime(2026, 9, 25, 11, 0, tzinfo=UTC))


class TestFormBucketRetention:
    """快照桶保留窗口：只保留最近 N 个桶，旧桶自动清理"""

    async def test_prune_keeps_latest_buckets(self, bi_db):
        from app.services.bi_aggregator import _prune_form_buckets

        async with bi_db() as s:
            # 造 5 个桶（时间递增）
            for i in range(5):
                s.add(BiMetricBucket(
                    metric_key="form_summary",
                    bucket_start=datetime(2026, 9, 25, 10, i, tzinfo=UTC),
                    dim_hash=f"h{i}", dimensions={"类别": f"c{i}"},
                    values={"总额": i}))
            await s.commit()
            removed = await _prune_form_buckets(s, "form_summary", keep=3)
        assert removed == 2
        async with bi_db() as s:
            left = (await s.execute(
                __import__("sqlalchemy").select(BiMetricBucket)
                .order_by(BiMetricBucket.bucket_start))).scalars().all()
        assert [b.bucket_start.minute for b in left] == [2, 3, 4]  # 最新 3 个

    async def test_run_metric_prunes_even_when_caught_up(self, bi_db):
        """水位线已追平时也要清旧桶（存量清理路径；上限 60）"""
        from app.services.bi_aggregator import FORM_SNAPSHOT_KEEP_BUCKETS

        async with bi_db() as s:
            s.add(BiMetricDef(**_FORM_DEFN))
            # 水位线 = 最新完整窗
            s.add(BiAggregationWatermark(
                metric_key="form_summary",
                last_bucket=datetime(2026, 9, 25, 10, 1, tzinfo=UTC)))
            # 65 个历史旧桶（都早于水位线）→ 超出保留上限 5 个
            base = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
            for i in range(FORM_SNAPSHOT_KEEP_BUCKETS + 5):
                s.add(BiMetricBucket(
                    metric_key="form_summary",
                    bucket_start=base + __import__("datetime").timedelta(minutes=i),
                    dim_hash=f"old{i}", dimensions={"类别": "旧"},
                    values={"总额": 0}))
            await s.commit()
        with patch("app.services.bi_sources.fetch_form_rows",
                   AsyncMock(return_value=list(_ROWS))):
            now = datetime(2026, 9, 25, 10, 2, 30, tzinfo=UTC)
            async with bi_db() as s:
                r = await run_metric(s, _FORM_DEFN, now=now)
        assert r["windows"] == 0 and r["pruned"] == 5
        async with bi_db() as s:
            left = (await s.execute(
                __import__("sqlalchemy").select(BiMetricBucket)
                .order_by(BiMetricBucket.bucket_start))).scalars().all()
        # 保留窗口 60 含水位线桶：59 个旧桶 + 水位线窗（10:01）
        assert len(left) == FORM_SNAPSHOT_KEEP_BUCKETS

    async def test_prune_noop_when_under_limit(self, bi_db):
        from app.services.bi_aggregator import _prune_form_buckets

        async with bi_db() as s:
            for i in range(2):
                s.add(BiMetricBucket(
                    metric_key="form_summary",
                    bucket_start=datetime(2026, 9, 25, 10, i, tzinfo=UTC),
                    dim_hash=f"h{i}", dimensions={}, values={}))
            await s.commit()
            removed = await _prune_form_buckets(s, "form_summary", keep=60)
        assert removed == 0


# ============================================================
# API：form-fields / form-rows / detail 查询
# ============================================================

@pytest.fixture
def bi_api(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/form_api.db")

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


class TestFormApi:

    async def _seed_conn_and_def(self, maker):
        async with maker() as s:
            conn = ApiConnection(name="库连接", connection_type="target",
                                 protocol="sqlite", db_type="sqlite",
                                 status="active",
                                 db_config={"path": "/tmp/x.db"})
            s.add(conn)
            await s.commit()
            await s.refresh(conn)
            s.add(BiMetricDef(**dict(
                _FORM_DEFN,
                source_ref=json.dumps({"connection_id": str(conn.id),
                                       "form_key": "skill_test_items"}),
                chart_type="detail", dimensions=[], measures=[],
            )))
            await s.commit()
            return str(conn.id)

    def test_form_rows_endpoint(self, bi_api):
        client, maker = bi_api
        conn_id = asyncio.run(self._seed_conn_and_def(maker))
        with patch("app.api.bi.fetch_form_rows",
                   AsyncMock(return_value=list(_ROWS))):
            r = client.get("/api/v1/bi/form-rows", params={
                "connection_id": conn_id, "form_key": "skill_test_items"})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 3 and len(body["rows"]) == 3

    def test_form_rows_limit(self, bi_api):
        client, maker = bi_api
        conn_id = asyncio.run(self._seed_conn_and_def(maker))
        with patch("app.api.bi.fetch_form_rows",
                   AsyncMock(return_value=list(_ROWS))):
            r = client.get("/api/v1/bi/form-rows", params={
                "connection_id": conn_id, "form_key": "t", "limit": 2})
        assert r.json()["total"] == 3
        assert len(r.json()["rows"]) == 2

    def test_detail_query_returns_rows(self, bi_api):
        client, maker = bi_api
        conn_id = asyncio.run(self._seed_conn_and_def(maker))
        with patch("app.api.bi.fetch_form_rows",
                   AsyncMock(return_value=list(_ROWS))):
            r = client.get("/api/v1/bi/metrics/form_summary",
                           params={"format": "bi-detail"})
        assert r.status_code == 200
        body = r.json()
        assert body["bucket_count"] == 0
        assert body["data"]["rows"] == _ROWS

    def test_form_fields_db(self, bi_api):
        client, maker = bi_api
        conn_id = asyncio.run(self._seed_conn_and_def(maker))
        schema = [
            {"name": "id", "type": "INTEGER", "nullable": False},
            {"name": "name", "type": "TEXT", "nullable": False},
            {"name": "updated_at", "type": "DATETIME", "nullable": True},
        ]
        with patch("app.connectors.database.get_database_connector") as fake_get, \
             patch("app.security.crypto.decrypt_db_config",
                   side_effect=lambda cfg: cfg):
            fake_connector = MagicMock()
            fake_connector.get_table_schema = AsyncMock(return_value=schema)
            fake_get.return_value = fake_connector
            r = client.get("/api/v1/bi/form-fields", params={
                "connection_id": conn_id, "form_key": "skill_test_items"})
        assert r.status_code == 200
        fields = {f["path"]: f for f in r.json()["fields"]}
        assert fields["id"]["bi_type"] == "number"
        assert fields["name"]["bi_type"] == "dimension"
        assert fields["updated_at"]["bi_type"] == "time"
