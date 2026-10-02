"""探针会话生命周期 — 状态迁移、审计、过期

状态机迁移表在 app/models/sync.py::PROBE_TRANSITIONS；
本模块提供受约束的迁移函数与过期清扫（task 3.1 / 3.12）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.probe.timestamps import as_east_eight
from app.models.sync import PROBE_TRANSITIONS, ProbeSession

# 进入这些状态后不再自动过期
_TERMINAL_OR_REVIEW = {"pending_review", "approved", "committed", "rejected", "failed", "expired"}


class InvalidTransition(Exception):
    """非法状态迁移"""

    def __init__(self, current: str, to: str):
        self.current = current
        self.to = to
        super().__init__(f"非法状态迁移: {current} → {to}")


def transition(session: ProbeSession, to: str, via: str = "api",
               summary: str = "") -> ProbeSession:
    """受约束的状态迁移：非法迁移抛 InvalidTransition，会话保持原状态

    迁移记录写入 audit_log（时间、来源、迁移、摘要）。
    """
    allowed = PROBE_TRANSITIONS.get(session.status, set())
    if to not in allowed:
        raise InvalidTransition(session.status, to)

    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "from": session.status,
        "to": to,
        "via": via,
        "summary": summary[:200],
    }
    audit = list(session.audit_log or [])
    audit.append(entry)
    session.audit_log = audit
    session.status = to
    session.updated_at = datetime.now(timezone.utc)
    return session


def ensure_expires_at(session: ProbeSession) -> ProbeSession:
    """创建/重置会话时按配置写有效期（默认 7 天）"""
    if session.expires_at is None:
        session.expires_at = datetime.now(timezone.utc) + timedelta(
            days=settings.probe_session_ttl_days
        )
    return session


def _as_utc(ts: datetime | None) -> datetime | None:
    """把读回的时间戳归一为 UTC aware

    SQLite 的 TIMESTAMP 列回读是 **naive** datetime（无 tzinfo），而本模块的时间基准
    都是 aware（`datetime.now(timezone.utc)`）——直接比较会抛
    `TypeError: can't compare offset-naive and offset-aware datetimes`
    （2026-09-29 浏览器实测抓到：待确认清单页整页 500）。与 bi_aggregator.to_utc 同口径。
    """
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


async def expire_stale_sessions(session: AsyncSession) -> int:
    """把超期未进清单的会话标记为 expired（task 3.12）

    供定时任务或清单查询前懒清扫调用。返回过期数量。
    """
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(ProbeSession).where(
            ProbeSession.expires_at.isnot(None),
            ProbeSession.status.not_in(_TERMINAL_OR_REVIEW),
        )
    )
    count = 0
    for stale in result.scalars().all():
        # Historical SQLite rows may mix naive local times and explicit offsets.
        # Filter instants here: lexical SQL comparison can miss already-expired rows.
        # 口径：无时区值按 UTC+08:00 解析（app/probe/timestamps.py 的统一约定）
        if not (stale.expires_at and as_east_eight(stale.expires_at) < now):
            continue
        if stale.status in _TERMINAL_OR_REVIEW:
            continue
        previous_status = stale.status
        stale.status = "expired"
        audit = list(stale.audit_log or [])
        audit.append({
            "at": now.isoformat(), "from": previous_status, "to": "expired",
            "via": "scheduler", "summary": "超过有效期未提交清单，自动过期",
        })
        stale.audit_log = audit
        stale.updated_at = now
        count += 1
    if count:
        await session.commit()
    return count
