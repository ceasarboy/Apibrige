# -*- coding: utf-8 -*-
"""HAP 目标删除同步 + push 去重 + delete_sync×async 守卫 测试"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.adapters.hap import HapV3Adapter


# ============================================================
# hap.get_rows_with_key：目标行 → {业务键: rowId}
# ============================================================

class TestHapGetRowsWithKey:
    """读取目标现有行的业务键 → rowId 映射"""

    @pytest.mark.asyncio
    async def test_extract_key_and_rowid(self):
        adapter = HapV3Adapter({"base_url": "https://api.mingdao.com"})
        rows = [
            {"rowid": "r1", "ctrl_key": "v1", "other": "x"},
            {"rowid": "r2", "ctrl_key": "  v2  ", "other": "y"},  # 值带空格 → strip
            {"rowid": "r3", "other": "z"},          # 缺 key 控件 → 跳过
            {"rowid": "r4", "ctrl_key": None},      # 值为 None → 跳过
            {"rowid": "r5", "ctrl_key": ""},        # 空值 → 跳过
            "not-a-dict",                           # 非 dict → 跳过
        ]
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(adapter, "get_worksheet_rows",
                       AsyncMock(return_value=rows))
            result = await adapter.get_rows_with_key("ws1", "ctrl_key")
        assert result == {"v1": "r1", "v2": "r2"}

    @pytest.mark.asyncio
    async def test_empty_rows(self):
        adapter = HapV3Adapter({"base_url": "https://api.mingdao.com"})
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(adapter, "get_worksheet_rows", AsyncMock(return_value=[]))
            result = await adapter.get_rows_with_key("ws1", "ctrl_key")
        assert result == {}


# ============================================================
# sync_handler._push_target：HAP 按 key_field 去重
# ============================================================

class _FakeConn:
    def __init__(self, protocol="hap", db_type=None):
        self.protocol = protocol
        self.db_type = db_type
        self.id = "c1"


class _FakeMapping:
    def __init__(self, mapping_config):
        self.mapping_config = mapping_config
        self.target_endpoint = None
        self.target_table = None


class TestHapPushDedup:
    """HAP 目标 push 去重（合并后实现：显式 hap_write_mode=deduplicate 才去重）

    合并背景：去重/对账收敛到 app/services/hap_target.py；未声明 hap_write_mode 的
    映射保持"旧追加模式不改变"。原有 get_rows_with_key 路径已下线。
    """

    @staticmethod
    def _adapter(existing: list[dict]):
        adapter = MagicMock()
        adapter.get_worksheet_controls = AsyncMock(
            return_value=[{"controlId": "ctrl_key", "alias": "ctrl_key"}])
        adapter.get_worksheet_rows = AsyncMock(return_value=existing)
        adapter.add_worksheet_row = AsyncMock(return_value="new-row")
        return adapter

    @pytest.mark.asyncio
    async def test_deduplicate_skips_existing(self):
        from app.services import hap_target

        adapter = self._adapter([
            {"rowid": "r1", "ctrl_key": "v1"}, {"rowid": "r2", "ctrl_key": "v2"}])
        mapping = _FakeMapping({"key_field": "ctrl_key",
                                "hap_write_mode": "deduplicate"})
        result = await hap_target.push(adapter, {"worksheet_id": "ws1"}, mapping,
                                       [{"ctrl_key": "v1"}, {"ctrl_key": "v3"}])
        adapter.add_worksheet_row.assert_awaited_once_with("ws1", {"ctrl_key": "v3"})
        assert result["body"]["skipped"] == 1
        assert result["body"]["row_ids"] == ["new-row"]

    @pytest.mark.asyncio
    async def test_all_existing_skips_push(self):
        from app.services import hap_target

        adapter = self._adapter([
            {"rowid": "r1", "ctrl_key": "v1"}, {"rowid": "r2", "ctrl_key": "v2"}])
        mapping = _FakeMapping({"key_field": "ctrl_key",
                                "hap_write_mode": "deduplicate"})
        result = await hap_target.push(adapter, {"worksheet_id": "ws1"}, mapping,
                                       [{"ctrl_key": "v1"}, {"ctrl_key": "v2"}])
        adapter.add_worksheet_row.assert_not_called()
        assert result["body"]["skipped"] == 2
        assert result["body"]["row_ids"] == []

    @pytest.mark.asyncio
    async def test_append_mode_no_dedup(self):
        """未声明 hap_write_mode → 不去重（旧追加模式不改变）"""
        from app.services import hap_target

        adapter = self._adapter([
            {"rowid": "r1", "ctrl_key": "v1"}, {"rowid": "r2", "ctrl_key": "v2"}])
        mapping = _FakeMapping({"key_field": "ctrl_key"})
        result = await hap_target.push(adapter, {"worksheet_id": "ws1"}, mapping,
                                       [{"ctrl_key": "v1"}, {"ctrl_key": "v2"}])
        assert adapter.add_worksheet_row.await_count == 2
        assert result["body"]["skipped"] == 0


# ============================================================
# sync_handler 删除同步：HAP 目标 stale 行删除
# ============================================================

class TestHapDeleteSync:
    """HAP 目标删除同步：目标中源已不存在的行按 rowId 删除"""

    @pytest.mark.asyncio
    async def test_removes_stale_rows(self, monkeypatch):
        import app.services.sync_handler as sh

        adapter = MagicMock()
        adapter.config = {"worksheet_id": "ws1"}
        adapter.get_worksheet_controls = AsyncMock(
            return_value=[{"controlId": "ctrl_key", "alias": "ctrl_key"}])
        adapter.get_worksheet_rows = AsyncMock(return_value=[
            {"rowid": "r1", "ctrl_key": "v1"},
            {"rowid": "r2", "ctrl_key": "v2"},
            {"rowid": "r3", "ctrl_key": "v3"},
        ])
        adapter.delete_worksheet_row = AsyncMock()
        monkeypatch.setattr(sh, "_api_adapter",
                            lambda *a, **k: (adapter, {"base_url": "https://x",
                                                       "worksheet_id": "ws1"}))
        monkeypatch.setattr(sh, "_fetch_source",
                            AsyncMock(return_value=[{"id": "v1"}, {"id": "v2"}]))
        monkeypatch.setattr(sh, "_push_target", AsyncMock(return_value={"rows": 2}))

        monkeypatch.setattr(sh.lock, "acquire_lock", AsyncMock(return_value="tok"))
        monkeypatch.setattr(sh.lock, "refresh_lock", AsyncMock(return_value=True))
        monkeypatch.setattr(sh.lock, "release_lock", AsyncMock(return_value=True))
        monkeypatch.setattr(sh, "is_duplicate", AsyncMock(return_value=False))
        monkeypatch.setattr(sh, "mark_processed", AsyncMock())
        monkeypatch.setattr(sh, "rate_limit", AsyncMock(return_value=True))
        monkeypatch.setattr(sh, "record_sync", MagicMock())
        monkeypatch.setattr(sh, "record_api_call", MagicMock())
        monkeypatch.setattr(sh, "log_execution", AsyncMock())

        class FMapping:
            id = "m1"
            sync_mode = "full"
            source_endpoint = None
            target_endpoint = None
            source_table = None
            target_table = "ws1"
            mapping_config = {"fields": [{"source": "id", "target": "ctrl_key",
                                          "transform": "string"}],
                              "delete_sync": True, "key_field": "ctrl_key"}
            incremental_field = None
            source_headers = None
            target_headers = None

        class FTask:
            id = "t1"
            # 合并后 HAP 目标策略校验会读 task.sync_mode / webhook_config
            # （删除对账仅支持全量 sync）
            sync_mode = "sync"
            webhook_config = None
            conflict_strategy = None
            fallback_to_async = False
            last_run_at = None
            last_sync_value = None
            task_type = "scheduled"

        session = MagicMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        result = await sh.sync_transfer(session, FTask(), FMapping(),
                                        _FakeConn(protocol="rest", db_type=None),
                                        _FakeConn(protocol="hap", db_type=None))
        assert result["status"] == "success"
        # stale = {"v3"} → 删除 r3
        adapter.delete_worksheet_row.assert_called_once_with("ws1", "r3")

    @pytest.mark.asyncio
    async def test_no_stale_no_delete(self, monkeypatch):
        import app.services.sync_handler as sh

        adapter = MagicMock()
        adapter.config = {"worksheet_id": "ws1"}
        adapter.get_worksheet_controls = AsyncMock(
            return_value=[{"controlId": "ctrl_key", "alias": "ctrl_key"}])
        adapter.get_worksheet_rows = AsyncMock(return_value=[
            {"rowid": "r1", "ctrl_key": "v1"},
            {"rowid": "r2", "ctrl_key": "v2"},
        ])
        adapter.delete_worksheet_row = AsyncMock()
        monkeypatch.setattr(sh, "_api_adapter",
                            lambda *a, **k: (adapter, {"base_url": "https://x",
                                                       "worksheet_id": "ws1"}))
        monkeypatch.setattr(sh, "_fetch_source",
                            AsyncMock(return_value=[{"id": "v1"}, {"id": "v2"}]))
        monkeypatch.setattr(sh, "_push_target", AsyncMock(return_value={"rows": 2}))

        monkeypatch.setattr(sh.lock, "acquire_lock", AsyncMock(return_value="tok"))
        monkeypatch.setattr(sh.lock, "refresh_lock", AsyncMock(return_value=True))
        monkeypatch.setattr(sh.lock, "release_lock", AsyncMock(return_value=True))
        monkeypatch.setattr(sh, "is_duplicate", AsyncMock(return_value=False))
        monkeypatch.setattr(sh, "mark_processed", AsyncMock())
        monkeypatch.setattr(sh, "rate_limit", AsyncMock(return_value=True))
        monkeypatch.setattr(sh, "record_sync", MagicMock())
        monkeypatch.setattr(sh, "record_api_call", MagicMock())
        monkeypatch.setattr(sh, "log_execution", AsyncMock())

        class FMapping:
            id = "m1"
            sync_mode = "full"
            source_endpoint = None
            target_endpoint = None
            source_table = None
            target_table = "ws1"
            mapping_config = {"fields": [{"source": "id", "target": "ctrl_key",
                                          "transform": "string"}],
                              "delete_sync": True, "key_field": "ctrl_key"}
            incremental_field = None
            source_headers = None
            target_headers = None

        class FTask:
            id = "t1"
            # 合并后 HAP 目标策略校验会读 task.sync_mode / webhook_config
            # （删除对账仅支持全量 sync）
            sync_mode = "sync"
            webhook_config = None
            conflict_strategy = None
            fallback_to_async = False
            last_run_at = None
            last_sync_value = None
            task_type = "scheduled"

        session = MagicMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        result = await sh.sync_transfer(session, FTask(), FMapping(),
                                        _FakeConn(protocol="rest", db_type=None),
                                        _FakeConn(protocol="hap", db_type=None))
        assert result["status"] == "success"
        adapter.delete_worksheet_row.assert_not_called()

    @pytest.mark.asyncio
    async def test_removes_stale_rows_with_dict_source(self, monkeypatch):
        """REST 源返回含 rows 包装键的 dict 时，删除同步仍应触发"""
        import app.services.sync_handler as sh

        adapter = MagicMock()
        adapter.config = {"worksheet_id": "ws1"}
        adapter.get_worksheet_controls = AsyncMock(
            return_value=[{"controlId": "ctrl_key", "alias": "ctrl_key"}])
        adapter.get_worksheet_rows = AsyncMock(return_value=[
            {"rowid": "r1", "ctrl_key": "v1"},
            {"rowid": "r2", "ctrl_key": "v2"},
            {"rowid": "r3", "ctrl_key": "v3"},
        ])
        adapter.delete_worksheet_row = AsyncMock()
        monkeypatch.setattr(sh, "_api_adapter",
                            lambda *a, **k: (adapter, {"base_url": "https://x",
                                                       "worksheet_id": "ws1"}))
        # REST 源：ListResponse 包装（rows 键），非 list
        monkeypatch.setattr(sh, "_fetch_source",
                            AsyncMock(return_value={"success": True,
                                                    "rows": [{"id": "v1"},
                                                             {"id": "v2"}],
                                                    "count": 2}))
        monkeypatch.setattr(sh, "_push_target", AsyncMock(return_value={"rows": 2}))

        monkeypatch.setattr(sh.lock, "acquire_lock", AsyncMock(return_value="tok"))
        monkeypatch.setattr(sh.lock, "refresh_lock", AsyncMock(return_value=True))
        monkeypatch.setattr(sh.lock, "release_lock", AsyncMock(return_value=True))
        monkeypatch.setattr(sh, "is_duplicate", AsyncMock(return_value=False))
        monkeypatch.setattr(sh, "mark_processed", AsyncMock())
        monkeypatch.setattr(sh, "rate_limit", AsyncMock(return_value=True))
        monkeypatch.setattr(sh, "record_sync", MagicMock())
        monkeypatch.setattr(sh, "record_api_call", MagicMock())
        monkeypatch.setattr(sh, "log_execution", AsyncMock())

        class FMapping:
            id = "m1"
            sync_mode = "full"
            source_endpoint = None
            target_endpoint = None
            source_table = None
            target_table = "ws1"
            mapping_config = {"fields": [{"source": "id", "target": "ctrl_key",
                                          "transform": "string"}],
                              "delete_sync": True, "key_field": "ctrl_key"}
            incremental_field = None
            source_headers = None
            target_headers = None

        class FTask:
            id = "t1"
            # 合并后 HAP 目标策略校验会读 task.sync_mode / webhook_config
            # （删除对账仅支持全量 sync）
            sync_mode = "sync"
            webhook_config = None
            conflict_strategy = None
            fallback_to_async = False
            last_run_at = None
            last_sync_value = None
            task_type = "scheduled"

        session = MagicMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        result = await sh.sync_transfer(session, FTask(), FMapping(),
                                        _FakeConn(protocol="rest", db_type=None),
                                        _FakeConn(protocol="hap", db_type=None))
        assert result["status"] == "success"
        # stale = {"v3"} → 删除 r3
        adapter.delete_worksheet_row.assert_called_once_with("ws1", "r3")


# ============================================================
# router：delete_sync × async 守卫
# ============================================================

class TestDeleteSyncAsyncGuard:
    """delete_sync 开启时 async/auto(异步分支) 必须拒绝，避免静默失效

    合并后守卫收敛到 app/services/hap_target.py::validate_policy（由 router 调用）：
    拒绝码为 hap_policy_mode；delete_sync 既有键与 hap_delete_sync 同等生效。
    """

    @pytest.mark.asyncio
    async def test_async_rejected(self, monkeypatch):
        import app.services.router as router
        monkeypatch.setattr(router, "check_dependency_ready",
                            AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(router.sync_handler, "sync_transfer",
                            AsyncMock(return_value={"status": "success"}))
        monkeypatch.setattr(router.async_handler, "submit_async",
                            AsyncMock(return_value="buf-1"))

        task = MagicMock(sync_mode="async", depends_on_task_id=None,
                         id="t1", auto_threshold=100, fallback_to_async=False,
                         webhook_config=None, conflict_strategy=None)
        mapping = MagicMock(mapping_config={"delete_sync": True, "key_field": "ctrl_key"},
                            sync_mode="full", id="m1")
        session = AsyncMock()

        # 合并后策略校验在路由入口抛 AdapterError（由上层记失败执行），
        # 关键不变量：异步分支不得被提交
        from app.adapters.errors import AdapterError
        with pytest.raises(AdapterError) as ei:
            await router.route_transfer(session, task, mapping,
                                        MagicMock(protocol="hap", db_type=None),
                                        MagicMock(protocol="hap", db_type=None))
        assert ei.value.code == "hap_policy_mode"
        router.async_handler.submit_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_delete_sync_db_target_rejected(self, monkeypatch):
        """合并回归补回：DB 目标 + delete_sync + async 通道 → 拒绝（不静默失效）"""
        import app.services.router as router
        monkeypatch.setattr(router, "check_dependency_ready",
                            AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(router.async_handler, "submit_async",
                            AsyncMock(return_value="buf-1"))

        task = MagicMock(sync_mode="async", depends_on_task_id=None,
                         id="t1", fallback_to_async=False, webhook_config=None)
        mapping = MagicMock(mapping_config={"delete_sync": True, "key_field": "id"},
                           sync_mode="full", id="m1")
        result = await router.route_transfer(AsyncMock(), task, mapping,
                                            MagicMock(protocol="rest", db_type=None),
                                            MagicMock(protocol="sqlite", db_type="sqlite"))
        assert result["status"] == "failed"
        assert "sync" in result["error"] and "delete_sync" in result["error"]
        router.async_handler.submit_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_hap_push_dedups_when_delete_sync_on(self):
        """P0：删除对账开启（任一键）时 push 强制按业务键去重——
        append 模式每轮全量重插会让目标键翻倍、此后 existing_rows 永久失败"""
        from app.services import hap_target

        adapter = MagicMock()
        adapter.get_worksheet_controls = AsyncMock(
            return_value=[{"controlId": "ctrl_key", "alias": "ctrl_key"}])
        adapter.get_worksheet_rows = AsyncMock(return_value=[
            {"rowid": "rA", "ctrl_key": "A"}])
        adapter.add_worksheet_row = AsyncMock(return_value="new-row")

        mapping = MagicMock(mapping_config={"key_field": "ctrl_key",
                                          "delete_sync": True})   # 无 hap_write_mode = append
        result = await hap_target.push(
            adapter, {"worksheet_id": "ws1"}, mapping,
            [{"ctrl_key": "A"}, {"ctrl_key": "B"}])
        # A 已存在 → 跳过；只新增 B
        assert result["body"]["skipped"] == 1
        assert result["body"]["row_ids"] == ["new-row"]

    @pytest.mark.asyncio
    async def test_auto_async_branch_rejected(self, monkeypatch):
        import app.services.router as router
        monkeypatch.setattr(router, "check_dependency_ready",
                            AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(router.sync_handler, "sync_transfer",
                            AsyncMock(return_value={"status": "success"}))
        monkeypatch.setattr(router.async_handler, "submit_async",
                            AsyncMock(return_value="buf-1"))
        monkeypatch.setattr(router, "_fetch_source",
                            AsyncMock(return_value=list(range(200))))  # >= 阈值

        task = MagicMock(sync_mode="auto", depends_on_task_id=None,
                         id="t1", auto_threshold=100, fallback_to_async=False,
                         webhook_config=None, conflict_strategy=None)
        mapping = MagicMock(mapping_config={"delete_sync": True, "key_field": "ctrl_key"},
                            sync_mode="full", id="m1")
        session = AsyncMock()

        from app.adapters.errors import AdapterError
        with pytest.raises(AdapterError) as ei:
            await router.route_transfer(session, task, mapping,
                                        MagicMock(protocol="hap", db_type=None),
                                        MagicMock(protocol="hap", db_type=None))
        assert ei.value.code == "hap_policy_mode"
        router.async_handler.submit_async.assert_not_called()