"""管理面鉴权 — 共享 token 校验（task 2.1-2.3）

单租户内部工具，采用最小共享 token 方案（RBAC 是既有非目标）：
    - 请求头 X-Admin-Token: <token>（兼容 Authorization: Bearer <token>）
    - token 已配置：恒定时间比较，不匹配一律 401
    - token 未配置：仅开发环境放行（启动时打印醒目告警）；生产模式拒绝启动
"""
import secrets

from app.config import settings

# 放行鉴权的前缀（task 2.2）：健康检查与指标采集
EXEMPT_PREFIXES = ("/health", "/metrics")


def check_admin_token(provided: str | None) -> tuple[bool, str]:
    """校验管理 token

    Returns:
        (是否放行, 拒绝码)；放行时拒绝码为空字符串
    """
    configured = settings.admin_token
    if not configured:
        # 生产模式空 token 在启动即被拒绝（main.py lifespan），此处仅兜底
        if settings.env == "production":
            return False, "admin_token_missing"
        return True, ""
    if provided:
        # 非 ASCII 输入（HTTP 头经 latin-1 解码放行 0x80-0xFF）compare_digest 会
        # TypeError → 未认证 500（审查 P3）；直接按未授权处理
        try:
            if secrets.compare_digest(provided, configured):
                return True, ""
        except TypeError:
            return False, "unauthorized"
    return False, "unauthorized"


def extract_token(headers) -> str | None:
    """从请求头提取 token：优先 X-Admin-Token，兼容 Authorization: Bearer"""
    direct = headers.get("x-admin-token")
    if direct:
        return direct
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None
