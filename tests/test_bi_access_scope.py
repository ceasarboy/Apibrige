# -*- coding: utf-8 -*-
"""BI 访问授权与数据范围（add-bi-access-scope）测试

三层：
1. 纯函数（范围计算：行过滤/列剥离/即席注入/卡片可见性）；
2. API 身份矩阵（admin / 有效档案 / 无效 / 停用；只读白名单默认拒绝）；
3. **泄露测试**：受限令牌拉到的数据逐行满足行过滤、隐藏字段在响应里不存在、
   聚合值等于按过滤口径直算的值。
"""
import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import get_session
from app.main import app
from app.models.bi import BiAccessProfile, BiMetricDef, BiScreenSettings
from app.models.sync import ApiConnection, Base
from app.services import bi_sources
from app.services.bi_access import (
    ADMIN_VIEWER,
    Viewer,
    adhoc_hidden_field_used,
    hash_token,
    inject_row_filters,
    issue_token,
    row_filter_allows,
    scope_buckets,
    scope_defn,
    scope_metric_list,
    scope_rows,
    valid_token_format,
)
from tests.conftest import TEST_ADMIN_TOKEN

TABLE = "access_rows"
ROWS = [
    (1, "A线", "早班", 10.0),
    (2, "A线", "中班", 20.0),
    (3, "B线", "早班", 30.0),
    (4, "B线", "中班", 40.0),
    (5, "C线", "晚班", 50.0),
]


# TestClient 会把 client 级头与请求头**合并**——受限身份必须清掉管理令牌，
# 否则请求实际是 admin 身份（本轮实测踩到：所有范围断言都"看起来没生效"）
def bi_headers(token: str) -> dict[str, str]:
    return {"X-BI-Token": token, "X-Admin-Token": ""}


def make_viewer(*, row_filters=None, hidden=None, boards=None) -> Viewer:
    prof = BiAccessProfile(
        key="p1", name="一车间", token_hash=hash_token("bit_x"),
        token_prefix="bit_x", enabled=True, boards=boards or [],
        row_filters=row_filters or {}, hidden_fields=hidden or [],
    )
    return Viewer(role="profile", profile=prof, profile_key="p1", name="一车间")


DEFN = {
    "metric_key": "line_qty", "name": "产线产量",
    "dimensions": [{"name": "产线", "field": "line"}],
    "measures": [{"key": "产量", "agg": "sum", "field": "qty", "field_type": "number"},
                 {"key": "成本", "agg": "sum", "field": "cost", "field_type": "number"}],
    "chart_type": "bar",
}


# ============================================================
# 1. 纯函数
# ============================================================

class TestScopeFunctions:

    def test_token_format_and_hash(self):
        tok = issue_token()
        assert valid_token_format(tok) and tok.startswith("bit_")
        assert not valid_token_format("bit_short") and not valid_token_format(None)
        assert hash_token(tok) == hash_token(tok) and len(hash_token(tok)) == 64

    def test_row_filter_allows_requires_dimension(self):
        v = make_viewer(row_filters={"line": "A线"})
        ok, reason = row_filter_allows(DEFN, v)
        assert ok and reason == ""
        # 过滤字段不在维度里 → 不可见（桶按维度组合存储，无法在桶层隔离行）
        v2 = make_viewer(row_filters={"shift": "早班"})
        ok2, reason2 = row_filter_allows(DEFN, v2)
        assert not ok2 and reason2 == "row_filter_dimension_missing:shift"
        assert row_filter_allows(DEFN, ADMIN_VIEWER)[0] is True
        # 例外：detail 卡不走桶（实时读行后按行过滤）→ 不受"过滤字段须为维度"限制
        detail_defn = {"metric_key": "d", "chart_type": "detail", "dimensions": [],
                       "measures": []}
        assert row_filter_allows(detail_defn, make_viewer(
            row_filters={"shift": "早班"}))[0] is True

    def test_scope_buckets_filters_and_strips(self):
        buckets = [
            {"bucket_start": None, "dimensions": {"产线": "A线"}, "values": {"产量": 30.0, "成本": 7.0}},
            {"bucket_start": None, "dimensions": {"产线": "B线"}, "values": {"产量": 70.0, "成本": 9.0}},
        ]
        v = make_viewer(row_filters={"line": "A线"}, hidden=["成本"])
        out = scope_buckets(buckets, v, DEFN)
        assert len(out) == 1 and out[0]["dimensions"]["产线"] == "A线"
        assert "成本" not in out[0]["values"]           # 隐藏度量被剥离
        # 过滤字段用 field 口径（line）也能命中显示名（产线）
        v2 = make_viewer(row_filters={"产线": "B线"})
        assert [b["dimensions"]["产线"] for b in scope_buckets(buckets, v2, DEFN)] == ["B线"]

    def test_scope_defn_strips_measures_and_dims(self):
        v = make_viewer(hidden=["成本"])
        out = scope_defn(DEFN, v)
        assert [m["key"] for m in out["measures"]] == ["产量"]
        assert DEFN["measures"][1]["key"] == "成本"      # 不改入参
        assert scope_defn(DEFN, ADMIN_VIEWER) is DEFN

    def test_scope_rows_filters_and_drops_columns(self):
        rows = [{"line": "A线", "qty": 10, "cost": 1}, {"line": "B线", "qty": 30, "cost": 3}]
        v = make_viewer(row_filters={"line": "A线"}, hidden=["cost"])
        out = scope_rows(rows, v)
        assert out == [{"line": "A线", "qty": 10}]

    def test_inject_row_filters_overrides_same_field(self):
        v = make_viewer(row_filters={"line": "A线"})
        # 客户端试图放宽（传入 line in [A线, B线]）→ 被授权值替换，且不再叠加
        got = inject_row_filters(
            [{"field": "line", "op": "in", "value": ["A线", "B线"]},
             {"field": "shift", "op": "eq", "value": "早班"}], v)
        fields = {r["field"]: r for r in got}
        assert fields["line"]["op"] == "eq" and fields["line"]["value"] == "A线"
        assert fields["line"].get("source") == "access_profile"
        assert fields["shift"]["value"] == "早班"        # 客户端其它条件保留
        # 旧等值字典形态同样处理
        got2 = inject_row_filters({"line": "B线"}, v)
        assert [r for r in got2 if r["field"] == "line"][0]["value"] == "A线"

    def test_adhoc_hidden_field_detection(self):
        v = make_viewer(hidden=["成本"])
        assert adhoc_hidden_field_used(
            {"measures": [{"key": "成本", "field": "cost"}]}, v) == "成本"
        assert adhoc_hidden_field_used(
            {"dimensions": [{"name": "产线", "field": "line"}]}, v) is None
        assert adhoc_hidden_field_used(
            {"filters": [{"field": "成本", "op": "eq", "value": 1}]}, v) == "成本"

    def test_scope_metric_list_board_and_visibility(self):
        v = make_viewer(boards=["board_a"])
        by_board = {"board_a": {"line_qty"}, "board_b": {"other"}}
        visible, hidden = scope_metric_list([DEFN], v, by_board, v.boards)
        assert [d["metric_key"] for d in visible] == ["line_qty"] and not hidden
        # 摆在别的看板 → 不可见
        v2 = make_viewer(boards=["board_b"])
        visible2, hidden2 = scope_metric_list([DEFN], v2, by_board, v2.boards)
        assert not visible2 and hidden2 == ["line_qty"]

    def test_viewer_board_allowed_empty_means_all(self):
        assert make_viewer(boards=[]).board_allowed("anything") is True
        assert make_viewer(boards=["b1"]).board_allowed("b1") is True
        assert make_viewer(boards=["b1"]).board_allowed("b2") is False
        assert ADMIN_VIEWER.board_allowed("whatever") is True


# ============================================================
# 2/3. API 身份矩阵 + 泄露测试（真实 SQLite 源）
# ============================================================

@pytest.fixture
def src_db(tmp_path):
    path = tmp_path / "access_src.db"
    conn = sqlite3.connect(str(path))
    conn.execute(f"CREATE TABLE {TABLE} (id INTEGER PRIMARY KEY, line TEXT, "
                 "shift TEXT, qty REAL, cost REAL)")
    conn.executemany(f"INSERT INTO {TABLE} VALUES (?,?,?,?,?)",
                     [(*r[:3], r[3], r[3] * 0.1) for r in ROWS])
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def api_env(src_db, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/access_api.db")

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
    yield client, maker, src_db
    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


async def _seed(maker, src_path, *, boards=None, row_filters=None, hidden=None):
    """建连接 + 指标定义 + 看板摆放 + 授权档案；返回 (conn_id, token, profile_key)"""
    async with maker() as s:
        conn = ApiConnection(name="即席源", connection_type="target", protocol="sqlite",
                             db_type="sqlite", db_config={"database": str(src_path)},
                             status="active")
        s.add(conn)
        await s.commit()
        await s.refresh(conn)
        conn_id = str(conn.id)
        s.add(BiMetricDef(
            metric_key="line_qty", name="产线产量", source_type="form",
            source_ref=json.dumps({"connection_id": conn_id, "form_key": TABLE}),
            granularity="1min", chart_type="bar", top_n=100, enabled=True,
            dimensions=[{"name": "产线", "field": "line"}],
            measures=[{"key": "产量", "agg": "sum", "field": "qty", "field_type": "number"},
                      {"key": "成本", "agg": "sum", "field": "cost", "field_type": "number"}],
            computed=[]))
        s.add(BiMetricDef(
            metric_key="shift_qty", name="班次产量", source_type="form",
            source_ref=json.dumps({"connection_id": conn_id, "form_key": TABLE}),
            granularity="1min", chart_type="bar", top_n=100, enabled=True,
            dimensions=[{"name": "班次", "field": "shift"}],
            measures=[{"key": "产量", "agg": "sum", "field": "qty", "field_type": "number"}],
            computed=[]))
        s.add(BiScreenSettings(id="board_a", data={"name": "一车间看板",
                                                   "cells": {"line_qty": {"x": 0, "y": 0, "w": 36, "h": 8},
                                                             "shift_qty": {"x": 0, "y": 8, "w": 36, "h": 8}}}))
        s.add(BiScreenSettings(id="board_b", data={"name": "别处看板",
                                                   "cells": {"shift_qty": {"x": 0, "y": 0, "w": 36, "h": 8}}}))
        token = issue_token()
        s.add(BiAccessProfile(key="p1", name="一车间", token_hash=hash_token(token),
                              token_prefix=token[:12], enabled=True,
                              boards=boards or [], row_filters=row_filters or {},
                              hidden_fields=hidden or []))
        await s.commit()
        return conn_id, token, "p1"


def _run_metric(client, key, token: str | None = None):
    """聚合用管理令牌触发（写操作），查询按给定身份（None=管理令牌）"""
    bi_sources._form_cache.clear()
    client.post("/api/v1/bi/aggregate/run", params={"metric": key})
    headers = bi_headers(token) if token else None
    return client.get(f"/api/v1/bi/metrics/{key}", params={"format": "raw"},
                      headers=headers)


class TestIdentityMatrix:

    def test_profile_token_only_readonly_allowlist(self, api_env):
        client, maker, src = api_env
        _, token, _ = asyncio.run(_seed(maker, src, hidden=["hidden_col"]))
        hdr = bi_headers(token)
        assert client.get("/api/v1/bi/screens", headers=hdr).status_code == 200
        assert client.get("/api/v1/bi/metrics", headers=hdr).status_code == 200
        # /status 只回启用开关，必须放行：前端 BiGate 每次进 BI 页面都先问它，
        # 403 会让受限身份（投放链接）卡在"无法读取 BI 状态"（浏览器实测抓到）
        status_resp = client.get("/api/v1/bi/status", headers=hdr)
        assert status_resp.status_code == 200, status_resp.text
        assert status_resp.json()["enabled"] is True
        # P0 修复回归：computed 表达式引用隐藏字段 → 403（不能借派生列绕列隐藏）
        adhoc = client.post("/api/v1/bi/adhoc/query", headers=hdr, json={
            "connection_id": str(src), "form_key": TABLE, "chart_type": "bar",
            "computed": [{"key": "c1", "name": "c1", "expr": "[hidden_col]",
                          "field_type": "number"}],
            "dimensions": [{"name": "x", "field": "c1"}],
            "measures": [{"key": "n", "agg": "count"}]})
        assert adhoc.status_code == 403
        # 命中返回的是派生列 key（c1）——口径以端点实际返回为准
        assert "c1" in adhoc.json()["detail"]["message"]
        # 写端点与其它端点一律 403（默认拒绝）
        assert client.post("/api/v1/bi/metrics", headers=hdr,
                           json={"metric_key": "x", "source_type": "form"}).status_code == 403
        assert client.post("/api/v1/bi/aggregate/run", headers=hdr).status_code == 403
        assert client.get("/api/v1/bi/access/profiles", headers=hdr).status_code == 403
        assert client.get("/api/v1/connections", headers=hdr).status_code == 401
        # adhoc 是 POST 但语义只读 → 放行
        r = client.post("/api/v1/bi/adhoc/query", headers=hdr, json={
            "connection_id": json.loads(client.get("/api/v1/bi/metrics", headers=hdr)
                                        .json()[0]["source_ref"])["connection_id"],
            "form_key": TABLE, "chart_type": "bar", "format": "raw",
            "dimensions": [{"name": "班次", "field": "shift"}],
            "measures": [{"key": "行数", "agg": "count"}]})
        assert r.status_code == 200, r.text

    def test_empty_board_placement_fail_closed(self, api_env):
        """P1 回归：授权看板一张卡都没摆（或看板不存在）→ 什么都不可见（fail-closed）"""
        client, maker, src = api_env
        _, token, _ = asyncio.run(_seed(maker, src, boards=["board_empty"]))
        hdr = bi_headers(token)
        assert client.get("/api/v1/bi/metrics/line_qty", headers=hdr).status_code == 404
        assert client.get("/api/v1/bi/metrics/shift_qty", headers=hdr).status_code == 404

    def test_invalid_and_disabled_token_401(self, api_env):
        client, maker, src = api_env
        _, token, key = asyncio.run(_seed(maker, src))
        assert client.get("/api/v1/bi/screens",
                          headers=bi_headers("bit_" + "0" * 32)).status_code == 401
        # 停用 → 立即 401
        client.put(f"/api/v1/bi/access/profiles/{key}", json={"enabled": False})
        r = client.get("/api/v1/bi/screens", headers=bi_headers(token))
        assert r.status_code == 401 and r.json()["detail"]["code"] == "bi_token_disabled"
        # 轮换 → 旧令牌失效
        client.put(f"/api/v1/bi/access/profiles/{key}", json={"enabled": True})
        new_token = client.post(f"/api/v1/bi/access/profiles/{key}/rotate").json()["token"]
        assert client.get("/api/v1/bi/screens",
                          headers=bi_headers(token)).status_code == 401
        assert client.get("/api/v1/bi/screens",
                          headers={"X-BI-Token": new_token}).status_code == 200

    def test_admin_unaffected(self, api_env):
        client, maker, src = api_env
        asyncio.run(_seed(maker, src))
        assert len(client.get("/api/v1/bi/screens").json()) == 3   # default + board_a + board_b
        assert client.get("/api/v1/bi/access/profiles").status_code == 200

    def test_profile_crud_never_returns_hash(self, api_env):
        client, _, _ = api_env
        r = client.post("/api/v1/bi/access/profiles", json={
            "name": "供应商A", "boards": [], "row_filters": {"line": "A线"},
            "hidden_fields": ["成本"], "note": "来料"})
        assert r.status_code == 201
        body = r.json()
        assert body["token"].startswith("bit_") and "token_hash" not in body
        assert body["row_filters"] == {"line": "A线"}
        # 列表不回显明文
        listed = client.get("/api/v1/bi/access/profiles").json()
        assert all("token" not in p and "token_hash" not in p for p in listed)


class TestScopeEnforcement:

    def test_board_scope(self, api_env):
        client, maker, src = api_env
        _, token, _ = asyncio.run(_seed(maker, src, boards=["board_a"]))
        hdr = bi_headers(token)
        boards = [b["key"] for b in client.get("/api/v1/bi/screens", headers=hdr).json()]
        assert boards == ["board_a"]
        assert client.get("/api/v1/bi/screen-settings", headers=hdr,
                          params={"board": "board_a"}).status_code == 200
        r = client.get("/api/v1/bi/screen-settings", headers=hdr,
                       params={"board": "board_b"})
        assert r.status_code == 404 and r.json()["detail"]["code"] == "not_visible"

    def test_metric_list_and_cells_scoped(self, api_env):
        client, maker, src = api_env
        _, token, _ = asyncio.run(_seed(maker, src, boards=["board_a"],
                                        row_filters={"line": "A线"}, hidden=["成本"]))
        hdr = bi_headers(token)
        keys = [m["metric_key"] for m in client.get("/api/v1/bi/metrics", headers=hdr).json()]
        # line_qty 行过滤维度齐备 → 可见；shift_qty 缺 line 维度 → 不可见
        assert keys == ["line_qty"]
        cells = client.get("/api/v1/bi/screen-settings", headers=hdr,
                           params={"board": "board_a"}).json()["cells"]
        assert set(cells) == {"line_qty"}
        defn = client.get("/api/v1/bi/metrics", headers=hdr).json()[0]
        assert [m["key"] for m in defn["measures"]] == ["产量"]     # 成本被剥离

    def test_bucket_values_filtered_row_wise(self, api_env):
        """泄露测试：桶查询只返回授权行的聚合，且与按口径直算一致"""
        client, maker, src = api_env
        conn_id, token, _ = asyncio.run(_seed(maker, src, row_filters={"line": "A线"}))
        hdr = bi_headers(token)
        data = _run_metric(client, "line_qty", token).json()["data"]
        got = dict(zip(data["x"], data["series"][0]["data"]))
        truth = dict(sqlite3.connect(str(src)).execute(
            f"SELECT line, SUM(qty) FROM {TABLE} WHERE line = ? GROUP BY line", ("A线",)))
        assert got == truth == {"A线": 30.0}

    def test_detail_rows_filtered_and_columns_dropped(self, api_env):
        """泄露测试：明细每行都满足行过滤；隐藏列在响应里不存在"""
        client, maker, src = api_env
        conn_id, token, _ = asyncio.run(_seed(maker, src, row_filters={"line": "A线"},
                                              hidden=["cost"]))
        hdr = bi_headers(token)
        body = client.get("/api/v1/bi/form-rows", headers=hdr, params={
            "connection_id": conn_id, "form_key": TABLE, "page": 1,
            "page_size": 100}).json()
        assert body["fallback"] == "row_permission" and body["total"] == 2
        for row in body["rows"]:
            assert row["line"] == "A线" and "cost" not in row
        # 不带 page 的旧路径同样受限
        rows = client.get("/api/v1/bi/form-rows", headers=hdr, params={
            "connection_id": conn_id, "form_key": TABLE, "limit": 50}).json()["rows"]
        assert len(rows) == 2 and all(r["line"] == "A线" for r in rows)

    def test_form_fields_hides_columns(self, api_env):
        client, maker, src = api_env
        conn_id, token, _ = asyncio.run(_seed(maker, src, hidden=["cost"]))
        schema = [{"name": n, "type": "TEXT"} for n in ("id", "line", "shift", "qty", "cost")]
        with patch("app.connectors.database.get_database_connector") as fake_get, \
             patch("app.security.crypto.decrypt_db_config", side_effect=lambda cfg: cfg):
            fake = AsyncMock()
            fake.get_table_schema = AsyncMock(return_value=[
                {**c, "nullable": True, "primary_key": False} for c in schema])
            fake_get.return_value = fake
            fields = client.get("/api/v1/bi/form-fields", headers=bi_headers(token),
                                params={"connection_id": conn_id, "form_key": TABLE}
                                ).json()["fields"]
        assert "cost" not in {f["path"] for f in fields}

    def test_adhoc_inject_and_hidden_403(self, api_env):
        client, maker, src = api_env
        conn_id, token, _ = asyncio.run(_seed(maker, src, row_filters={"line": "A线"},
                                              hidden=["成本"]))
        hdr = bi_headers(token)
        # 客户端试图放宽（in [A线, B线]）→ 授权值覆盖，结果只含 A 线
        r = client.post("/api/v1/bi/adhoc/query", headers=hdr, json={
            "connection_id": conn_id, "form_key": TABLE, "chart_type": "bar",
            "format": "raw",
            "dimensions": [{"name": "产线", "field": "line"}],
            "measures": [{"key": "产量", "agg": "sum", "field": "qty",
                          "field_type": "number"}],
            "filters": [{"field": "line", "op": "in", "value": ["A线", "B线"]}]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["data"]["x"] == ["A线"] and body["data"]["series"][0]["data"] == [30.0]
        # 引用隐藏度量 → 403
        r2 = client.post("/api/v1/bi/adhoc/query", headers=hdr, json={
            "connection_id": conn_id, "form_key": TABLE, "chart_type": "bar",
            "dimensions": [{"name": "产线", "field": "line"}],
            "measures": [{"key": "成本", "agg": "sum", "field": "cost",
                          "field_type": "number"}]})
        assert r2.status_code == 403
        assert r2.json()["detail"]["code"] == "hidden_field"

    def test_detail_metric_endpoint_scoped(self, api_env):
        """detail 卡不受"过滤字段须为维度"限制：实时读行后按行过滤"""
        client, maker, src = api_env
        conn_id, token, _ = asyncio.run(_seed(maker, src, row_filters={"line": "B线"}))
        client.post("/api/v1/bi/metrics", json={
            "metric_key": "line_detail", "name": "产线明细", "source_type": "form",
            "source_ref": json.dumps({"connection_id": conn_id, "form_key": TABLE}),
            "chart_type": "detail", "dimensions": [], "measures": [], "enabled": True})
        rows = client.get("/api/v1/bi/metrics/line_detail",
                          headers=bi_headers(token),
                          params={"format": "bi-detail"}).json()["data"]["rows"]
        assert len(rows) == 2 and all(r["line"] == "B线" for r in rows)
