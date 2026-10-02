"""待确认清单后端集成测试（task 4.1-4.11）"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import get_session
from app.models.sync import ProbeSession

NOW = datetime.now(timezone.utc)


def _pending_probe(**overrides):
    """构造一个处于 pending_review、L2 新鲜的会话"""
    probe = ProbeSession(
        status="pending_review",
        source_snapshot={"protocol": "rest", "base_url": "https://src.example.com"},
        target_snapshot={"protocol": "rest", "base_url": "https://tgt.example.com"},
        source_endpoint="/users",
        target_endpoint="/customers",
        source_schema={"fields": [
            {"path": "user_name", "type": "string", "nullable": False, "is_array": False},
            {"path": "age", "type": "integer", "nullable": False, "is_array": False},
        ]},
        target_schema=None,
        mapping_draft={"mapping_config": {"fields": [
            {"source": "user_name", "target": "customer_name", "transform": "string"},
            {"source": "age", "target": "age", "transform": "int"},
        ]}},
        l2_result={"level": "L2", "valid": True, "sample_size": 5,
                   "verified_at": NOW.isoformat()},
        decided_by="agent-x",
    )
    for k, v in overrides.items():
        setattr(probe, k, v)
    return probe


@pytest.fixture()
def review_client():
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    session.add = MagicMock()
    session.delete = AsyncMock()

    async def _populate(instance):
        if not instance.id:
            instance.id = uuid4()
        if instance.status is None:
            instance.status = "draft"

    session.refresh = AsyncMock(side_effect=_populate)
    empty = MagicMock()
    empty.scalars.return_value.all.return_value = []
    empty.rowcount = 0

    from sqlalchemy import Update as _SaUpdate

    async def _fake_execute(stmt, *a, **kw):
        # approve 的并发守卫走条件 UPDATE 并依赖 rowcount——mock 需按语句类型区分
        if isinstance(stmt, _SaUpdate):
            ok = MagicMock()
            ok.rowcount = 1
            return ok
        return empty

    session.execute = AsyncMock(side_effect=_fake_execute)

    async def override():
        yield session

    app.dependency_overrides[get_session] = override
    yield session, TestClient(app, headers={"X-Admin-Token": "test-admin-token"})
    app.dependency_overrides.clear()


def _seed(session, probe):
    if not probe.id:
        probe.id = uuid4()
    session.add(probe)
    session.get = AsyncMock(return_value=probe)
    return probe


APPROVE_BODY = {
    "decided_by": "工程师-王",
    "via": "ui",
    "source_connection": {"protocol": "rest", "base_url": "https://src.example.com"},
    "target_connection": {"protocol": "rest", "base_url": "https://tgt.example.com"},
}


# ============================================================
# task 4.1：默认清单只含 pending_review + 过滤
# ============================================================

class TestReviewList:
    def test_default_view_only_pending_review(self, review_client):
        session, client = review_client
        pending = _pending_probe()
        l1_only = _pending_probe(status="L1_reachable")
        captured = []

        def _capture(obj):
            captured.append(obj)
            return MagicMock()

        session.add.side_effect = _capture
        rows = [pending, l1_only]
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        session.execute = AsyncMock(return_value=result)

        items = client.get("/api/v1/probes").json()
        assert all(i["status"] == "pending_review" for i in items)
        assert len(items) == 1
        assert items[0]["level"] == "L2"
        assert items[0]["protocol"] == "rest"

    def test_status_filter_overrides_default(self, review_client):
        session, client = review_client
        rows = [_pending_probe(status="rejected")]
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        session.execute = AsyncMock(return_value=result)

        items = client.get("/api/v1/probes", params={"status": "rejected"}).json()
        assert len(items) == 1


# ============================================================
# task 4.2：详情五组对照 + 脱敏
# ============================================================

class TestProbeDetail:
    def test_detail_has_comparison_groups(self, review_client):
        session, client = review_client
        probe = _pending_probe(source_sample=[{"user_name": "张三", "token": "sk-1"}],
                               l2_result={"level": "L2", "valid": True,
                                          "contract_issues": [
                                              {"code": "missing_required",
                                               "path": "age", "message": "必填未覆盖"}]},
                               verified_fresh=True)
        _seed(session, probe)
        detail = client.get(f"/api/v1/probes/{probe.id}").json()
        for key in ("source_sample", "source_schema", "mapping_draft",
                    "target_schema", "comparison"):
            assert key in detail
        assert detail["comparison"]["contract_issues"][0]["path"] == "age"
        assert detail["source_sample"][0]["token"] == "***"  # 认证字段不外泄


# ============================================================
# task 4.4-4.7：批准（单事务建桥 / 修改映射 / L2 时效 / 回滚）
# ============================================================

class TestApprove:
    def test_approve_creates_bridge_and_committed(self, review_client):
        """task 4.4：单事务创建三表记录，会话 committed 并记录 ID"""
        session, client = review_client
        probe = _seed(session, _pending_probe())
        resp = client.post(f"/api/v1/probes/{probe.id}/approve", json=APPROVE_BODY)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "committed"
        assert len(body["bridge_ids"]["connection_ids"]) == 2
        assert body["bridge_ids"]["mapping_id"]
        assert body["bridge_ids"]["task_id"]
        # seed 的会话 1 次 + 建桥 4 个对象（2 连接 + 1 映射 + 1 任务）
        assert session.add.call_count == 5

    def test_approve_with_modified_mapping_revalidates(self, review_client):
        """task 4.5：修改后的映射必须重过确定性校验；不通过保持 pending_review"""
        session, client = review_client
        probe = _seed(session, _pending_probe())
        bad_mapping = {"fields": [
            {"source": "no_such_field", "target": "customer_name", "transform": "string"}]}
        resp = client.post(f"/api/v1/probes/{probe.id}/approve", json={
            **APPROVE_BODY, "mapping_config": bad_mapping})
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "mapping_revalidation_failed"
        assert probe.status == "pending_review"

    def test_approve_rejected_when_l2_stale(self, review_client):
        """task 4.6：L2 结果过期或映射被修改 → 要求重跑"""
        session, client = review_client
        old = NOW - timedelta(hours=2)
        probe = _seed(session, _pending_probe(
            l2_result={"level": "L2", "valid": True, "verified_at": old.isoformat()}))
        resp = client.post(f"/api/v1/probes/{probe.id}/approve", json=APPROVE_BODY)
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "l2_stale"
        assert probe.status == "pending_review"

    def test_approve_fails_rolls_back_and_keeps_pending(self, review_client):
        """task 4.7：事务内任一步失败全回滚，状态保持 pending_review"""
        session, client = review_client
        probe = _seed(session, _pending_probe())
        session.flush = AsyncMock(side_effect=RuntimeError("db constraint boom"))
        resp = client.post(f"/api/v1/probes/{probe.id}/approve", json=APPROVE_BODY)
        assert resp.status_code == 500
        assert resp.json()["detail"]["code"] == "approve_failed"
        assert probe.status == "pending_review"
        session.rollback.assert_called()

    def test_approve_records_audit(self, review_client):
        """task 4.10：操作者/时间/来源/级别/映射摘要可追溯"""
        session, client = review_client
        probe = _seed(session, _pending_probe())
        client.post(f"/api/v1/probes/{probe.id}/approve", json={
            **APPROVE_BODY, "decided_by": "工程师-王"})
        bridge = probe.bridge_ids
        assert bridge["approved_by"] == "工程师-王"
        assert bridge["approved_via"] == "ui"
        assert bridge["approved_level"] == "L2"
        assert bridge["mapping_digest"]
        assert any(a["to"] == "committed" for a in probe.audit_log)


# ============================================================
# task 4.8：驳回
# ============================================================

class TestReject:
    def test_reject_requires_reason(self, review_client):
        session, client = review_client
        probe = _seed(session, _pending_probe())
        resp = client.post(f"/api/v1/probes/{probe.id}/reject", json={
            "reason": "", "decided_by": "工程师-王"})
        assert resp.status_code == 422  # pydantic min_length 拒绝空原因

    def test_rejected_cannot_be_approved(self, review_client):
        """task 4.8：已驳回再批准 → 409"""
        session, client = review_client
        probe = _seed(session, _pending_probe())
        r1 = client.post(f"/api/v1/probes/{probe.id}/reject", json={
            "reason": "源系统不稳定", "decided_by": "工程师-王"})
        assert r1.json()["status"] == "rejected"
        assert probe.reject_reason == "源系统不稳定"

        r2 = client.post(f"/api/v1/probes/{probe.id}/approve", json=APPROVE_BODY)
        assert r2.status_code == 409
        assert r2.json()["detail"]["code"] == "already_rejected"


# ============================================================
# task 4.9：L3 授权
# ============================================================

class TestAuthorizeE2E:
    def test_authorize_sets_gate(self, review_client):
        session, client = review_client
        probe = _seed(session, _pending_probe())
        resp = client.post(f"/api/v1/probes/{probe.id}/authorize-e2e", json={
            "authorized_by": "工程师-王"})
        assert resp.status_code == 200
        assert probe.e2e_authorized is True
        assert probe.authorized_by == "工程师-王"
        assert any(a.get("action") == "authorize_e2e" for a in probe.audit_log)

    def test_l3_verify_passes_gate_after_authorize(self, review_client):
        """授权后 verify --level L3 不再返回 requires_human_authorization"""
        session, client = review_client
        probe = _seed(session, _pending_probe(
            status="transform_verified",
            mapping_draft={"mapping_config": {"fields": [
                {"source": "user_name", "target": "customer_name", "transform": "string"}]}}))
        client.post(f"/api/v1/probes/{probe.id}/authorize-e2e", json={
            "authorized_by": "工程师-王"})

        adapter = MagicMock()
        adapter.fetch_data = AsyncMock(return_value=[{"user_name": "张三"}])
        adapter.push_data = AsyncMock(return_value={
            "status_code": 201, "body": {"customer_name": "张三"}})
        factory = MagicMock()
        factory.create = MagicMock(return_value=adapter)
        with __import__("unittest.mock", fromlist=["patch"]).patch(
                "app.probe.prober.ProtocolAdapterFactory", factory):
            resp = client.post(f"/api/v1/probes/{probe.id}/verify",
                               json={"level": "L3", "via": "ui"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["l3_result"]["level"] == "L3"
        adapter.push_data.assert_called_once()  # 授权后才写，且只写 1 条
