# -*- coding: utf-8 -*-
"""PostgreSQL 部署级复现：UtcTimestamp 修复验证（临时脚本，跑完即删）

失败模式（修复前）：asyncpg 对 TIMESTAMP WITHOUT TIME ZONE 拒绝 aware 绑定
—— ORM 默认值 _utcnow() 是 aware → 任何 INSERT 在编码器处 TypeError。
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

import asyncpg

# 凭据全部走环境变量（发布要求：仓库内不得有硬编码凭据）
PG = dict(host=os.environ.get("PGHOST", "127.0.0.1"),
          port=int(os.environ.get("PGPORT", "5432")),
          user=os.environ.get("PGUSER", "postgres"),
          password=os.environ.get("PGPASSWORD", ""))
URL = os.environ.get(
    "APIBRIDGE_DATABASE_URL",
    f"postgresql+asyncpg://{PG['user']}:{PG['password']}@{PG['host']}:{PG['port']}/apibridge_pgcheck")
SCRATCH = "apibridge_pgcheck"


async def reset_db():
    conn = await asyncpg.connect(database="postgres", **{k: v for k, v in PG.items() if k != "database"})
    await conn.execute(f'DROP DATABASE IF EXISTS {SCRATCH}')
    await conn.execute(f'CREATE DATABASE {SCRATCH}')
    await conn.close()
    print("[1] 临时库已重建:", SCRATCH)


async def native_asyncpg_contrast():
    """对照实验：asyncpg 原生编码器至今拒绝 aware（证明失败模式真实存在）"""
    conn = await asyncpg.connect(database=SCRATCH, **{k: v for k, v in PG.items() if k != "database"})
    aware = datetime.now(timezone.utc)
    await conn.execute("CREATE TABLE raw_t (ts TIMESTAMP)")
    try:
        await conn.execute("INSERT INTO raw_t VALUES ($1)", aware)
        print("[2] 原生 aware 绑定: 意外成功（?）")
    except Exception as e:
        # asyncpg 0.31 在编码器抛 DataError（消息即 naive/aware 相减 TypeError）
        print("[2] 原生 aware 绑定仍被拒（失败模式确认）:", type(e).__name__,
              "|", "offset-naive" in str(e) or "offset-aware" in str(e))


async def orm_roundtrip():
    """修复验证：用**产品真实引擎**（app.database，含 json_serializer）全默认值插入"""
    os.environ["APIBRIDGE_DATABASE_URL"] = URL
    from sqlalchemy import select
    from app.database import async_session, engine   # 导入期即按 env 建 PG 引擎
    from app.models.sync import Base, ApiConnection, DataBuffer, ProbeSession, SyncTask, FieldMapping
    from app.models.bi import BiMetricBucket, BiAggregationWatermark

    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    print("[3] create_all（35 个时间列）在 PG 建表成功")

    async with async_session() as s:
        # a) 连接：created_at/updated_at 默认值 = _utcnow()（aware）——修复前必炸
        conn = ApiConnection(name="pgcheck", connection_type="source",
                             protocol="rest", status="active")
        s.add(conn)
        await s.flush()
        # b) buffer：JSON 列带 datetime/Decimal（引擎级 json_serializer）；
        #    PG 强制外键 → 先建真实 mapping/task 再引用
        mapping = FieldMapping(name="pgcheck-m", source_connection_id=conn.id,
                               target_connection_id=conn.id, mapping_config={})
        s.add(mapping)
        await s.flush()
        task = SyncTask(mapping_id=mapping.id, task_type="realtime",
                        sync_mode="sync")
        s.add(task)
        await s.flush()
        buf = DataBuffer(task_id=task.id, source_data={
            "rows": [{"ts": datetime(2026, 10, 1, 8, 0), "amt": Decimal("1.5")}]},
            status="completed")
        s.add(buf)
        # c) 探针会话：ProbeTimestamp（+08 语义）也要在 PG 上往返
        probe = ProbeSession(status="draft", source_snapshot={"base_url": "https://x"})
        s.add(probe)
        # d) BI 桶 + 水位线
        s.add(BiMetricBucket(metric_key="m", bucket_start=datetime(2026, 10, 1),
                             dim_hash="h", dimensions={"产线": "A"}, values={"n": 1}))
        s.add(BiAggregationWatermark(metric_key="m",
                                     last_bucket=datetime(2026, 10, 1)))
        await s.commit()

    async with async_session() as s:
        conn = (await s.execute(select(ApiConnection))).scalars().first()
        assert conn.created_at.tzinfo is None, "PG 回读应为 naive（TIMESTAMP WITHOUT TIME ZONE）"
        lag = abs((datetime.now(timezone.utc).replace(tzinfo=None) - conn.created_at).total_seconds())
        assert lag < 60, f"时间值异常: {conn.created_at}"
        print(f"[4] ApiConnection INSERT/SELECT 成功；naive UTC 回读，与当前时差 {lag:.2f}s")

        buf = (await s.execute(select(DataBuffer))).scalars().first()
        assert isinstance(buf.source_data["rows"][0]["amt"], str)  # Decimal→str（default=str）
        print("[5] DataBuffer JSON 列（datetime/Decimal）写入/读回成功")

        probe = (await s.execute(select(ProbeSession))).scalars().first()
        print("[6] ProbeSession（ProbeTimestamp +08）写入成功:", probe.created_at)

        bucket = (await s.execute(select(BiMetricBucket))).scalars().first()
        wm = await s.get(BiAggregationWatermark, "m")
        print("[7] BiMetricBucket/水位线 写入成功:", bucket.bucket_start, "/", wm.last_bucket)

    await engine.dispose()
    print("== PG 部署级验证全部通过 ==")


async def main():
    await reset_db()
    await native_asyncpg_contrast()
    await orm_roundtrip()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
