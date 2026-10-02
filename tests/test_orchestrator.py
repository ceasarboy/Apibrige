"""任务编排 + 冲突检测测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from app.services.orchestrator import check_dependency_ready
from app.services.conflict import detect_conflicts, detect_hash_conflict, compute_record_hash


# ============================================================
# 任务编排测试
# ============================================================

class TestOrchestrator:
    """check_dependency_ready 测试"""

    @pytest.mark.asyncio
    async def test_no_dependency(self):
        task = MagicMock(depends_on_task_id=None)
        session = AsyncMock()
        ready, reason = await check_dependency_ready(session, task)
        assert ready is True
        assert reason is None

    @pytest.mark.asyncio
    async def test_dependency_completed(self):
        dep_id = uuid4()
        task = MagicMock(depends_on_task_id=dep_id)
        session = AsyncMock()

        # mock session.get 返回前置任务
        mock_dep_task = MagicMock(id=dep_id)
        session.get = AsyncMock(return_value=mock_dep_task)

        # mock session.execute for buffer query 返回有 completed 的 buffer
        mock_buffer = MagicMock(status="completed")
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_buffer]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        session.execute = AsyncMock(return_value=mock_result)

        ready, reason = await check_dependency_ready(session, task)
        assert ready is True

    @pytest.mark.asyncio
    async def test_dependency_not_ready(self):
        dep_id = uuid4()
        task = MagicMock(depends_on_task_id=dep_id)
        session = AsyncMock()

        mock_dep_task = MagicMock(id=dep_id)
        session.get = AsyncMock(return_value=mock_dep_task)

        # 无 buffer（空列表）
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        session.execute = AsyncMock(return_value=mock_result)

        ready, reason = await check_dependency_ready(session, task)
        assert ready is False
        assert "尚未成功执行" in reason

    @pytest.mark.asyncio
    async def test_dependency_deleted(self):
        dep_id = uuid4()
        task = MagicMock(depends_on_task_id=dep_id)
        session = AsyncMock()
        session.get.return_value = None  # 前置任务不存在

        ready, reason = await check_dependency_ready(session, task)
        assert ready is True
        assert "已删除" in reason


# ============================================================
# 冲突检测测试
# ============================================================

class TestConflictDetection:
    """冲突检测测试"""

    def test_no_existing_data(self):
        to_push, conflicts, skipped = detect_conflicts(
            [{"id": 1, "name": "A"}], [], key_field="id"
        )
        assert len(to_push) == 1
        assert len(conflicts) == 0

    def test_skip_conflict(self):
        existing = [{"id": 1, "name": "Old"}]
        to_push, conflicts, skipped = detect_conflicts(
            [{"id": 1, "name": "New"}], existing, key_field="id", strategy="skip"
        )
        assert len(to_push) == 0
        assert len(conflicts) == 1
        assert len(skipped) == 1

    def test_overwrite_conflict(self):
        existing = [{"id": 1, "name": "Old"}]
        to_push, conflicts, skipped = detect_conflicts(
            [{"id": 1, "name": "New"}], existing, key_field="id", strategy="overwrite"
        )
        assert len(to_push) == 1
        assert len(conflicts) == 1
        assert len(skipped) == 0

    def test_mixed_no_conflict_items(self):
        existing = [{"id": 1}]
        data = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}, {"id": 3, "name": "C"}]
        to_push, conflicts, skipped = detect_conflicts(
            data, existing, key_field="id", strategy="skip"
        )
        assert len(to_push) == 2  # id=2,3
        assert len(conflicts) == 1  # id=1
        assert len(skipped) == 1

    def test_hash_consistency(self):
        h1 = compute_record_hash({"id": 1, "name": "A"})
        h2 = compute_record_hash({"id": 1, "name": "A"})
        h3 = compute_record_hash({"id": 1, "name": "B"})
        assert h1 == h2
        assert h1 != h3

    def test_hash_ignores_key_field(self):
        h1 = compute_record_hash({"id": 1, "name": "A"}, key_field="id")
        h2 = compute_record_hash({"id": 2, "name": "A"}, key_field="id")
        assert h1 == h2  # 忽略 id 后内容相同

    def test_detect_hash_conflict_skip(self):
        hashes = {compute_record_hash({"id": 1, "name": "A"}, key_field="id")}
        to_push, count = detect_hash_conflict(
            [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
            hashes, strategy="skip", key_field="id",
        )
        assert len(to_push) == 1  # id=2 通过
        assert count == 1  # id=1 冲突

    def test_detect_hash_conflict_overwrite(self):
        hashes = {compute_record_hash({"id": 1, "name": "A"}, key_field="id")}
        to_push, count = detect_hash_conflict(
            [{"id": 1, "name": "A"}], hashes, strategy="overwrite", key_field="id",
        )
        assert len(to_push) == 1  # overwrite 全通过
        assert count == 0


# ============================================================
# 集成测试：router 中依赖阻塞
# ============================================================

class TestDependencyInRouter:
    """验证 router 中任务依赖阻塞"""

    @pytest.mark.asyncio
    async def test_task_blocked_by_dependency(self, mock_redis):
        """有依赖且前置未完成时返回 blocked"""
        from app.services.router import route_transfer

        dep_id = uuid4()
        task = MagicMock(
            id=uuid4(), sync_mode="sync",
            depends_on_task_id=dep_id, conflict_strategy=None,
        )
        mapping = MagicMock()
        source_conn = MagicMock(db_type=None)
        target_conn = MagicMock(id=uuid4(), db_type=None)
        session = AsyncMock()

        # mock dependency not ready
        with patch("app.services.router.check_dependency_ready", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = (False, "前置任务尚未成功执行")
            result = await route_transfer(session, task, mapping, source_conn, target_conn)

        assert result["status"] == "blocked"
        assert result["depends_on"] == str(dep_id)

    @pytest.mark.asyncio
    async def test_task_ready_no_dependency(self, mock_redis, mock_api_source, mock_api_target):
        """无依赖时正常执行"""
        from app.services.router import route_transfer

        task = MagicMock(
            id=uuid4(), sync_mode="sync",
            depends_on_task_id=None, conflict_strategy=None,
        )
        mapping = MagicMock(
            source_endpoint="/users", target_endpoint="/api/customers",
            mapping_config={"fields": []},
        )
        source_conn = MagicMock(db_type=None, base_url="https://source.example.com", headers={}, timeout_ms=30000)
        target_conn = MagicMock(id=uuid4(), db_type=None, base_url="https://target.example.com", headers={}, timeout_ms=30000)
        session = AsyncMock()
        buffer_obj = MagicMock(id=uuid4())
        session.add = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        with patch("app.services.sync_handler.DataBuffer") as MockBuffer:
            MockBuffer.return_value = buffer_obj
            result = await route_transfer(session, task, mapping, source_conn, target_conn)

        assert result["status"] == "success"


# ============================================================
# 集成测试：冲突检测在 sync_handler 中
# ============================================================

class TestConflictInSyncHandler:
    """验证 sync_handler 中冲突检测"""

    @pytest.mark.asyncio
    async def test_conflict_skip_works(self, mock_redis, mock_api_source, mock_api_target):
        """conflict_strategy=skip 时正常推送（mock 哈希不冲突）"""
        from app.services.sync_handler import sync_transfer

        task = MagicMock(
            id=uuid4(), fallback_to_async=False, timeout_ms=30000,
            conflict_strategy="skip",
        )
        mapping = MagicMock(
            source_endpoint="/users", target_endpoint="/api/customers",
            mapping_config={
                "fields": [{"source": "user_name", "target": "name", "transform": "string"}],
            },
        )
        source_conn = MagicMock(db_type=None, base_url="https://source.example.com", headers={}, timeout_ms=30000)
        target_conn = MagicMock(id=uuid4(), db_type=None, base_url="https://target.example.com", headers={}, timeout_ms=30000)
        session = AsyncMock()
        session.add = MagicMock()

        # mock hash query: return empty set (no conflicts)
        mock_hash_scalars = MagicMock()
        mock_hash_scalars.all.return_value = []
        mock_hash_execute = MagicMock()
        mock_hash_execute.scalars.return_value = mock_hash_scalars
        session.execute = AsyncMock(return_value=mock_hash_execute)

        result = await sync_transfer(session, task, mapping, source_conn, target_conn)

        assert result["status"] == "success", f"Got: {result}"

    @pytest.mark.asyncio
    async def test_no_conflict_strategy_skips_detection(self, mock_redis, mock_api_source, mock_api_target):
        """conflict_strategy=None 时跳过冲突检测"""
        from app.services.sync_handler import sync_transfer

        task = MagicMock(id=uuid4(), fallback_to_async=False, timeout_ms=30000, conflict_strategy=None)
        mapping = MagicMock(
            source_endpoint="/users", target_endpoint="/api/customers",
            mapping_config={"fields": []},
        )
        source_conn = MagicMock(db_type=None, base_url="https://source.example.com", headers={}, timeout_ms=30000)
        target_conn = MagicMock(id=uuid4(), db_type=None, base_url="https://target.example.com", headers={}, timeout_ms=30000)
        session = AsyncMock()
        buffer_obj = MagicMock(id=uuid4())
        session.add = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        with patch("app.services.sync_handler.DataBuffer") as MockBuffer:
            MockBuffer.return_value = buffer_obj
            result = await sync_transfer(session, task, mapping, source_conn, target_conn)

        assert result["status"] == "success"
