"""探针目标安全防护 — SSRF 防线（task 2.6-2.9）

四个原语，探针内核（阶段Ⅱ）与将来的受控代理共用：
    - validate_target: 目标白名单 + DNS 预解析后拒绝回环/RFC1918/链路本地/
      云元数据地址；私网目标需显式列入 APIBRIDGE_PROBE_ALLOWED_HOSTS
    - check_redirect: 重定向逐跳复校验（httpx follow_redirects 的每跳由调用方回调）
    - BodyLimiter: 响应体大小上限，超限标记截断
    - ProbeBudget: 单会话对同一目标的调用预算（默认 ≤10 次）

拒绝统一返回 TargetNotAllowed 异常，reason 固定 "target_not_allowed"；
预算耗尽 reason 为 "probe_budget_exhausted"。
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.config import settings


class TargetNotAllowed(Exception):
    """目标地址被安全策略拒绝"""

    def __init__(self, host: str, detail: str = ""):
        self.host = host
        self.reason = "target_not_allowed"
        super().__init__(f"target_not_allowed: {host} {detail}".strip())


class ProbeBudgetExhausted(Exception):
    """探针调用预算耗尽"""

    def __init__(self, target: str):
        self.reason = "probe_budget_exhausted"
        super().__init__(f"probe_budget_exhausted: {target}")


def _denied_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """IP 属于拒绝范围时返回原因"""
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"  # 含云元数据地址 169.254.169.254
    if ip.is_private:
        return "private"  # RFC1918 / 站点内网
    if ip.is_reserved or ip.is_unspecified or ip.is_multicast:
        return "reserved"
    return None


def allowed_hosts() -> list[str]:
    """显式白名单（逗号分隔配置）"""
    return [h.strip().lower() for h in settings.probe_allowed_hosts.split(",") if h.strip()]


def resolve_host(host: str) -> list[str]:
    """DNS 预解析（解析失败返回空列表，由调用方决定语义）"""
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except (socket.gaierror, OSError):
        return []


def validate_target(url: str) -> None:
    """校验探针目标 URL，违规抛 TargetNotAllowed

    规则（task 2.6）:
        - 总开关 probe_ssrf_enforce（默认关）：本产品为本地部署，探针/同步目标
          常驻内网且与运行时同 URL——默认强制私网拒绝会破坏核心用法；
          面向多租户/隔离部署时置 APIBRIDGE_PROBE_SSRF_ENFORCE=true 启用
        - 仅允许 http/https
        - 主机名 DNS 预解析，任一解析结果落在拒绝范围即拒绝
        - 拒绝范围：回环 / RFC1918 / 链路本地（含云元数据）/ 保留段
        - 白名单（APIBRIDGE_PROBE_ALLOWED_HOSTS）中的主机豁免私网限制
    """
    if not settings.probe_ssrf_enforce:
        return
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise TargetNotAllowed(url, detail=f"不允许的协议 {parsed.scheme or '(空)'}")
    host = parsed.hostname
    if not host:
        raise TargetNotAllowed(url, detail="缺少主机名")

    if host.lower() in allowed_hosts():
        return

    # 直接是 IP 字面量时无需解析
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        ips = [ipaddress.ip_address(a) for a in resolve_host(host)]
        if not ips:
            raise TargetNotAllowed(host, detail="DNS 解析失败")

    for ip in ips:
        reason = _denied_reason(ip)
        if reason:
            raise TargetNotAllowed(host, detail=f"解析到受限地址 {ip}（{reason}）")


def validate_host(host: str) -> None:
    """校验裸主机/IP（DB 侧探测用）：同 validate_target 的解析+拒绝段+白名单语义，
    但不经过 http(s) scheme 门——数据库目标不是 URL（复审修正：此前 tcp:// 硬造
    URL 被 scheme 检查无条件拒绝，enforce 开启后 DB 探针 100% 挂）"""
    if not settings.probe_ssrf_enforce:
        return
    host = str(host or "").strip()
    if not host:
        raise TargetNotAllowed(host, detail="缺少主机名")
    if host.lower() in allowed_hosts():
        return
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        ips = [ipaddress.ip_address(a) for a in resolve_host(host)]
        if not ips:
            raise TargetNotAllowed(host, detail="DNS 解析失败")
    for ip in ips:
        reason = _denied_reason(ip)
        if reason:
            raise TargetNotAllowed(host, detail=f"解析到受限地址 {ip}（{reason}）")


def check_redirect(location_url: str) -> None:
    """重定向逐跳复校验（task 2.7）：每一跳都按目标规则重新校验"""
    validate_target(location_url)


@dataclass
class BodyLimiter:
    """响应体大小限制器（task 2.8）

    用法：每读到一块数据调用 feed(chunk)，返回是否仍可继续读；
    读取结束后 body_truncated 标记是否发生截断。
    """

    limit: int = 0  # 0 表示取全局配置
    received: int = 0
    truncated: bool = False

    def __post_init__(self):
        if self.limit <= 0:
            self.limit = settings.probe_max_body_bytes

    def feed(self, chunk_len: int) -> bool:
        """喂入一块数据的长度；超限时标记截断并停止"""
        if self.truncated:
            return False
        self.received += chunk_len
        if self.received > self.limit:
            self.truncated = True
            return False
        return True


@dataclass
class ProbeBudget:
    """单会话对同一目标的调用预算（task 2.9）

    默认上限 settings.probe_budget（10 次）；按 (会话, 目标) 计数。
    """

    max_calls: int = 0
    used: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        if self.max_calls <= 0:
            self.max_calls = settings.probe_budget

    def consume(self, session_id: str, target: str) -> None:
        """消耗一次调用额度；耗尽抛 ProbeBudgetExhausted"""
        key = f"{session_id}->{target}"
        used = self.used.get(key, 0)
        if used >= self.max_calls:
            raise ProbeBudgetExhausted(target)
        self.used[key] = used + 1

    def remaining(self, session_id: str, target: str) -> int:
        return max(0, self.max_calls - self.used.get(f"{session_id}->{target}", 0))
