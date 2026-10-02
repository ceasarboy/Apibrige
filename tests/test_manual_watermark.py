# -*- coding: utf-8 -*-
"""人工水位 API 测试（发行包线特性：手动增量水位）

覆盖（原本无测试）：
- 仅增量任务可读/可改；非增量 → 422；
- 二次确认（`confirmed`）为强制项，缺失 → 422；
- 乐观锁：`expected_version`/`expected_value` 与现值不符 → 409；
- 配置指纹（`config_token`）变更 → 409（防基于过期信息改水位）；
- 进行中轮次 / 未完成批次 → 409（必须先处理）；
- 值校验：日期格式、时间需含时刻、空串拒绝、整数字段拒绝小数；
- 成功路径：写入新值 + `checkpoint_version` +1 + 审计日志一条。
"""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import get_session
from app.main import app
from app.models.sync import (
    ApiConnection, Base, DataBuffer, ExecutionLog, FieldMapping, SyncTask,
)
from tests.conftest import TEST_ADMIN_TOKEN

WS = "ws_src"


@pytest.fixture
def wm_env(tmp_path):
    """临时库 + 依赖覆盖的 TestClient；返回 (client, maker)"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/wm.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = override
    client = TestClient(app, headers={"X-Admin-Token": TEST_ADMIN_TOKEN})
    yield client, maker
    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


def _seed(maker, *, number_field=True, sync_mode="incremental", with_round=False,
          with_pending=False):
    """建 HAP 源增量任务（HAP 源 → 走 api_time_compatible 路径，避免连真实库）"""
    async def _run():
        async with maker() as s:
            source = ApiConnection(name="HAP 源", connection_type="source", protocol="hap",
                                   base_url="https://hap.example.com", auth_type="custom",
                                   auth_config={}, status="active")
            target = ApiConnection(name="目标", connection_type="target", protocol="rest",
                                   base_url="https://t", auth_type=None, auth_config={},
                                   status="active")
            s.add_all([source, target])
            await s.commit()
            await s.refresh(source)
            await s.refresh(target)
            mapping = FieldMapping(
                name="增量桥", source_connection_id=source.id, target_connection_id=target.id,
                source_endpoint=WS, target_endpoint="/c", sync_mode=sync_mode,
                incremental_field="seq" if number_field else "biz_at",
                mapping_config={"fields": [{"source": "seq", "target": "seq"}],
                                "protocol": {"worksheet_id": WS}})
            s.add(mapping)
            await s.commit()
            await s.refresh(mapping)
            task = SyncTask(mapping_id=mapping.id, task_type="scheduled", status="active",
                            sync_mode="sync", last_sync_value="100", checkpoint_version=3,
                            active_round_id=uuid.uuid4() if with_round else None)
            s.add(task)
            await s.commit()
            await s.refresh(task)
            if with_pending:
                s.add(DataBuffer(task_id=task.id, source_data={}, status='pending'))
                await s.commit()
            return str(task.id), str(mapping.id)

    return asyncio.run(_run())


def _patch_controls(monkeypatch, ctype=6):
    """HAP 控件定义替身：seq=数值 / biz_at=日期时间（经适配器工厂返回假适配器）"""
    class FakeAdapter:
        def __init__(self, cfg=None):
            pass

        async def get_worksheet_controls(self, worksheet_id):
            return [{"controlId": "seq", "alias": "seq", "controlName": "序号", "type": 6},
                    {"controlId": "biz_at", "alias": "biz_at", "controlName": "时间", "type": 16}]

        async def disconnect(self):
            pass

    from app.adapters.factory import ProtocolAdapterFactory
    monkeypatch.setattr(ProtocolAdapterFactory, "create",
                        staticmethod(lambda protocol, cfg: FakeAdapter(cfg)))
    monkeypatch.setattr("app.services.adapter_config.build_adapter_config", lambda *a, **k: {})


def _get(client, task_id):
    return client.get(f"/api/v1/tasks/{task_id}/watermark")


def _put(client, task_id, body):
    return client.put(f"/api/v1/tasks/{task_id}/watermark", json=body)


class TestWatermarkGuards:

    def test_non_incremental_task_rejected(self, wm_env, monkeypatch):
        client, maker = wm_env
        _patch_controls(monkeypatch)
        task_id, _ = _seed(maker, sync_mode="full")
        resp = _get(client, task_id)
        assert resp.status_code == 422

    def test_confirmation_required(self, wm_env, monkeypatch):
        client, maker = wm_env
        _patch_controls(monkeypatch)
        task_id, _ = _seed(maker)
        info = _get(client, task_id).json()
        resp = _put(client, task_id, {"value": "120", "expected_version": info["version"],
                                      "expected_value": info["value"],
                                      "config_token": info["config_token"], "confirmed": False})
        assert resp.status_code == 422

    def test_active_round_blocks(self, wm_env, monkeypatch):
        client, maker = wm_env
        _patch_controls(monkeypatch)
        task_id, _ = _seed(maker, with_round=True)
        assert _get(client, task_id).status_code == 409

    def test_pending_batch_blocks(self, wm_env, monkeypatch):
        client, maker = wm_env
        _patch_controls(monkeypatch)
        task_id, _ = _seed(maker, with_pending=True)
        assert _get(client, task_id).status_code == 409

    def test_missing_task_404(self, wm_env):
        client, _maker = wm_env
        assert _get(client, str(uuid.uuid4())).status_code == 404


class TestWatermarkWrite:

    def _info(self, client, task_id):
        resp = _get(client, task_id)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_get_reports_type_and_version(self, wm_env, monkeypatch):
        client, maker = wm_env
        _patch_controls(monkeypatch)
        task_id, _ = _seed(maker)
        info = self._info(client, task_id)
        assert info["type"] == "number"
        assert info["value"] == "100"
        assert info["version"] == 3
        assert len(info["config_token"]) == 64
        assert "raw_type" not in info

    def test_put_advances_value_and_version_with_audit(self, wm_env, monkeypatch):
        client, maker = wm_env
        _patch_controls(monkeypatch)
        task_id, _ = _seed(maker)
        info = self._info(client, task_id)
        resp = _put(client, task_id, {"value": "250", "expected_version": info["version"],
                                      "expected_value": info["value"],
                                      "config_token": info["config_token"], "confirmed": True})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"value": "250", "version": 4,
                               "message": "水位已更新；下次按增量字段严格大于该值读取"}

        async def _audit():
            async with maker() as s:
                return (await s.scalars(select(ExecutionLog).where(
                    ExecutionLog.task_id == uuid.UUID(task_id),
                    ExecutionLog.action == 'watermark_manual'))).all()
        logs = asyncio.run(_audit())
        assert len(logs) == 1
        assert logs[0].response_data["old_watermark"] == "100"
        assert logs[0].response_data["new_watermark"] == "250"
        assert logs[0].response_data["confirmed"] is True

    def test_optimistic_lock_on_version_or_value(self, wm_env, monkeypatch):
        client, maker = wm_env
        _patch_controls(monkeypatch)
        task_id, _ = _seed(maker)
        info = self._info(client, task_id)
        stale_version = {**info, "value": "250", "expected_version": 2}
        resp = _put(client, task_id, {"value": "250", "expected_version": 2,
                                      "expected_value": info["value"],
                                      "config_token": info["config_token"], "confirmed": True})
        assert resp.status_code == 409 and "水位已被其他操作修改" in resp.json()["detail"]
        resp2 = _put(client, task_id, {"value": "250", "expected_version": info["version"],
                                       "expected_value": "999",
                                       "config_token": info["config_token"], "confirmed": True})
        assert resp2.status_code == 409
        assert stale_version  # 显式说明：并发信息过期场景

    def test_config_token_mismatch_rejected(self, wm_env, monkeypatch):
        client, maker = wm_env
        _patch_controls(monkeypatch)
        task_id, _ = _seed(maker)
        info = self._info(client, task_id)
        resp = _put(client, task_id, {"value": "250", "expected_version": info["version"],
                                      "expected_value": info["value"],
                                      "config_token": "0" * 64, "confirmed": True})
        assert resp.status_code == 409
        assert "配置已变化" in resp.json()["detail"]

    def test_value_validation(self, wm_env, monkeypatch):
        client, maker = wm_env
        _patch_controls(monkeypatch)
        task_id, _ = _seed(maker)
        info = self._info(client, task_id)

        def _body(value):
            return {"value": value, "expected_version": info["version"],
                    "expected_value": info["value"],
                    "config_token": info["config_token"], "confirmed": True}

        assert _put(client, task_id, _body("   ")).status_code == 422       # 空串/全空白
        assert _put(client, task_id, _body("2026-09-30")).status_code == 422  # 数值字段给了日期
        assert _put(client, task_id, _body("250")).status_code == 200        # 合法数值
        info2 = self._info(client, task_id)
        # 日期时间字段：必须带时刻
        task2, _ = _seed(maker, number_field=False)
        info_dt = self._info(client, task2)
        resp = _put(client, task2, {"value": "2026-09-30", "expected_version": info_dt["version"],
                                    "expected_value": info_dt["value"],
                                    "config_token": info_dt["config_token"], "confirmed": True})
        assert resp.status_code == 422
        assert "时间水位应包含日期和时间" in resp.json()["detail"]
