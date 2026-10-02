# -*- coding: utf-8 -*-
"""BI 即席分析（add-bi-adhoc-analysis）测试

覆盖 `app/services/bi_adhoc.py` 与 `POST /api/v1/bi/adhoc/query`：
- 定义归一与校验（与保存端点同一套规则）；
- 取值三态：下推命中 / 内存回退（含回退原因与截断标注）；
- 排序（度量/维度，升/降，缺值末尾）与 Top N 的独立生效；
- 输出与 `/bi/metrics` 同构（格式层复用）；数值与源库 SQL 直查逐项一致；
- 回传的 `defn` 可直接保存为卡片（"能出图就能保存"）。
"""
import asyncio
import json
import sqlite3
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import get_session
from app.main import app
from app.models.sync import ApiConnection, Base
from app.services import bi_sources
from app.services.bi_adhoc import (
    AdhocError,
    build_adhoc_defn,
    default_format,
    query_adhoc_defn,
)
from tests.conftest import TEST_ADMIN_TOKEN

TABLE = "adhoc_rows"
ROWS = [
    (1, "A线", "早班", 10.0, 12.0, "n1"),
    (2, "A线", "中班", 20.0, 20.0, "n2"),
    (3, "B线", "早班", 30.0, 30.0, None),
    (4, "B线", "中班", 5.0, 40.0, "n4"),
    (5, "C线", "晚班", None, 10.0, "n5"),
    (6, None, "早班", 40.0, 50.0, "n6"),
]


@pytest.fixture
def src_db(tmp_path):
    path = tmp_path / "adhoc_src.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        f"CREATE TABLE {TABLE} (id INTEGER PRIMARY KEY, line TEXT, shift TEXT, "
        "qty REAL, plan REAL, note TEXT)")
    conn.executemany(
        f"INSERT INTO {TABLE} (id, line, shift, qty, plan, note) "
        "VALUES (?, ?, ?, ?, ?, ?)", ROWS)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def app_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/adhoc_app.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def _clear_caches():
    bi_sources._form_cache.clear()
    bi_sources._pushdown_cache.clear()
    yield
    bi_sources._form_cache.clear()
    bi_sources._pushdown_cache.clear()


def _sql(db, sql, params=()):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


async def _add_conn(maker, src_path, *, protocol="sqlite", db_type="sqlite"):
    async with maker() as s:
        conn = ApiConnection(
            name="即席源", connection_type="target", protocol=protocol,
            db_type=db_type,
            db_config={"database": str(src_path)} if db_type else None,
            auth_type="custom" if protocol == "hap" else None,
            auth_config={} if protocol == "hap" else None,
            status="active",
        )
        s.add(conn)
        await s.commit()
        await s.refresh(conn)
        return str(conn.id)


async def _query(maker, conn_id, **over):
    payload = {
        "connection_id": conn_id, "form_key": TABLE, "chart_type": "bar",
        "format": "raw",
        "dimensions": [{"name": "产线", "field": "line"}],
        "measures": [{"key": "产量", "agg": "sum", "field": "qty",
                      "field_type": "number"}],
    }
    payload.update(over)
    body = {k: v for k, v in payload.items() if k not in ("sort", "format")}
    defn = build_adhoc_defn(body)
    async with maker() as s:
        return await query_adhoc_defn(
            s, defn, fmt=payload.get("format"), sort=payload.get("sort"))


# ============================================================
# 定义归一与校验
# ============================================================

class TestDefn:

    def test_defaults_and_source_ref(self):
        d = build_adhoc_defn({"connection_id": " c1 ", "form_key": " t1 "})
        assert json.loads(d["source_ref"]) == {"connection_id": "c1", "form_key": "t1"}
        assert d["chart_type"] == "bar" and d["top_n"] == 100
        assert d["granularity"] == "1min" and d["source_type"] == "form"
        assert default_format("number") == "datav-number"
        assert default_format("time_line") == "echarts-line"

    def test_validation_reuses_metric_rules(self, app_db, src_db):
        """与保存端点同一套校验：4 个维度 / 未知算子 / 缺数据源 都被拒"""
        conn_id = asyncio.run(_add_conn(app_db, src_db))

        async def _run(over):
            with pytest.raises(AdhocError) as ei:
                await _query(app_db, conn_id, **over)
            return ei.value.issues

        issues = asyncio.run(_run({"dimensions": [
            {"name": f"d{i}", "field": f"f{i}"} for i in range(4)]}))
        assert issues[0]["code"] == "too_many_dimensions"

        issues = asyncio.run(_run({"measures": [{"key": "k", "agg": "nope"}]}))
        assert issues[0]["code"] == "unsupported_agg"

        async def _bad_source():
            defn = build_adhoc_defn({"connection_id": "", "form_key": ""})
            async with app_db() as s:
                await query_adhoc_defn(s, defn)
        with pytest.raises(AdhocError) as ei:
            asyncio.run(_bad_source())
        assert ei.value.issues[0]["code"] == "invalid_source_ref"

    def test_invalid_sort_and_format_rejected(self, app_db, src_db):
        conn_id = asyncio.run(_add_conn(app_db, src_db))
        with pytest.raises(AdhocError) as ei:
            asyncio.run(_query(app_db, conn_id, sort={"by": "不存在"}))
        assert ei.value.issues[0]["code"] == "invalid_sort"
        with pytest.raises(AdhocError) as ei:
            asyncio.run(_query(app_db, conn_id, format="not-a-format"))
        assert ei.value.issues[0]["code"] == "invalid_format"


# ============================================================
# 取值：下推 / 内存回退 / 截断
# ============================================================

class TestQuery:

    def test_pushdown_engine_and_values(self, app_db, src_db):
        conn_id = asyncio.run(_add_conn(app_db, src_db))
        out = asyncio.run(_query(app_db, conn_id))
        assert out["engine"] == "pushdown" and out["fallback_reason"] is None
        assert out["truncated"] is False
        assert out["format"] == "raw"
        assert default_format("bar") == "echarts-bar"   # 缺省格式另测

        truth = {(k if k is not None else "未知"): v for k, v in _sql(
            src_db, f"SELECT line, SUM(qty) FROM {TABLE} GROUP BY line")}
        got = {x: v for x, v in zip(out["data"]["x"], out["data"]["series"][0]["data"])}
        assert got.keys() == truth.keys()
        for k, v in truth.items():
            if v is None:            # C线 qty 全空：两侧都应是 NULL（不编造 0）
                assert got[k] is None
                continue
            assert abs(got[k] - v) <= 1e-9 * max(1.0, abs(v))

    def test_memory_engine_for_hap_source(self, app_db, src_db):
        """HAP 源：内存路径 + 原因透出（与 P3 判定一致，不谎报下推）"""
        conn_id = asyncio.run(_add_conn(app_db, src_db, protocol="hap", db_type=None))
        rows = [{"line": r[1], "shift": r[2], "qty": r[3]} for r in ROWS]
        with patch("app.services.bi_sources.fetch_form_rows",
                   AsyncMock(return_value=rows)):
            out = asyncio.run(_query(app_db, conn_id))
        assert out["engine"] == "memory" and out["fallback_reason"] == "source_not_db"
        assert out["row_count"] == len(ROWS)
        got = {x: v for x, v in zip(out["data"]["x"], out["data"]["series"][0]["data"])}
        assert got["A线"] == 30.0 and got["未知"] == 40.0

    def test_memory_engine_for_computed(self, app_db, src_db):
        """含 computed → 不可下推（原因 computed_fields），内存路径算出派生列"""
        conn_id = asyncio.run(_add_conn(app_db, src_db))
        out = asyncio.run(_query(
            app_db, conn_id,
            dimensions=[],          # 数值卡取单条汇总（有维度时是"最后一组"的值）
            computed=[{"key": "double", "name": "两倍", "expr": "[qty] * 2",
                       "field_type": "number"}],
            measures=[{"key": "两倍和", "agg": "sum", "field": "double",
                       "field_type": "number"}],
            chart_type="number"))
        assert out["engine"] == "memory"
        assert out["fallback_reason"] == "computed_fields"
        assert out["data"]["value"] == sum(r[3] * 2 for r in ROWS if r[3])

    def test_truncated_flag_on_memory_path(self, app_db, src_db, monkeypatch):
        conn_id = asyncio.run(_add_conn(app_db, src_db, protocol="hap", db_type=None))
        rows = [{"line": r[1], "qty": r[3]} for r in ROWS]
        monkeypatch.setattr(bi_sources, "MAX_FORM_ROWS", 3)
        with patch("app.services.bi_sources.fetch_form_rows",
                   AsyncMock(return_value=rows)):
            out = asyncio.run(_query(app_db, conn_id))
        assert out["engine"] == "memory" and out["truncated"] is True

    def test_ratio_measure_pushdown_with_scale(self, app_db, src_db):
        conn_id = asyncio.run(_add_conn(app_db, src_db))
        out = asyncio.run(_query(
            app_db, conn_id, dimensions=[],
            measures=[{"key": "达成率", "agg": "ratio",
                       "numerator": {"agg": "sum", "field": "qty"},
                       "denominator": {"agg": "sum", "field": "plan"},
                       "scale": 100}],
            chart_type="number"))
        assert out["engine"] == "pushdown"
        num = sum(r[3] for r in ROWS if r[3])
        den = sum(r[4] for r in ROWS)
        assert abs(out["data"]["value"] - num / den * 100) < 1e-9


# ============================================================
# 排序与 Top N
# ============================================================

class TestSortTopN:

    def test_sort_by_measure_desc_and_asc(self, app_db, src_db):
        conn_id = asyncio.run(_add_conn(app_db, src_db))
        desc = asyncio.run(_query(app_db, conn_id,
                                  sort={"by": "产量", "order": "desc"}))
        assert desc["data"]["x"] == ["未知", "B线", "A线", "C线"]   # 40/35/30/None
        asc = asyncio.run(_query(app_db, conn_id,
                                 sort={"by": "产量", "order": "asc"}))
        # 升序：A线(30) < B线(35) < 未知(40)；C线 qty 全空 → 缺值恒排末尾
        assert asc["data"]["x"] == ["A线", "B线", "未知", "C线"]

    def test_sort_by_dimension(self, app_db, src_db):
        conn_id = asyncio.run(_add_conn(app_db, src_db))
        out = asyncio.run(_query(app_db, conn_id,
                                 sort={"by": "产线", "order": "asc"}))
        assert out["data"]["x"] == ["A线", "B线", "C线", "未知"]

    def test_sort_then_top_n_others(self, app_db, src_db):
        """排序先于 Top N：取最高的 1 项，其余归并「其他」（可加算子求和）"""
        conn_id = asyncio.run(_add_conn(app_db, src_db))
        out = asyncio.run(_query(app_db, conn_id, top_n=1,
                                 sort={"by": "产量", "order": "desc"}))
        assert out["data"]["x"] == ["未知", "其他"]
        vals = out["data"]["series"][0]["data"]
        assert vals[0] == 40.0 and vals[1] == 35.0 + 30.0

    def test_no_sort_keeps_source_order(self, app_db, src_db):
        """不传 sort：顺序由源库分组顺序决定（不额外排序，也不丢组）"""
        conn_id = asyncio.run(_add_conn(app_db, src_db))
        out = asyncio.run(_query(app_db, conn_id))
        assert set(out["data"]["x"]) == {"A线", "B线", "C线", "未知"}
        assert len(out["data"]["x"]) == len(out["data"]["series"][0]["data"])


# ============================================================
# API 端点
# ============================================================

@pytest.fixture
def adhoc_api(src_db, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/adhoc_api.db")

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


class TestAdhocApi:

    def test_query_and_save_as_card(self, adhoc_api, src_db):
        """即席查询 → 回传 defn 原样保存为卡片（"能出图就能保存"）+ 数值一致"""
        client, maker = adhoc_api
        conn_id = asyncio.run(_add_conn(maker, src_db))

        r = client.post("/api/v1/bi/adhoc/query", json={
            "connection_id": conn_id, "form_key": TABLE, "chart_type": "bar",
            "format": "raw",
            "dimensions": [{"name": "产线", "field": "line"}],
            "measures": [{"key": "产量", "agg": "sum", "field": "qty",
                          "field_type": "number"}],
            "sort": {"by": "产量", "order": "desc"},
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["engine"] == "pushdown"
        assert body["data"]["x"] == ["未知", "B线", "A线", "C线"]
        assert "sort" not in body["defn"]          # 排序不进定义

        # 另存为卡片：用回传 defn 直接 POST /bi/metrics
        save = client.post("/api/v1/bi/metrics", json={
            **body["defn"], "metric_key": "adhoc_saved", "name": "即席另存",
            "enabled": True,
        })
        assert save.status_code == 201, save.text

        # 聚合入桶后查询：数值与即席一致
        client.post("/api/v1/bi/aggregate/run", params={"metric": "adhoc_saved"})
        got = client.get("/api/v1/bi/metrics/adhoc_saved",
                         params={"format": "raw"}).json()["data"]
        card = {x: v for x, v in zip(got["x"], got["series"][0]["data"])}
        live = {x: v for x, v in zip(body["data"]["x"],
                                     body["data"]["series"][0]["data"])}
        assert card == live

    def test_invalid_request_returns_issues(self, adhoc_api, src_db):
        client, maker = adhoc_api
        conn_id = asyncio.run(_add_conn(maker, src_db))
        r = client.post("/api/v1/bi/adhoc/query", json={
            "connection_id": conn_id, "form_key": TABLE,
            "measures": [{"key": "k", "agg": "p95"}],   # 缺 field
        })
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["code"] == "invalid_adhoc"
        assert any(i["code"] == "missing_measure_field" for i in detail["issues"])

    def test_missing_connection_returns_502(self, adhoc_api, src_db):
        client, _ = adhoc_api
        r = client.post("/api/v1/bi/adhoc/query", json={
            "connection_id": str(uuid.uuid4()), "form_key": TABLE,
            "measures": [{"key": "n", "agg": "count"}],
        })
        assert r.status_code == 502

    def test_detects_calc_ref_normalization(self, adhoc_api, src_db):
        """computed 的中文引用在 API 层归一为字段键（与保存端点同一实现）"""
        client, maker = adhoc_api
        conn_id = asyncio.run(_add_conn(maker, src_db))
        schema = [
            {"name": "line", "type": "TEXT"},
            {"name": "qty", "type": "REAL"},
        ]
        with patch("app.connectors.database.get_database_connector") as fake_get, \
             patch("app.security.crypto.decrypt_db_config",
                   side_effect=lambda cfg: cfg):
            fake_connector = AsyncMock()
            fake_connector.get_table_schema = AsyncMock(return_value=[
                {**c, "nullable": True, "primary_key": False} for c in schema])
            fake_get.return_value = fake_connector
            r = client.post("/api/v1/bi/adhoc/query", json={
                "connection_id": conn_id, "form_key": TABLE, "chart_type": "number",
                "computed": [{"key": "doubled", "name": "两倍",
                              "expr": "[qty] * 2", "field_type": "number"}],
                "measures": [{"key": "两倍和", "agg": "sum", "field": "doubled",
                              "field_type": "number"}],
            })
        assert r.status_code == 200, r.text
        assert r.json()["defn"]["computed"][0]["expr"] == "[qty] * 2"
