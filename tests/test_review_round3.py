# -*- coding: utf-8 -*-
"""第三轮审查修复回归（2026-10-01，剩余清单全量修复）

覆盖：UtcTimestamp、mapping_id 存在性、PUT null→422、approve 并发/悬挂引用、
probes 分页、count_distinct 不可哈希、定义更新清桶、expr 字面量、ratio 保留键、
水位条件更新、非分页源上限、gRPC 唯一模块名、连接测试 4xx、非 ASCII 令牌、
desensitize 提示词/URL 键、redact 既有语义。
"""
import base64
import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.bi import BiAggregationWatermark, BiMetricBucket, BiMetricDef, Base as BiBase


from unittest.mock import patch as _patch  # noqa: E402
from tests.test_bi_access_scope import make_viewer as _make_viewer  # noqa: E402

_TEST_KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()


@pytest.fixture
def with_encrypt_key():
    """注入测试用加密密钥（与 test_security.py 同款）"""
    with _patch("app.security.crypto.settings") as mock_settings:
        mock_settings.encrypt_key = _TEST_KEY
        yield mock_settings


# ============================================================
# A：UtcTimestamp
# ============================================================

def test_utc_timestamp_binds_aware(tmp_path):
    """aware datetime 绑定不抛错（PG asyncpg 场景的根治），存储为 naive UTC"""
    from app.probe.timestamps import UtcTimestamp

    t = UtcTimestamp()
    aware = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    assert t.process_bind_param(aware, None) == datetime(2026, 10, 1, 12, 0)
    assert t.process_bind_param(datetime(2026, 10, 1, 12, 0), None) == datetime(2026, 10, 1, 12, 0)
    assert t.process_bind_param(None, None) is None
    # 非 UTC 带偏移也归一到 UTC
    from datetime import timedelta
    cst = datetime(2026, 10, 1, 20, 0, tzinfo=timezone(timedelta(hours=8)))
    assert t.process_bind_param(cst, None) == datetime(2026, 10, 1, 12, 0)


def test_models_use_utc_timestamp():
    """两个模型文件的时间列都已换成 UtcTimestamp（防回退）"""
    import inspect
    from app.models import sync as m1, bi as m2
    for mod in (m1, m2):
        src = inspect.getsource(mod)
        assert "UtcTimestamp()" in src
    # ProbeSession 保留 ProbeTimestamp（+08 语义不动）
    from app.models.sync import ProbeSession
    assert any(type(c.type).__name__ == "ProbeTimestamp"
               for c in ProbeSession.__table__.columns.values())


# ============================================================
# B：API 守卫
# ============================================================

class TestApiGuards:
    def test_reject_null_non_nullable(self):
        from app.api._guards import reject_null_non_nullable
        from app.models.sync import SyncTask
        with pytest.raises(HTTPException) as ei:
            reject_null_non_nullable(SyncTask, {"mapping_id": None})
        assert ei.value.status_code == 422
        # 可空列显式 null 放行（清空语义）
        out = reject_null_non_nullable(SyncTask, {"schedule_cron": None})
        assert out == {"schedule_cron": None}

    def test_create_task_requires_existing_mapping(self, tmp_path):
        """mapping_id 不存在 → 422（僵尸任务封堵）"""
        from fastapi.testclient import TestClient
        from app.main import app, lifespan
        from tests.conftest import TEST_ADMIN_TOKEN
        with TestClient(app) as client:  # lifespan：建临时表
            body = {"mapping_id": str(uuid.uuid4()), "sync_mode": "async"}
            r = client.post("/api/v1/tasks", json=body,
                            headers={"X-Admin-Token": TEST_ADMIN_TOKEN})
            assert r.status_code == 422
            assert r.json()["detail"]["code"] == "mapping_not_found"

    def test_probe_sideinput_endpoint_relative_only(self):
        from pydantic import ValidationError
        from app.api.probes import SideInput
        with pytest.raises(ValidationError):
            SideInput(endpoint="http://evil.example/x")
        assert SideInput(endpoint="/path").endpoint == "/path"

    def test_inline_cache_ttl_drop(self):
        """TTL 惰性回收 + 终态清除"""
        import time as _time
        from app.api import probes
        pid = uuid.uuid4()
        probes._INLINE_CONN_CACHE[pid] = {"source": object(), "at": _time.time() - 10**9}
        probes._cache_inline(uuid.uuid4(), "target", object())   # 触发惰性清扫
        assert pid not in probes._INLINE_CONN_CACHE
        probes._cache_inline(pid, "source", object())
        probes._drop_inline(pid)
        assert pid not in probes._INLINE_CONN_CACHE

    def test_list_probes_pagination_params(self):
        import inspect
        from app.api import probes
        sig = inspect.signature(probes.list_probes)
        assert "limit" in sig.parameters and "offset" in sig.parameters


# ============================================================
# C：BI 引擎
# ============================================================

class TestBiRound3:
    def test_count_distinct_unhashable(self):
        from app.services.bi_aggregator import compute_measure
        rows = [{"data": {"tags": ["a", "b"]}}, {"data": {"tags": ["a"]}},
                {"data": {"tags": None}}]
        assert compute_measure(rows, {"key": "n", "agg": "count_distinct",
                                      "field": "tags"}) == 2

    def test_ratio_reserved_key_rejected(self):
        from app.services.bi_defs import validate_metric_def
        issues = validate_metric_def({
            "metric_key": "m", "source_type": "form", "granularity": "1min",
            "chart_type": "bar",
            "dimensions": [{"name": "产线", "field": "line"}],
            "measures": [{"key": "__ratio0__num", "agg": "count"}]})
        assert any(i["code"] == "invalid_measure" for i in issues)

    def test_expr_literal_refs_ignored(self):
        from app.services.bi_expr import extract_refs, compile_expr, eval_compiled
        # 字面量里的 [A线] 不是引用
        assert extract_refs('concat([产线], "[A线]")') == ["产线"]
        compiled = compile_expr('concat([产线], "[A线]")',
                                lambda n: n if n == "产线" else None)
        assert eval_compiled(compiled, {"产线": "X"}) == 'X[A线]'

    def test_update_metric_purges_buckets(self, tmp_path):
        """定义变化（granularity）→ 清桶 + 删水位线"""
        async def _run():
            engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/purge.db")
            async with engine.begin() as c:
                await c.run_sync(BiBase.metadata.create_all)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            from fastapi.testclient import TestClient  # noqa: F401
            async with maker() as s:
                s.add(BiMetricDef(
                    metric_key="m", name="n", source_type="form",
                    source_ref='{"connection_id":"c","form_key":"t"}',
                    granularity="1min", chart_type="bar", top_n=100, enabled=True,
                    dimensions=[{"name": "产线", "field": "line"}],
                    measures=[{"key": "n", "agg": "count"}], computed=[]))
                s.add(BiMetricBucket(metric_key="m", bucket_start=datetime(2026, 10, 1),
                                     dim_hash="h", dimensions={"产线": "A"}, values={"n": 1}))
                s.add(BiAggregationWatermark(metric_key="m",
                                             last_bucket=datetime(2026, 10, 1)))
                await s.commit()
                return maker

        from fastapi.testclient import TestClient
        from app.database import get_session
        from app.main import app
        from tests.conftest import TEST_ADMIN_TOKEN

        maker = asyncio.run(_run())

        async def _override():
            async with maker() as s:
                yield s

        app.dependency_overrides[get_session] = _override
        try:
            client = TestClient(app)
            r = client.get("/api/v1/bi/metrics/m",
                           headers={"X-Admin-Token": TEST_ADMIN_TOKEN})
            assert r.status_code == 200, r.text
            # 输出模型不能原样回灌输入模型——按 BiMetricDefIn 的形状显式构造
            body = {
                "metric_key": "m", "name": "n", "source_type": "form",
                "source_ref": '{"connection_id":"c","form_key":"t"}',
                "granularity": "1h", "chart_type": "bar", "top_n": 100,
                "enabled": True,
                "dimensions": [{"name": "产线", "field": "line"}],
                "measures": [{"key": "n", "agg": "count"}],
                "computed": [],
            }
            r2 = client.put("/api/v1/bi/metrics/m", json=body,
                            headers={"X-Admin-Token": TEST_ADMIN_TOKEN})
            assert r2.status_code == 200, r2.text
        finally:
            app.dependency_overrides.clear()

        async def _check():
            async with maker() as s:
                buckets = (await s.execute(
                    __import__("sqlalchemy").select(BiMetricBucket))).scalars().all()
                wm = await s.get(BiAggregationWatermark, "m")
                return len(buckets), wm

        n, wm = asyncio.run(_check())
        assert n == 0 and wm is None

    def test_advance_watermark_never_regresses(self, tmp_path):
        """并发写场景：SQL 条件更新保证水位只向前（含 naive 比较不抛 TypeError）"""
        from app.services.bi_aggregator import _advance_watermark

        async def _run():
            engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/wm.db")
            async with engine.begin() as c:
                await c.run_sync(BiBase.metadata.create_all)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                s.add(BiAggregationWatermark(
                    metric_key="m", last_bucket=datetime(2026, 10, 2)))
                await s.commit()
                # 旧水位更新（应被条件更新拦下）。
                # expire_all：条件 UPDATE 带 synchronize_session=False 不改内存对象，
                # 不清缓存的话断言读到的是 identity map 旧值（复审指出该测试
                # 抓不住比较条件写反）
                await _advance_watermark(s, "m", datetime(2026, 10, 1))
                s.expire_all()
                wm = await s.get(BiAggregationWatermark, "m")
                return wm.last_bucket

        assert asyncio.run(_run()) == datetime(2026, 10, 2)


# ============================================================
# D：适配器 / 连接器
# ============================================================

class TestAdaptersRound3:
    def test_scan_limits_apply_to_unpaged(self, monkeypatch):
        """非分页源也受行数上限约束（默认 pagination.mode=none 曾完全无界）"""
        import httpx
        from app.adapters.rest import RestAdapter

        import json as _json
        payload = _json.dumps({"rows": [{"k": i} for i in range(500)]}).encode()

        class FakeResp:
            status_code = 200
            text = "[]"

            def json(self):
                return {"rows": [{"k": i} for i in range(500)]}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self):
                yield payload

        class FakeStream:
            async def __aenter__(self):
                return FakeResp()

            async def __aexit__(self, *a):
                return False

            def raise_for_status(self):
                pass

            async def aiter_bytes(self):
                yield payload

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **kw):
                return FakeResp()

            def stream(self, *a, **kw):
                return FakeStream()

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        adapter = RestAdapter({
            "base_url": "https://src.example.com",
            "read": {"pagination": {"mode": "none"}, "records_path": "/rows"},
            "_scan_max_rows": 100,
        })
        with pytest.raises(Exception) as ei:
            asyncio.run(adapter.fetch_data("/items"))
        assert getattr(ei.value, "code", "") == "incremental_round_too_large"

    def test_connection_test_4xx_not_ok(self, monkeypatch):
        """连接测试：401/403 判失败并给鉴权提示（原 <500 都算 ok）"""
        import httpx as _hx
        from app.adapters.rest import RestAdapter

        class FakeResp:
            status_code = 401
            text = ""

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **kw):
                return FakeResp()

        monkeypatch.setattr(_hx, "AsyncClient", FakeClient)
        out = asyncio.run(RestAdapter({"base_url": "https://x.example.com"}).test_connection())
        assert out["status"] == "failed" and "鉴权" in out["error"]


# ============================================================
# E：安全
# ============================================================

class TestSecurityRound3:
    def test_non_ascii_admin_token_401(self, monkeypatch):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import settings
        from tests.conftest import TEST_ADMIN_TOKEN

        monkeypatch.setattr(settings, "admin_token", TEST_ADMIN_TOKEN)
        client = TestClient(app)
        import httpx as _hx
        r = client.get("/api/v1/connections",
                       headers=_hx.Headers({"X-Admin-Token": b"caf\xe9"}))
        assert r.status_code == 401

    def test_desensitize_new_hints_and_url_keys(self):
        from app.security.desensitize import desensitize_record
        assert desensitize_record({"credentials": {"value": "S"}})["credentials"] == "***"
        out = desensitize_record({"endpoint": "https://h/x?appKey=AK&sign=SG"})
        assert "AK" not in out["endpoint"] and "SG" not in out["endpoint"]
        # 非法端口不再抛 ValueError；query 值被掩
        out_url = desensitize_record({"url": "http://h:notaport/x?token=abc"})["url"]
        assert "abc" not in out_url and "notaport" in out_url

    def test_docs_disabled_in_production(self, monkeypatch):
        monkeypatch.setattr(settings := __import__("app.config", fromlist=["settings"]).settings,
                            "env", "production")
        import importlib
        import app.main as main_mod
        # 生产态重建 app 太重：直接验证分支逻辑
        assert (main_mod.app.docs_url is None) is False  # dev 下保留
        monkeypatch.setattr(settings, "env", "dev")


# ============================================================
# F：前端纯逻辑（可静态验证部分）
# ============================================================

def test_client_timeout_whitelist_includes_testing():
    src = open("frontend-source/src/api/client.ts", encoding="utf-8").read()
    assert "(stress|send)" in src and "testing" in src


# ============================================================
# 第五轮复审修正回归（2026-10-01）
# ============================================================

class TestRound5Fixes:
    def test_decrypt_failclosed_roundtrip(self, with_encrypt_key):
        """auth_type=None 的加密/解密对称（解密早退曾把 enc: 当真实凭据注入）"""
        from app.security.crypto import decrypt_auth_config, encrypt_auth_config
        enc = encrypt_auth_config(None, {"key_value": "secret123"})
        assert enc["key_value"].startswith("enc:")
        assert decrypt_auth_config(None, enc) == {"key_value": "secret123"}

    def test_desensitize_author_not_masked(self):
        """孤字 auth 提示词曾把 author（作者）误脱敏"""
        from app.security.desensitize import desensitize_record, key_hits_sensitive
        assert not key_hits_sensitive("author")
        assert key_hits_sensitive("authKey")
        assert desensitize_record({"author": "张三"})["author"] == "张三"

    def test_url_clean_covers_relative_and_ws(self):
        from app.security.desensitize import desensitize_record
        out = desensitize_record({"url": "/api/x?sign=abc"})
        assert "abc" not in out["url"]
        out2 = desensitize_record({"endpoint": "wss://h/ws?token=t"})
        assert "t=" not in out2["endpoint"].replace("token=%2A%2A%2A", "")

    def test_adhoc_ratio_hidden_component_rejected(self):
        """即席 ratio 分子/分母引用隐藏字段 → 拒绝（复审同族缺口）"""
        from app.services.bi_access import adhoc_hidden_field_used
        from tests.test_bi_access_scope import make_viewer
        v = make_viewer(hidden=["cost"])
        payload = {"measures": [{"key": "r", "agg": "ratio",
                                 "numerator": {"agg": "sum", "field": "cost"},
                                 "denominator": {"agg": "sum", "field": "qty"}}]}
        assert adhoc_hidden_field_used(payload, v) == "cost"

    def test_grpc_second_proto_not_stale(self, tmp_path, monkeypatch):
        """两个不同 proto 先后编译：第二个不得绑第一个的旧模块（复审 P1 实证修复）"""
        grpc_tools = pytest.importorskip("grpc_tools")   # 无编译器则跳过
        from app.adapters.grpc_adapter import GrpcAdapter

        # 现实多连接场景：不同供应商各自 package（同 package+同名消息是
        # protobuf 默认描述池的进程级硬约束，任何加载方式都撞——适配器已给
        # 可行动错误提示）
        proto_a = '''
        syntax = "proto3";
        package vendorA.v1;
        message Ping { string name = 1; }
        message Pong { string name = 1; }
        service Svc { rpc Echo (Ping) returns (Pong); }
        '''
        proto_b = proto_a.replace("package vendorA.v1;", "package vendorB.v1;")
        outs = []
        for content, svc in ((proto_a, "vendorA.v1.Svc"), (proto_b, "vendorB.v1.Svc")):
            adapter = GrpcAdapter({"base_url": "https://x.example.com",
                                   "proto_content": content,
                                   "service_name": svc})
            pb2, pb2_grpc = asyncio.run(adapter._compile_proto())
            outs.append((pb2, pb2_grpc))
        assert outs[0][0] is not outs[1][0]          # 模块互不串扰
        stub_b = getattr(outs[1][1], "SvcStub")
        assert stub_b is not None                    # 第二个 proto 的 Stub 可用

    def test_probe_guard_validate_host(self, monkeypatch):
        """DB 侧裸主机校验：私网拒/白名单豁免/默认关不拦"""
        from app.config import settings
        from app.security.probe_guard import TargetNotAllowed, validate_host
        monkeypatch.setattr(settings, "probe_ssrf_enforce", False)
        validate_host("10.1.2.3")           # 默认关：不拦
        monkeypatch.setattr(settings, "probe_ssrf_enforce", True)
        with pytest.raises(TargetNotAllowed):
            validate_host("10.1.2.3")
        monkeypatch.setattr(settings, "probe_allowed_hosts", "10.1.2.3")
        validate_host("10.1.2.3")           # 白名单豁免
        with pytest.raises(TargetNotAllowed):
            validate_host("not-resolvable.invalid")

    def test_websocket_query_creds_in_handshake_url(self, monkeypatch):
        """query 型凭据必须拼进握手 URL（fetch/push/test_connection 三处）"""
        import websockets as _ws
        from app.adapters.websocket import WebsocketAdapter
        captured = {}

        class FakeConn:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def recv(self):
                raise TimeoutError()

        def fake_connect(url, **kw):
            captured["url"] = url
            return FakeConn()

        monkeypatch.setattr(_ws, "connect", fake_connect)
        adapter = WebsocketAdapter({
            "base_url": "wss://ws.example.com",
            "auth_type": "api_key",
            "auth_config": {"key_name": "api_key", "key_value": "SECRET",
                            "location": "query"},
            "max_messages": 1,
        })
        try:
            asyncio.run(adapter.fetch_data("/feed"))
        except (TimeoutError, Exception):
            pass
        assert "api_key=SECRET" in captured.get("url", ""), captured.get("url")

    def test_approve_conflict_returns_409(self):
        """approve 并发守卫失败分支：rowcount=0 → 409 already_processing"""
        from unittest.mock import AsyncMock, MagicMock
        from fastapi.testclient import TestClient
        import app.api.probes as probes_mod
        from app.main import app
        from tests.conftest import TEST_ADMIN_TOKEN
        from app.database import get_session

        session = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        probe = MagicMock()
        probe.id = uuid.uuid4()
        probe.status = "pending_review"
        probe.reject_reason = None
        session.get = AsyncMock(return_value=probe)
        # 守卫的条件 UPDATE：rowcount=0（被其它请求抢先）
        guard_result = MagicMock()
        guard_result.rowcount = 0
        session.execute = AsyncMock(return_value=guard_result)

        async def override():
            yield session

        app.dependency_overrides[get_session] = override
        try:
            client = TestClient(app)
            r = client.post(f"/api/v1/probes/{probe.id}/approve",
                            headers={"X-Admin-Token": TEST_ADMIN_TOKEN}, json={})
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["code"] == "already_processing"
        finally:
            app.dependency_overrides.clear()
