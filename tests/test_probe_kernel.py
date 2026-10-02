"""探针内核单测（task 3.16）

覆盖：状态机（3.1）、Schema 推断（3.4）、草案校验/退化匹配（3.5/3.6）、
样本脱敏（3.11）、L2 零写入与 no_contract 降级（3.7/3.14）、L3 授权闸门（3.10）、
HTTP 面六端点全链路（3.15）。不依赖 Redis/PG 之外的外部服务。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.sync import Base, ProbeSession
from app.probe.mapping_draft import (
    generate_degenerate_draft,
    types_compatible,
    validate_draft,
)
from app.probe.schema_infer import infer_schema
from app.probe.session import (
    InvalidTransition,
    ensure_expires_at,
    expire_stale_sessions,
    transition,
)
from app.security.desensitize import desensitize_record


# ============================================================
# task 3.1：状态机
# ============================================================

def _fresh_session(status="draft"):
    probe = ProbeSession(status=status)
    ensure_expires_at(probe)
    return probe


class TestStateMachine:
    def test_legal_transition_records_audit(self):
        probe = _fresh_session()
        transition(probe, "probing", via="cli", summary="L1")
        assert probe.status == "probing"
        assert probe.audit_log[-1]["to"] == "probing"
        assert probe.audit_log[-1]["via"] == "cli"

    def test_illegal_transition_rejected_and_kept(self):
        """task 3.1：draft 不能直接跳 pending_review，非法迁移被拒并保持原状态"""
        probe = _fresh_session("draft")
        with pytest.raises(InvalidTransition):
            transition(probe, "pending_review")
        assert probe.status == "draft"

    def test_no_contract_cannot_review(self):
        """task 3.14：L2_partial_no_contract 不得进入 pending_review"""
        probe = _fresh_session("L2_partial_no_contract")
        with pytest.raises(InvalidTransition):
            transition(probe, "pending_review")

    def test_expired_after_ttl(self):
        """task 3.12：超过有效期未进清单 → expired"""
        from datetime import datetime, timedelta, timezone

        probe = _fresh_session("L1_reachable")
        probe.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        assert probe.expires_at < datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_expire_stale_sessions(self):
        """task 3.12：过期清扫把超期会话标记 expired"""
        from datetime import datetime, timedelta, timezone

        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        from sqlalchemy.ext.asyncio import async_sessionmaker
        maker = async_sessionmaker(engine, expire_on_commit=False)

        stale = _fresh_session("L1_reachable")
        stale.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        fresh = _fresh_session("L1_reachable")
        in_review = _fresh_session("pending_review")
        in_review.expires_at = datetime.now(timezone.utc) - timedelta(days=1)

        async with maker() as session:
            session.add_all([stale, fresh, in_review])
            await session.commit()
            count = await expire_stale_sessions(session)
            assert count == 1
            await session.refresh(stale)
            assert stale.status == "expired"
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_expire_stale_sessions_from_db_read(self):
        """回归（2026-09-29 浏览器实测抓到 500）：**从库里读回**的时间戳是 naive

        SQLite 的 TIMESTAMP 回读为 naive datetime，而清扫用的 now 是 aware——
        Python 侧复检 `expires_at < now` 会抛
        `TypeError: can't compare offset-naive and offset-aware datetimes`，
        导致待确认清单页整页 500。旧用例把对象留在 identity map 里所以从未暴露；
        本用例提交后**换一个新 session 读回**，复现真实条件。
        """
        from datetime import datetime, timedelta, timezone

        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        from sqlalchemy.ext.asyncio import async_sessionmaker
        maker = async_sessionmaker(engine, expire_on_commit=False)

        stale = _fresh_session("L1_reachable")
        stale.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        async with maker() as s1:
            s1.add(stale)
            await s1.commit()

        async with maker() as s2:                      # 新 session → 值来自库（naive）
            count = await expire_stale_sessions(s2)
            assert count == 1
        async with maker() as s3:
            again = await s3.get(type(stale), stale.id)
            assert again.status == "expired"
        await engine.dispose()


# ============================================================
# task 3.4：Schema 推断
# ============================================================

class TestSchemaInfer:
    def test_nested_dot_paths(self):
        sample = {"data": {"order": {"customer": {"name": "张三"}, "id": 1}}}
        fields = {f["path"]: f for f in infer_schema(sample)}
        assert "data.order.customer.name" in fields
        assert fields["data.order.id"]["type"] == "integer"

    def test_array_items_marker(self):
        """task 3.4：数组元素以 items[].field 形式表达"""
        sample = {"items": [{"name": "a", "qty": 1}, {"name": "b", "qty": 2}]}
        fields = {f["path"]: f for f in infer_schema(sample)}
        assert "items[].name" in fields
        assert "items[].qty" in fields
        assert fields["items[].name"]["is_array"] is True

    def test_nullable_detected(self):
        sample = [{"remark": None}, {"remark": "ok"}]
        fields = {f["path"]: f for f in infer_schema(sample)}
        assert fields["remark"]["nullable"] is True


# ============================================================
# task 3.5/3.6：草案校验与退化匹配
# ============================================================

SOURCE_SCHEMA = [
    {"path": "user_name", "type": "string", "nullable": False, "is_array": False},
    {"path": "age", "type": "integer", "nullable": False, "is_array": False},
    {"path": "tags", "type": "string", "nullable": True, "is_array": True},
]
TARGET_SCHEMA = [
    {"name": "customer_name", "type": "varchar", "nullable": False, "primary_key": False},
    {"name": "age", "type": "integer", "nullable": False, "primary_key": True},
    {"name": "remark", "type": "text", "nullable": True, "primary_key": False},
]


class TestMappingDraft:
    def test_unknown_source_field_rejected(self):
        """task 3.5：引用不存在的源字段 → unknown_source_field 且不入草案"""
        result = validate_draft(
            SOURCE_SCHEMA, TARGET_SCHEMA,
            [{"source": "no_such_field", "target": "customer_name"}],
        )
        assert result["valid"] is False
        assert result["issues"][0]["code"] == "unknown_source_field"
        assert result["fields"] == []

    def test_unknown_target_field_rejected(self):
        result = validate_draft(
            SOURCE_SCHEMA, TARGET_SCHEMA,
            [{"source": "user_name", "target": "no_such_column"}],
        )
        assert result["issues"][0]["code"] == "unknown_target_field"

    def test_array_to_scalar_type_infeasible(self):
        """task 3.5：数组源 → 标量目标 → type_infeasible"""
        result = validate_draft(
            SOURCE_SCHEMA, TARGET_SCHEMA,
            [{"source": "tags", "target": "remark"}],
        )
        assert result["issues"][0]["code"] == "type_infeasible"

    def test_string_to_number_infeasible(self):
        assert types_compatible("string", "integer") is False
        assert types_compatible("integer", "number") is True
        assert types_compatible("integer", "varchar") is True

    def test_missing_required_reported(self):
        result = validate_draft(SOURCE_SCHEMA, TARGET_SCHEMA,
                                [{"source": "user_name", "target": "customer_name"}],
                                required_target_fields={"age"})
        codes = [i["code"] for i in result["issues"]]
        assert "missing_required" in codes

    def test_degenerate_same_name(self):
        """task 3.6：无模型退化——同名 + 类型兼容"""
        source = [{"path": "age", "type": "integer", "nullable": False, "is_array": False},
                  {"path": "remark", "type": "string", "nullable": True, "is_array": False}]
        draft = generate_degenerate_draft(source, TARGET_SCHEMA)
        pairs = {(d["source"], d["target"]) for d in draft}
        assert ("age", "age") in pairs
        assert ("remark", "remark") in pairs

    def test_degenerate_works_end_to_end(self):
        """task 3.6：退化草案 + L1/L2 全程不依赖模型"""
        source = [{"path": "age", "type": "integer", "nullable": False, "is_array": False}]
        draft = generate_degenerate_draft(source, TARGET_SCHEMA)
        validated = validate_draft(source, TARGET_SCHEMA, draft,
                                   required_target_fields={"age"})
        assert validated["valid"] is True


# ============================================================
# task 3.11：样本脱敏
# ============================================================

class TestSampleDesensitize:
    def test_sensitive_keys_masked(self):
        sample = [{"name": "张三", "id_card": "320...", "phone": "138...",
                   "rows": [{"api_key": "sk-123", "amount": 5}]}]
        cleaned = desensitize_record(sample)
        assert cleaned[0]["id_card"] == "***"
        assert cleaned[0]["phone"] == "***"
        assert cleaned[0]["rows"][0]["api_key"] == "***"
        assert cleaned[0]["name"] == "张三"  # 非敏感保留
        assert cleaned[0]["rows"][0]["amount"] == 5

    def test_sensitive_keys_normalized_separators(self):
        """P0-4 回归：分隔符/大小写变体与规范名同判（app_key、App-Key、user token）"""
        sample = {"App-Key": "sk-1", "x-app-key": "sk-2", "app_key": "sk-3",
                  "User-Token ": "t", "remark": "ok"}
        cleaned = desensitize_record(sample)
        assert cleaned["App-Key"] == "***"
        assert cleaned["x-app-key"] == "***"
        assert cleaned["app_key"] == "***"
        assert cleaned["User-Token "] == "***"
        assert cleaned["remark"] == "ok"


# ============================================================
# task 3.7/3.10/3.14：L2 零写入、降级、L3 闸门
# ============================================================

def _side(protocol="rest"):
    conn = SimpleNamespace(
        protocol=protocol, base_url="https://src.example.com",
        auth_type=None, auth_config=None, headers={}, timeout_ms=30000,
        retry_count=3, db_type=None, db_config=None,
    )
    return conn


def _patch_factory(fetch_return, push_return=None):
    adapter = MagicMock()
    adapter.fetch_data = AsyncMock(return_value=fetch_return)
    adapter.push_data = AsyncMock(return_value=push_return or {"status_code": 200, "body": None})
    adapter.test_connection = AsyncMock(return_value={"status": "ok", "latency_ms": 5, "error": None})
    factory = MagicMock()
    factory.create = MagicMock(return_value=adapter)
    return patch("app.probe.prober.ProtocolAdapterFactory", factory), adapter


MAPPING = {
    "fields": [{"source": "user_name", "target": "customer_name", "transform": "string"}],
    "target_contract": [
        {"name": "customer_name", "type": "string"},
        {"name": "age", "type": "integer", "required": True},
    ],
}


class TestL2:
    @pytest.mark.asyncio
    async def test_l2_runs_real_transform_and_no_write(self):
        """task 3.7：真实 transform/validate + 契约比对，且绝不写目标"""
        patcher, adapter = _patch_factory([{"user_name": "张三", "age": 25}])
        source = prober_side(_side(), endpoint="/users")
        target = prober_side(_side(), endpoint="/customers")
        with patcher:
            result = await run_l2(source, target, MAPPING)
        assert result["level"] == "L2"
        assert result["sample_size"] == 1
        assert result["zero_write"] is True
        adapter.push_data.assert_not_called()  # 零写入
        # 契约比对：缺 required 的 age → 不通过
        assert result["valid"] is False
        assert any(i["code"] == "missing_required" for i in result["contract_issues"])

    @pytest.mark.asyncio
    async def test_l2_pass_when_contract_satisfied(self):
        mapping = {
            "fields": [{"source": "user_name", "target": "customer_name", "transform": "string"},
                       {"source": "age", "target": "age", "transform": "int"}],
            "target_contract": [
                {"name": "customer_name", "type": "string"},
                {"name": "age", "type": "integer", "required": True},
            ],
        }
        patcher, adapter = _patch_factory([{"user_name": "张三", "age": "25"}])
        with patcher:
            result = await run_l2(prober_side(_side(), "/users"),
                                  prober_side(_side(), "/customers"), mapping)
        assert result["valid"] is True
        adapter.push_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_l2_no_contract_downgraded(self):
        """task 3.14：API 目标无契约 → L2_partial_no_contract，不得视为 L2"""
        mapping = {"fields": [{"source": "user_name", "target": "customer_name",
                               "transform": "string"}]}
        patcher, _ = _patch_factory([{"user_name": "张三"}])
        with patcher:
            result = await run_l2(prober_side(_side(), "/users"),
                                  prober_side(_side(), "/customers"), mapping)
        assert result["level"] == "L2_partial_no_contract"
        assert result["valid"] is False
        assert "next_action" in result


def prober_side(conn, endpoint="", table=""):
    from app.probe.prober import Side
    return Side(conn=conn, endpoint=endpoint, table=table)


def run_l2(source, target, mapping):
    from app.probe.prober import run_l2 as _run
    return _run(source, target, mapping)


class TestCaseInsensitiveContract:
    def test_oracle_uppercase_schema_matches_lowercase_output(self):
        """Oracle 列名大写 × 映射输出小写 → 比对互认"""
        source_schema = [{"path": "user_name", "type": "string", "nullable": False,
                          "is_array": False}]
        target_schema = [{"name": "CUSTOMER_NAME", "type": "VARCHAR2",
                          "nullable": True, "primary_key": False}]
        result = validate_draft(source_schema, target_schema,
                                [{"source": "user_name", "target": "customer_name",
                                  "transform": "string"}])
        assert result["valid"] is True
        assert result["fields"][0]["target"] == "customer_name"

    def test_missing_required_case_insensitive(self):
        result = validate_draft(
            SOURCE_SCHEMA if False else
            [{"path": "age", "type": "integer", "nullable": False, "is_array": False}],
            [{"name": "AGE", "type": "number", "nullable": False, "primary_key": True}],
            [{"source": "age", "target": "age", "transform": "int"}],
            required_target_fields={"age"},
        )
        assert result["valid"] is True  # 大小写不敏感后必填已覆盖


class TestL3Gate:
    @pytest.mark.asyncio
    async def test_l3_requires_human_authorization(self):
        """task 3.10：未授权 → requires_human_authorization 且目标零写入"""
        from app.probe.prober import RequiresHumanAuthorization

        patcher, adapter = _patch_factory([{"user_name": "张三"}])
        probe = _fresh_session("transform_verified")
        probe.e2e_authorized = False
        source = prober_side(_side(), "/users")
        target = prober_side(_side(), "/customers")
        with patcher:
            with pytest.raises(RequiresHumanAuthorization, match="requires_human_authorization"):
                await run_l3(source, target, MAPPING, session=probe)
        adapter.push_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_l3_authorized_writes_one_row_and_compares(self):
        """task 3.9：授权后写 1 条 → 回读 → comparator 比对一致"""
        patcher, adapter = _patch_factory([{"user_name": "张三", "age": 25}],
                                          push_return={"status_code": 201, "body": {
                                              "customer_name": "张三", "age": 25}})
        probe = _fresh_session("transform_verified")
        probe.e2e_authorized = True
        source = prober_side(_side(), "/users")
        target = prober_side(_side(), "/customers")
        with patcher:
            result = await run_l3(source, target, MAPPING, session=probe)
        assert result["level"] == "L3"
        assert result["valid"] is True
        adapter.push_data.assert_called_once()  # 只写 1 次


def run_l3(source, target, mapping, session=None):
    from app.probe.prober import run_l3 as _run
    return _run(source, target, mapping, session=session)


# ============================================================
# task 3.15：HTTP 面六端点全链路（TestClient 等价于 curl 序列）
# ============================================================

@pytest.fixture()
def probe_client():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_session

    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session.add = MagicMock()

    async def _populate(instance):
        if not instance.id:
            instance.id = uuid4()
        if instance.status is None:
            instance.status = "draft"
        if not instance.expires_at:
            from datetime import datetime, timedelta, timezone
            instance.expires_at = datetime.now(timezone.utc) + timedelta(days=7)

    session.refresh = AsyncMock(side_effect=_populate)
    empty_result = MagicMock()
    empty_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=empty_result)

    async def override():
        yield session

    app.dependency_overrides[get_session] = override
    yield session, TestClient(app, headers={"X-Admin-Token": "test-admin-token"})
    app.dependency_overrides.clear()


class TestProbeHttpFace:
    def test_six_endpoints_draft_to_pending_review(self, probe_client):
        """task 3.15：不经 CLI，依次调 6 个端点把会话从 draft 推到 pending_review"""
        session, client = probe_client

        # 1. POST /probes（内联双形态）
        created = client.post("/api/v1/probes", json={
            "source": {"protocol": "rest", "base_url": "https://src.example.com",
                       "auth_type": "bearer", "auth_config": {"token": "sk-plain"},
                       "endpoint": "/users"},
            "target": {"protocol": "rest", "base_url": "https://tgt.example.com",
                       "endpoint": "/customers"},
            "via": "cli",
        }).json()
        probe_id = created["id"]
        assert created["status"] == "draft"
        # 内联明文凭证不落库（task 3.13）
        assert created["source_snapshot"]["auth_config"]["token"] == "***"

        # 后续读取都返回这个会话
        stored = session.add.call_args_list[0][0][0]
        session.get = AsyncMock(return_value=stored)

        patcher, adapter = _patch_factory(
            [{"user_name": "张三", "age": 25}, {"user_name": "李四", "age": 30}])

        with patcher:
            # 2. POST /probes/{id}/probe → L1
            r = client.post(f"/api/v1/probes/{probe_id}/probe", params={"via": "cli"})
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "L1_reachable"

            # 3. POST /probes/{id}/infer
            r = client.post(f"/api/v1/probes/{probe_id}/infer", params={"via": "cli"})
            assert r.status_code == 200, r.text
            paths = [f["path"] for f in r.json()["source_schema"]["fields"]]
            assert "user_name" in paths

            # 4. POST /probes/{id}/draft（退化草案 + 契约声明）
            r = client.post(f"/api/v1/probes/{probe_id}/draft", json={
                "fields": [
                    {"source": "user_name", "target": "customer_name", "transform": "string"},
                    {"source": "age", "target": "age", "transform": "int"},
                ],
                "target_contract": [
                    {"name": "customer_name", "type": "string"},
                    {"name": "age", "type": "integer", "required": True},
                ],
                "via": "cli"})
            assert r.status_code == 200, r.text
            assert r.json()["valid"] is True

            # 5. POST /probes/{id}/verify → L2（零写入）
            r = client.post(f"/api/v1/probes/{probe_id}/verify",
                            json={"level": "L2", "via": "cli"})
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "transform_verified"

        # 6. POST /probes/{id}/submit → pending_review
        r = client.post(f"/api/v1/probes/{probe_id}/submit", json={"via": "cli",
                                                                   "decided_by": "agent-x"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending_review"
        adapter.push_data.assert_not_called()  # 全程零写入

    def test_submit_blocked_before_l2(self, probe_client):
        """task 4.3：未达 L2 的会话提交被拒并说明缺失条件"""
        session, client = probe_client
        created = client.post("/api/v1/probes", json={
            "source": {"protocol": "rest", "base_url": "https://s.example.com",
                       "endpoint": "/u"},
            "target": {"protocol": "rest", "base_url": "https://t.example.com",
                       "endpoint": "/c"},
        }).json()
        stored = session.add.call_args_list[0][0][0]
        session.get = AsyncMock(return_value=stored)

        r = client.post(f"/api/v1/probes/{created['id']}/submit", json={"via": "cli"})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "below_l2_threshold"

    def test_l3_requires_authorization_over_http(self, probe_client):
        """task 3.10：HTTP 面同样拒绝未授权 L3"""
        session, client = probe_client
        created = client.post("/api/v1/probes", json={
            "source": {"protocol": "rest", "base_url": "https://s.example.com",
                       "endpoint": "/u"},
            "target": {"protocol": "rest", "base_url": "https://t.example.com",
                       "endpoint": "/c"},
        }).json()
        stored = session.add.call_args_list[0][0][0]
        session.get = AsyncMock(return_value=stored)
        stored.status = "transform_verified"

        patcher, adapter = _patch_factory([{"user_name": "张三"}])
        with patcher:
            r = client.post(f"/api/v1/probes/{created['id']}/verify",
                            json={"level": "L3"})
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "requires_human_authorization"
        adapter.push_data.assert_not_called()


# ============================================================
# P1 回归：probe_guard SSRF 防线接线
# ============================================================

from app.config import settings
from app.probe import prober
from app.security.probe_guard import TargetNotAllowed, validate_target


class TestProbeGuardWiring:
    def test_validate_target_noop_when_enforce_off(self, monkeypatch):
        """默认关：本地部署内网目标合法（探针与同步同 URL）"""
        monkeypatch.setattr(settings, "probe_ssrf_enforce", False)
        validate_target("http://127.0.0.1:8000/x")
        validate_target("http://169.254.169.254/latest/meta-data")

    def test_validate_target_blocks_private_when_enforced(self, monkeypatch):
        monkeypatch.setattr(settings, "probe_ssrf_enforce", True)
        with pytest.raises(TargetNotAllowed):
            validate_target("http://127.0.0.1:8000/x")
        with pytest.raises(TargetNotAllowed):
            validate_target("http://10.1.2.3/api")
        with pytest.raises(TargetNotAllowed):
            validate_target("http://169.254.169.254/latest/meta-data")

    def test_allowed_hosts_exempt(self, monkeypatch):
        monkeypatch.setattr(settings, "probe_ssrf_enforce", True)
        monkeypatch.setattr(settings, "probe_allowed_hosts", "192.168.1.10")
        validate_target("http://192.168.1.10/api")
        with pytest.raises(TargetNotAllowed):
            validate_target("http://192.168.1.11/api")

    def test_api_adapter_wired(self, monkeypatch):
        """探针构造适配器前过 validate_target（_api_adapter 接线点）"""
        monkeypatch.setattr(settings, "probe_ssrf_enforce", True)
        side = SimpleNamespace(
            conn=SimpleNamespace(protocol="rest", base_url="http://127.0.0.1:9/x",
                                 auth_type=None, auth_config=None, headers={},
                                 timeout_ms=3000, retry_count=1, db_type=None,
                                 db_config=None),
            mapping_config={}, mapping_headers={})
        with pytest.raises(TargetNotAllowed):
            prober._api_adapter(side, "source")
