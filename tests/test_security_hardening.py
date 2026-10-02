"""阶段Ⅰ 组2 安全收口测试（task 2.11：安全用例随全量套件执行）

覆盖：管理 token 鉴权矩阵（2.1）、豁免路径（2.2）、生产启动守卫（2.3）、
debug 裸端点移除（2.10）、SSRF 目标校验（2.6）、重定向复校验（2.7）、
响应体上限（2.8）、调用预算（2.9）。
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app, lifespan
from app.security.probe_guard import (
    BodyLimiter,
    ProbeBudget,
    ProbeBudgetExhausted,
    TargetNotAllowed,
    check_redirect,
    validate_target,
)
from tests.conftest import TEST_ADMIN_TOKEN


@pytest.fixture()
def client():
    # 不进入 lifespan（与其他 client fixture 一致，避免跨用例 loop 关闭问题）
    return TestClient(app, headers={"X-Admin-Token": TEST_ADMIN_TOKEN})


# ============================================================
# task 2.1：鉴权矩阵
# ============================================================

class TestAdminTokenMatrix:
    def test_no_token_rejected_401(self):
        client = TestClient(app)
        resp = client.get("/api/v1/connections")
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "unauthorized"

    def test_wrong_token_rejected_401(self):
        client = TestClient(app, headers={"X-Admin-Token": "wrong-token"})
        resp = client.get("/api/v1/connections")
        assert resp.status_code == 401

    def test_bearer_scheme_accepted(self):
        # 以上下文管理器进入 lifespan：真实建表（发行包/全新环境没有既有库，
        # 本用例要真正打到业务端点，不能只验中间件）
        with TestClient(app, headers={
                "Authorization": f"Bearer {TEST_ADMIN_TOKEN}"}) as client:
            resp = client.get("/api/v1/connections")
        assert resp.status_code != 401  # 鉴权通过（后续业务行为由用例自身负责）

    def test_correct_token_accepted(self):
        with TestClient(app, headers={"X-Admin-Token": TEST_ADMIN_TOKEN}) as client:
            resp = client.get("/api/v1/connections")
        assert resp.status_code != 401


# ============================================================
# task 2.2：豁免路径
# ============================================================

class TestExemptPaths:
    def test_health_without_token_ok(self):
        resp = TestClient(app).get("/health")
        assert resp.status_code == 200

    def test_metrics_without_token_ok(self):
        resp = TestClient(app).get("/metrics")
        assert resp.status_code == 200


# ============================================================
# task 2.3：生产启动守卫
# ============================================================

class TestProductionStartupGuard:
    def test_production_empty_token_refuses_startup(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "env", "production")
        monkeypatch.setattr(settings, "admin_token", "")
        with pytest.raises(RuntimeError, match="admin_token_missing"):
            asyncio.run(lifespan(app).__aenter__())

    def test_dev_empty_token_starts_with_warning(self, monkeypatch, caplog):
        from app.config import settings

        monkeypatch.setattr(settings, "env", "dev")
        monkeypatch.setattr(settings, "admin_token", "")
        # 本用例只验 token 守卫；schema 自检跑在真实库上（合并后 check_bi_schema 还查
        # P6 新表 bi_access_profiles），与断言无关，置空以免受开发/临时库既有状态影响
        monkeypatch.setattr("app.migrations.check_schema", lambda conn: None)
        monkeypatch.setattr("app.migrations.check_bi_schema", lambda conn: None)
        with caplog.at_level("WARNING", logger="app.main"):
            asyncio.run(lifespan(app).__aenter__())
        assert any("APIBRIDGE_ADMIN_TOKEN" in r.message for r in caplog.records)


# ============================================================
# task 2.10：debug 裸端点已移除
# ============================================================

class TestDebugEndpointsRemoved:
    def test_proxy_endpoint_gone(self, client):
        resp = client.post("/api/v1/debug/proxy", json={"url": "https://x.example.com"})
        assert resp.status_code == 404

    def test_quick_proxy_endpoint_gone(self, client):
        resp = client.get("/api/v1/debug/quick-proxy", params={"url": "https://x.example.com"})
        assert resp.status_code == 404


# ============================================================
# task 2.6：SSRF 目标校验
# ============================================================

class TestTargetValidation:
    @pytest.fixture(autouse=True)
    def _enforce_on(self, monkeypatch):
        # 拒绝语义只在 APIBRIDGE_PROBE_SSRF_ENFORCE=true 时生效（默认关=既有部署行为）
        from app.config import settings
        monkeypatch.setattr(settings, "probe_ssrf_enforce", True)

    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",  # 云元数据（链路本地）
        "http://127.0.0.1:8000/health",              # 回环
        "http://10.1.2.3/api",                        # RFC1918
        "http://192.168.1.1/",                        # RFC1918
        "http://172.16.0.9/",                         # RFC1918
        "http://[::1]/",                              # IPv6 回环
        "http://0.0.0.0/",                            # 未指定地址
    ])
    def test_denied_targets(self, url):
        with pytest.raises(TargetNotAllowed, match="target_not_allowed"):
            validate_target(url)

    def test_non_http_scheme_denied(self):
        with pytest.raises(TargetNotAllowed):
            validate_target("file:///etc/passwd")

    def test_dns_failure_denied(self, monkeypatch):
        from app.security import probe_guard

        monkeypatch.setattr(probe_guard, "resolve_host", lambda host: [])
        with pytest.raises(TargetNotAllowed, match="DNS"):
            validate_target("http://nonexistent.invalid/api")

    def test_dns_resolving_to_private_denied(self, monkeypatch):
        """DNS 解析结果落在私网也拒绝（防 DNS rebinding）"""
        from app.security import probe_guard

        monkeypatch.setattr(probe_guard, "resolve_host", lambda host: ["10.0.0.9"])
        with pytest.raises(TargetNotAllowed, match="10.0.0.9"):
            validate_target("http://public-looking.example.com/api")

    def test_public_ip_allowed(self):
        validate_target("https://93.184.216.34/api")  # 不抛即通过，且未发起网络连接

    def test_allowlist_overrides_private(self, monkeypatch):
        """task 2.6：私网目标显式列入白名单后放行"""
        from app.config import settings

        monkeypatch.setattr(settings, "probe_allowed_hosts", "10.0.0.5,192.168.1.1")
        validate_target("http://10.0.0.5:8000/api")  # 不抛即通过
        validate_target("http://192.168.1.1/")


# ============================================================
# task 2.7：重定向逐跳复校验
# ============================================================

class TestRedirectRecheck:
    @pytest.fixture(autouse=True)
    def _enforce_on(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "probe_ssrf_enforce", True)

    def test_redirect_to_private_denied(self):
        """首跳放行的公网目标，重定向到私网必须在第二跳被拒"""
        with pytest.raises(TargetNotAllowed):
            check_redirect("http://169.254.169.254/latest/meta-data/")

    def test_redirect_to_public_allowed(self):
        check_redirect("https://93.184.216.34/next")


# ============================================================
# task 2.8：响应体上限
# ============================================================

class TestBodyLimiter:
    def test_under_limit_ok(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "probe_max_body_bytes", 1000)
        limiter = BodyLimiter()
        assert limiter.feed(600) is True
        assert limiter.feed(300) is True
        assert limiter.truncated is False

    def test_over_limit_truncated(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "probe_max_body_bytes", 1000)
        limiter = BodyLimiter()
        assert limiter.feed(800) is True
        assert limiter.feed(500) is False
        assert limiter.truncated is True


# ============================================================
# task 2.9：调用预算
# ============================================================

class TestProbeBudget:
    def test_budget_exhausted_after_max(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "probe_budget", 3)
        budget = ProbeBudget()
        for _ in range(3):
            budget.consume("sess-1", "https://api.example.com")
        with pytest.raises(ProbeBudgetExhausted, match="probe_budget_exhausted"):
            budget.consume("sess-1", "https://api.example.com")

    def test_budget_per_target_isolated(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "probe_budget", 2)
        budget = ProbeBudget()
        budget.consume("sess-1", "https://a.example.com")
        budget.consume("sess-1", "https://a.example.com")
        with pytest.raises(ProbeBudgetExhausted):
            budget.consume("sess-1", "https://a.example.com")
        # 另一目标不受影响
        budget.consume("sess-1", "https://b.example.com")
        assert budget.remaining("sess-1", "https://b.example.com") == 1
