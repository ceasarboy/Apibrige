# -*- coding: utf-8 -*-
"""增量轮次管线测试（发行包线核心：先封存 → 串行处理 → 整轮成功才提交水位）

覆盖（原本无测试）：
- 水位只在**整轮批次全部完成**后提交，并推进 checkpoint_version、清 active_round_id；
- 批次清单不完整 / 水位版本冲突 / 候选水位回退 → blocked，水位保持原值；
- 重试上限为 0 时禁止重发已尝试批次（`NO_RETRY_MESSAGE`）；
- `start_round` 复用活动轮次；旧版未完成批次（round_id 为空）拒绝开新轮；
- 批次串行：前序批次未完成时后续批次不得处理；
- 失败退避：可重试 → retry_wait + next_retry_at；不可重试 → blocked。
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.sync import (
    ApiConnection, Base, DataBuffer, FieldMapping, SyncRound, SyncTask,
)
from app.services import incremental_rounds as rounds
from app.services.incremental import seal, typed, unseal

UTC = timezone.utc


# ============================================================
# 夹具：真实 SQLite + 轮次管线打桩（不派发 Celery、不推送目标、不碰 Redis）
# ============================================================

@pytest.fixture
def round_env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/rounds.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(rounds, "session_factory", maker)
    monkeypatch.setattr(rounds, "dispatch", lambda buffer_id: None)   # 不打 Celery
    monkeypatch.setattr("app.cache.rate_limiter.rate_limit",
                        AsyncMockTrue())                          # 不限流
    yield maker
    asyncio.run(engine.dispose())


def AsyncMockTrue():
    async def _ok(*a, **k):
        return True
    return _ok


async def _seed(maker, *, batch_size=2, last_sync_value="100", target_protocol="rest"):
    """建任务/映射/连接（REST 目标 + batch_atomic，满足增量契约）"""
    async with maker() as s:
        source = ApiConnection(name="源库", connection_type="source", protocol="sqlite",
                               db_type="sqlite", db_config={"database": "x"}, status="active")
        target = ApiConnection(name="目标", connection_type="target", protocol=target_protocol,
                               base_url="https://t", auth_type=None, auth_config={},
                               status="active")
        s.add_all([source, target])
        await s.commit()
        await s.refresh(source)
        await s.refresh(target)
        mapping = FieldMapping(
            name="增量桥", source_connection_id=source.id, target_connection_id=target.id,
            source_table="orders", target_table="orders", sync_mode="incremental",
            incremental_field="id", batch_size=batch_size,
            mapping_config={"fields": [{"source": "id", "target": "id"}],
                            "key_field": "id",
                            "protocol": {"batch_atomic": True}})
        s.add(mapping)
        await s.commit()
        await s.refresh(mapping)
        task = SyncTask(mapping_id=mapping.id, task_type="scheduled", status="active",
                        sync_mode="async", last_sync_value=last_sync_value,
                        checkpoint_version=0)
        s.add(task)
        await s.commit()
        await s.refresh(task)
        return str(task.id), str(mapping.id), str(source.id), str(target.id)


def _patch_snapshot(monkeypatch, rows, candidate, kind="number"):
    async def fake_read_snapshot(source, mapping, from_watermark):
        return list(rows), candidate, kind
    monkeypatch.setattr("app.connectors.incremental_snapshot.read_snapshot", fake_read_snapshot)
    monkeypatch.setattr(rounds, "read_snapshot", fake_read_snapshot)


def _patch_push(monkeypatch, behavior=None):
    """push_batch 打桩；behavior(records) 可抛异常以模拟失败"""
    calls = []

    async def fake_push(target, mapping, records, batch_id):
        calls.append({'batch_id': str(batch_id), 'records': list(records)})
        if behavior is not None:
            behavior(records)
        return {'status_code': 200, 'body': {'row_ids': ['x']}}

    monkeypatch.setattr(rounds, "push_batch", fake_push)
    return calls


async def _start(maker, monkeypatch, rows, candidate, **seed_kw):
    _patch_snapshot(monkeypatch, rows, candidate)
    _patch_push(monkeypatch)
    task_id, mapping_id, _src, _tgt = await _seed(maker, **seed_kw)
    async with maker() as s:
        task = await s.get(SyncTask, uuid.UUID(task_id))
        mapping = await s.get(FieldMapping, uuid.UUID(mapping_id))
        source = await s.get(ApiConnection, mapping.source_connection_id)
        target = await s.get(ApiConnection, mapping.target_connection_id)
        result = await rounds.start_round(s, task, mapping, source, target)
    return task_id, result


async def _batches(maker, run_id):
    async with maker() as s:
        return (await s.scalars(select(DataBuffer).where(
            DataBuffer.round_id == uuid.UUID(str(run_id))).order_by(DataBuffer.batch_no))).all()


async def _task(maker, task_id):
    async with maker() as s:
        return await s.get(SyncTask, uuid.UUID(str(task_id)))


async def _run(maker, run_id):
    async with maker() as s:
        return await s.get(SyncRound, uuid.UUID(str(run_id)))


_ROWS = [{"id": i} for i in range(1, 6)]        # 5 行 / batch_size=2 → 3 批


# ============================================================
# 水位提交语义
# ============================================================

class TestWatermarkCommit:

    def test_watermark_only_after_all_batches(self, round_env, monkeypatch):
        async def _run_all():
            task_id, started = await _start(round_env, monkeypatch, _ROWS, 105)
            run_id = started['round_id']
            batches = await _batches(round_env, run_id)
            assert [b.batch_no for b in batches] == [0, 1, 2]
            assert started['status'] == 'running'

            # 只跑前两批 → 水位不动
            for batch in batches[:2]:
                await rounds.process_batch(batch.id)
            task = await _task(round_env, task_id)
            run = await _run(round_env, run_id)
            assert task.last_sync_value == "100", "整轮未完成不得推进水位"
            assert task.checkpoint_version == 0
            assert run.status == 'running'

            # 最后一批完成 → 提交水位 + 版本 +1 + 释放活动轮次
            await rounds.process_batch(batches[2].id)
            task = await _task(round_env, task_id)
            run = await _run(round_env, run_id)
            assert task.last_sync_value == "105"
            assert task.checkpoint_version == 1
            assert task.active_round_id is None
            assert run.status == 'completed'

        asyncio.run(_run_all())

    def test_candidate_rollback_rejected(self, round_env, monkeypatch):
        """候选水位小于原水位 → blocked，水位保持原值"""
        async def _run_all():
            task_id, started = await _start(round_env, monkeypatch, _ROWS, 50)
            run_id = started['round_id']
            for batch in await _batches(round_env, run_id):
                await rounds.process_batch(batch.id)
            task = await _task(round_env, task_id)
            run = await _run(round_env, run_id)
            assert task.last_sync_value == "100"
            assert run.status == 'blocked'
            assert '回退' in (run.error_message or '')

        asyncio.run(_run_all())

    def test_incomplete_batch_list_blocks(self, round_env, monkeypatch):
        """轮次声明 3 批但库里只有 2 批 → blocked，水位不动"""
        async def _run_all():
            task_id, started = await _start(round_env, monkeypatch, _ROWS, 105)
            run_id = started['round_id']
            batches = await _batches(round_env, run_id)
            async with round_env() as s:
                await s.delete(batches[2])
                await s.commit()
            await rounds.advance(uuid.UUID(str(run_id)))
            task = await _task(round_env, task_id)
            run = await _run(round_env, run_id)
            assert task.last_sync_value == "100"
            assert run.status == 'blocked'
            assert '批次清单不完整' in (run.error_message or '')

        asyncio.run(_run_all())

    def test_checkpoint_version_conflict_blocks(self, round_env, monkeypatch):
        """轮次期间水位版本被外部改动 → 即便批次都完成也拒绝提交"""
        async def _run_all():
            task_id, started = await _start(round_env, monkeypatch, _ROWS, 105)
            run_id = started['round_id']
            async with round_env() as s:
                task = await s.get(SyncTask, uuid.UUID(task_id))
                task.checkpoint_version += 1          # 模拟并发改动（先改，再跑完批次）
                await s.commit()
            for batch in await _batches(round_env, run_id):
                await rounds.process_batch(batch.id)
            run = await _run(round_env, run_id)
            task = await _task(round_env, task_id)
            assert run.status == 'blocked'
            assert '版本冲突' in (run.error_message or '')
            assert task.last_sync_value == "100"      # 水位仍未推进

        asyncio.run(_run_all())


# ============================================================
# 开轮约束与串行
# ============================================================

class TestRoundGuards:

    def test_start_round_reuses_active_round(self, round_env, monkeypatch):
        async def _run_all():
            _patch_snapshot(monkeypatch, _ROWS, 105)
            _patch_push(monkeypatch)
            task_id, mapping_id, _s, _t = await _seed(round_env)
            async with round_env() as s:
                task = await s.get(SyncTask, uuid.UUID(task_id))
                mapping = await s.get(FieldMapping, uuid.UUID(mapping_id))
                source = await s.get(ApiConnection, mapping.source_connection_id)
                target = await s.get(ApiConnection, mapping.target_connection_id)
                first = await rounds.start_round(s, task, mapping, source, target)
                second = await rounds.start_round(s, task, mapping, source, target)
            assert second['reused'] is True
            assert second['round_id'] == first['round_id']

        asyncio.run(_run_all())

    def test_legacy_pending_batch_rejected(self, round_env, monkeypatch):
        """存在旧版未完成批次（无 round_id）→ 拒绝开新轮（防止其被当成旧数据跳过）"""
        async def _run_all():
            _patch_snapshot(monkeypatch, _ROWS, 105)
            _patch_push(monkeypatch)
            task_id, mapping_id, _s, _t = await _seed(round_env)
            async with round_env() as s:
                s.add(DataBuffer(task_id=uuid.UUID(task_id), source_data={"data": []},
                                 status='pending'))
                await s.commit()
                task = await s.get(SyncTask, uuid.UUID(task_id))
                mapping = await s.get(FieldMapping, uuid.UUID(mapping_id))
                source = await s.get(ApiConnection, mapping.source_connection_id)
                target = await s.get(ApiConnection, mapping.target_connection_id)
                with pytest.raises(HTTPException) as ei:
                    await rounds.start_round(s, task, mapping, source, target)
            assert ei.value.detail['code'] == 'incremental_legacy_pending'

        asyncio.run(_run_all())

    def test_second_batch_waits_for_first(self, round_env, monkeypatch):
        """批次串行：前序未完成时后一批不得进入处理"""
        async def _run_all():
            task_id, started = await _start(round_env, monkeypatch, _ROWS, 105)
            batches = await _batches(round_env, started['round_id'])
            await rounds.process_batch(batches[1].id)      # 跳过第 0 批
            after = await _batches(round_env, started['round_id'])
            assert after[1].status == 'pending', "前序未完成不得处理后续批次"
            assert after[1].attempt_token is None

        asyncio.run(_run_all())

    def test_paused_task_does_not_process(self, round_env, monkeypatch):
        async def _run_all():
            task_id, started = await _start(round_env, monkeypatch, _ROWS, 105)
            batches = await _batches(round_env, started['round_id'])
            async with round_env() as s:
                task = await s.get(SyncTask, uuid.UUID(task_id))
                task.status = 'paused'
                await s.commit()
            await rounds.process_batch(batches[0].id)
            after = await _batches(round_env, started['round_id'])
            assert after[0].status == 'pending'

        asyncio.run(_run_all())


# ============================================================
# 失败与重试
# ============================================================

class TestRetrySemantics:

    def test_failure_with_retry_budget_waits(self, round_env, monkeypatch):
        """有重试预算：批次 failed + 退避时间，轮次 retry_wait（水位不动）"""
        async def _run_all():
            _patch_snapshot(monkeypatch, _ROWS, 105)
            def boom(records):
                raise RuntimeError("目标 500")
            _patch_push(monkeypatch, behavior=boom)
            task_id, mapping_id, _s, _t = await _seed(round_env)
            async with round_env() as s:
                task = await s.get(SyncTask, uuid.UUID(task_id))
                mapping = await s.get(FieldMapping, uuid.UUID(mapping_id))
                source = await s.get(ApiConnection, mapping.source_connection_id)
                target = await s.get(ApiConnection, mapping.target_connection_id)
                # REST 目标带幂等头 → 重试预算 > 0
                # （JSON 列要整体重新赋值才会被 ORM 追踪）
                mapping.mapping_config = {**mapping.mapping_config,
                                          'protocol': {'batch_atomic': True,
                                                       'idempotency_header': 'X-Batch-Id'}}
                await s.commit()
                started = await rounds.start_round(s, task, mapping, source, target)
            batches = await _batches(round_env, started['round_id'])
            await rounds.process_batch(batches[0].id)
            after = await _batches(round_env, started['round_id'])
            run = await _run(round_env, started['round_id'])
            assert after[0].status == 'failed'
            assert after[0].next_retry_at is not None
            assert run.status == 'retry_wait'
            task = await _task(round_env, task_id)
            assert task.last_sync_value == "100"

        asyncio.run(_run_all())

    def test_retry_limit_zero_blocks_resend(self, round_env, monkeypatch):
        """重试上限 0：已尝试批次禁止重发，轮次 blocked 并给出明确文案"""
        async def _run_all():
            task_id, started = await _start(round_env, monkeypatch, _ROWS, 105)
            run_id = started['round_id']
            batches = await _batches(round_env, run_id)
            async with round_env() as s:
                run = await s.get(SyncRound, uuid.UUID(str(run_id)))
                saved = unseal(run.config_snapshot)
                saved['retry_limit'] = 0                    # 冻结 0 次重试
                run.config_snapshot = seal(saved)
                batch = await s.get(DataBuffer, batches[0].id)
                batch.status, batch.retry_count = 'failed', 1
                run.status = 'blocked'
                await s.commit()

            # 重试入口：应拒绝（不能重发已尝试批次）
            async with round_env() as s:
                task = await s.get(SyncTask, uuid.UUID(task_id))
                with pytest.raises(HTTPException) as ei:
                    await rounds.retry_round(s, task)
            assert ei.value.detail['code'] == 'incremental_retry_disabled'
            assert rounds.NO_RETRY_MESSAGE in ei.value.detail['message']

        asyncio.run(_run_all())

    def test_retry_resets_batch_to_pending_when_budget_allows(self, round_env, monkeypatch):
        async def _run_all():
            task_id, started = await _start(round_env, monkeypatch, _ROWS, 105)
            run_id = started['round_id']
            batches = await _batches(round_env, run_id)
            _patch_push(monkeypatch)
            async with round_env() as s:
                run = await s.get(SyncRound, uuid.UUID(str(run_id)))
                saved = unseal(run.config_snapshot)
                saved['retry_limit'] = 2
                run.config_snapshot = seal(saved)
                batch = await s.get(DataBuffer, batches[0].id)
                batch.status, batch.retry_count = 'failed', 1
                run.status = 'retry_wait'
                await s.commit()
            async with round_env() as s:
                task = await s.get(SyncTask, uuid.UUID(task_id))
                detail = await rounds.retry_round(s, task)
            assert detail['status'] in ('running', 'retry_wait')
            after = await _batches(round_env, run_id)
            assert after[0].status in ('pending', 'completed')

        asyncio.run(_run_all())

    def test_round_detail_reports_progress(self, round_env, monkeypatch):
        async def _run_all():
            task_id, started = await _start(round_env, monkeypatch, _ROWS, 105)
            async with round_env() as s:
                task = await s.get(SyncTask, uuid.UUID(task_id))
                detail = await rounds.round_detail(s, task, uuid.UUID(str(started['round_id'])))
            assert detail['round_id'] == started['round_id']
            assert detail['batches'] is not None
            assert len(detail['batches']) == 3
            assert detail['from_watermark'] == "100"
            assert detail['candidate_watermark'] == "105"

        asyncio.run(_run_all())
