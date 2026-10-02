"""BI 冒烟：造中间数据与执行日志，验证聚合端到端（临时脚本，用完即删）

用法：
    APIBRIDGE_DATABASE_URL=sqlite+aiosqlite:///./data/bi_smoke.db \
        python scripts/bi_smoke_seed.py
"""
import asyncio
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 允许直接以脚本方式运行（把项目根加入 import 路径）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault(
    "APIBRIDGE_DATABASE_URL", "sqlite+aiosqlite:///./data/bi_smoke.db")

from app.database import async_session  # noqa: E402
from app.models.sync import (  # noqa: E402
    ApiConnection, DataBuffer, ExecutionLog, FieldMapping, SyncTask,
)

CATEGORIES = ["注塑件", "五金件", "电子料", "包装材"]
REGIONS = ["华东", "华南", "华北"]
STATUSES = ["completed", "completed", "completed", "completed", "failed"]


async def main(rows: int = 3000) -> None:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    base = now - timedelta(minutes=10)

    async with async_session() as s:
        src = ApiConnection(name="MES库", connection_type="source",
                            protocol="oracle", db_type="oracle")
        tgt = ApiConnection(name="明道云", connection_type="target", protocol="hap")
        s.add_all([src, tgt])
        await s.flush()

        mp = FieldMapping(name="MES→明道云", source_connection_id=src.id,
                          target_connection_id=tgt.id, mapping_config={})
        s.add(mp)
        await s.flush()

        task = SyncTask(mapping_id=mp.id, task_type="manual")
        s.add(task)
        await s.flush()

        for i in range(rows):
            ts = base + timedelta(seconds=i % 600)
            status = random.choice(STATUSES)
            payload = {
                "material": f"物料{i % 300}",
                "category": random.choice(CATEGORIES),
                "region": random.choice(REGIONS),
                "customer": f"客户{i % 40}",
                "amount": round(random.uniform(10, 5000), 2),
                "qty": random.randint(1, 500),
                "plan_no": f"JH{i:06d}",
            }
            s.add(DataBuffer(task_id=task.id, source_data=payload,
                             transformed_data=payload, status=status, created_at=ts))
            s.add(ExecutionLog(
                task_id=task.id, action=random.choice(["fetch", "transform", "push"]),
                status="success" if status == "completed" else "failed",
                duration_ms=random.randint(20, 3000),
                error_message=None if status == "completed" else "连接超时 timeout after 30s",
                created_at=ts,
            ))

        await s.commit()
    print(f"已写入 {rows} 条中间数据 + {rows} 条执行日志")


if __name__ == "__main__":
    asyncio.run(main(int(os.environ.get("SMOKE_ROWS", "3000"))))
