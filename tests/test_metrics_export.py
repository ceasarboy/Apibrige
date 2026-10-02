# -*- coding: utf-8 -*-
"""监控指标导出守卫（tasks 1.1 / 1.2）

背景：项目内置 Grafana + Prometheus，但长期存在"纸面看板"问题——
指标只声明未赋值、进程级指标根本没注册、抓取目标主机名写错，
导致面板恒空、告警永不触发。本文件把
**「配置里引用的指标必须真实存在」**固化为自动化守卫，防止再次退化。
"""
import json
import platform
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "configs"
GRAFANA_JSON = CONFIG_DIR / "grafana-dashboard.json"
ALERTS_YML = CONFIG_DIR / "alerts.yml"
PROMETHEUS_YML = CONFIG_DIR / "prometheus.yml"
COMPOSE_YML = ROOT / "docker-compose.yml"

# ProcessCollector 依赖 /proc，**仅在 Linux 上产出序列**（本机 Windows 实测为 0 条）；
# 生产部署是 Docker(Linux)，因此 Linux 下这些指标必须存在。
IS_LINUX = platform.system() == "Linux"
PROCESS_METRICS = ["process_resident_memory_bytes", "process_cpu_seconds_total"]

# 必须始终存在的关键指标（Gauge → 与是否有流量无关）
CRITICAL_ALWAYS_PRESENT = [
    "apibridge_buffer_pending",
    "apibridge_active_tasks",
    "apibridge_active_connections",
    "apibridge_db_pool_connections",
    "apibridge_db_pool_size",
]

# 需要"先发生一次活动"才会产生序列的指标（带标签的 Counter/Histogram）
CRITICAL_AFTER_ACTIVITY = [
    "apibridge_sync_total",
    "apibridge_sync_duration_seconds_bucket",
    "apibridge_api_calls_total",
    "apibridge_api_call_duration_seconds_bucket",
]

# 允许"按条件缺失"的指标：
# 1) apibridge_queue_depth 在 MQ 不可达时**故意不导出**（D2：宁可 no data 也不谎报 0）
# 2) process_* 在非 Linux 平台不产出（ProcessCollector 依赖 /proc）
CONDITIONALLY_ABSENT = {"apibridge_queue_depth"}
if not IS_LINUX:
    CONDITIONALLY_ABSENT |= set(PROCESS_METRICS)

_METRIC_RE = re.compile(r"\b(apibridge_[a-z0-9_]+|process_[a-z0-9_]+)\b")


def render_metrics(with_activity: bool = True) -> str:
    """渲染 /metrics 输出

    with_activity 时先制造一次活动（同步 / 外部 API 调用 / HTTP 请求），
    使带标签的 Counter/Histogram 产生序列——否则它们不出现，
    会把“没跑过”误判成“没导出”。
    """
    from app.metrics_collector import (
        get_metrics, http_requests, record_api_call, record_sync,
    )

    if with_activity:
        record_sync("success", "manual", 0.12)
        record_api_call("http://example.invalid", "ok", 0.05)
        http_requests.labels(method="GET", endpoint="/health", status="200").inc()
    return get_metrics().decode("utf-8")


def metric_names_in(text: str) -> set[str]:
    """从 /metrics 输出提取指标名（去掉标签与类型后缀）"""
    names: set[str] = set()
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        head = line.split("{", 1)[0].split(" ", 1)[0].strip()
        if head:
            names.add(head)
    return names


def referenced_metrics() -> dict[str, set[str]]:
    """从 Grafana 面板与 Prometheus 告警规则中提取引用的指标名"""
    out: dict[str, set[str]] = {}
    for path in (GRAFANA_JSON, ALERTS_YML):
        out[path.name] = set(_METRIC_RE.findall(path.read_text(encoding="utf-8")))
    return out


# ============================================================
# 1.1 关键指标必须出现在 /metrics 输出中
# ============================================================

class TestCriticalMetricsExported:

    @pytest.mark.parametrize("name", CRITICAL_ALWAYS_PRESENT)
    def test_always_present_metric(self, name):
        """Gauge / 进程级指标与流量无关，必须始终可见"""
        exported = metric_names_in(render_metrics())
        assert name in exported, f"关键指标未导出: {name}"

    @pytest.mark.skipif(
        not IS_LINUX,
        reason="ProcessCollector 依赖 /proc，仅 Linux 产出序列（生产为 Docker/Linux）")
    @pytest.mark.parametrize("name", PROCESS_METRICS)
    def test_process_metric_present_on_linux(self, name):
        """进程级指标：Linux（=生产环境）下必须可见

        否则 Grafana 内存面板恒空、CPU/内存告警永不触发。
        """
        exported = metric_names_in(render_metrics())
        assert name in exported, f"进程级指标未导出: {name}"

    @pytest.mark.parametrize("name", CRITICAL_AFTER_ACTIVITY)
    def test_present_after_activity(self, name):
        """带标签的 Counter/Histogram 在产生一次活动后必须可见"""
        exported = metric_names_in(render_metrics(with_activity=True))
        assert name in exported, f"关键指标未导出: {name}"


# ============================================================
# 1.2 配置引用的每个指标都必须真实存在
# ============================================================

class TestConfigReferencesResolvable:

    def test_every_referenced_metric_exists(self):
        """Grafana 面板 / 告警规则引用的指标必须能在 /metrics 中找到

        否则面板恒空、告警永不触发——这正是本轮要修的"纸面看板"。
        """
        exported = metric_names_in(render_metrics(with_activity=True))
        gaps: list[str] = []
        for source, names in referenced_metrics().items():
            for name in sorted(names):
                if name in CONDITIONALLY_ABSENT:
                    continue  # 见 CONDITIONALLY_ABSENT 的说明
                if name not in exported:
                    gaps.append(f"{source} → {name}")
        assert not gaps, "配置引用了不存在的指标（面板/告警将恒空）:\n  " + \
                         "\n  ".join(gaps)

    def test_grafana_dashboard_is_parseable(self):
        payload = json.loads(GRAFANA_JSON.read_text(encoding="utf-8"))
        assert payload.get("panels"), "看板应含面板"

    def test_prometheus_scrape_target_matches_compose_service(self):
        """抓取目标主机名必须与 compose 服务名一致，否则 Prometheus 永远抓不到"""
        compose = COMPOSE_YML.read_text(encoding="utf-8")
        services = set(re.findall(r"^  ([a-z0-9_-]+):", compose, re.MULTILINE))
        targets = re.findall(r'-\s*"([a-z0-9_-]+):\d+"',
                             PROMETHEUS_YML.read_text(encoding="utf-8"))
        assert targets, "prometheus.yml 应配置静态抓取目标"
        for host in targets:
            assert host in services, (
                f"prometheus.yml 抓取目标 {host!r} 不在 docker-compose 服务列表中 "
                f"{sorted(services)}")
