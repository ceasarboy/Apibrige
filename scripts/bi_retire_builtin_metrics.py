# -*- coding: utf-8 -*-
"""内置 BI 指标一次性撤屏收敛（2026-09-25 重定位拍板）

把存量库中 builtin=True 且仍在启用的内置指标停用（幂等，可重复执行）。
用户手动启用任何定义后不受影响（本脚本按需运行一次，非常驻逻辑）。

用法:
    APIBRIDGE_DATABASE_URL="sqlite+aiosqlite:///./data/smoke_case1.db" \
        python scripts/bi_retire_builtin_metrics.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, update  # noqa: E402


async def main() -> int:
    from app.models.bi import BiMetricDef
    from app.database import async_session

    async with async_session() as session:
        rows = (await session.execute(
            select(BiMetricDef).where(
                BiMetricDef.builtin.is_(True),
                BiMetricDef.enabled.is_(True),
            ))).scalars().all()
        if not rows:
            print("无启用中的内置指标，无需收敛")
            return 0
        keys = [r.metric_key for r in rows]
        await session.execute(
            update(BiMetricDef)
            .where(BiMetricDef.builtin.is_(True),
                   BiMetricDef.enabled.is_(True))
            .values(enabled=False))
        await session.commit()
        print(f"已停用 {len(keys)} 个内置指标: {keys}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
