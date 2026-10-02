"""7.1 同步模式端到端测试 + 7.4 幂等性测试"""
import time
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from uuid import uuid4
from app.services.transformer import transform_data
from app.cache.idempotency import compute_hash, is_duplicate


class TestSyncModeE2E:
    """7.1 同步模式端到端测试（REST API→REST API 单条同步）"""

    @pytest.mark.asyncio
    async def test_sync_transfer_success(self, mock_redis, mock_api_source, mock_api_target, mapping_config):
        """验证: 数据完整同步，日志可追溯"""
        from app.services.sync_handler import sync_transfer

        # 构造 mock 对象
        task = MagicMock(id=uuid4(), fallback_to_async=False, timeout_ms=30000, conflict_strategy=None)
        mapping = MagicMock(source_endpoint="/users", target_endpoint="/api/customers",
                            mapping_config=mapping_config)
        source_conn = MagicMock(db_type=None, auth_type=None, auth_config=None, base_url="https://source.example.com", headers={}, timeout_ms=30000)
        target_conn = MagicMock(id=uuid4(), db_type=None, auth_type=None, auth_config=None, base_url="https://target.example.com", headers={}, timeout_ms=30000)

        # mock 数据库会话
        session = AsyncMock()
        buffer_obj = MagicMock()
        session.add = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))
        # 模拟 commit 后 buffer 有 id
        buffer_obj.id = uuid4()
        with patch("app.services.sync_handler.DataBuffer") as MockBuffer:
            MockBuffer.return_value = buffer_obj
            result = await sync_transfer(session, task, mapping, source_conn, target_conn)

        # 验证结果
        assert result["status"] == "success"
        assert "buffer_id" in result
        # 验证源 API 被调用
        mock_api_source.assert_called_once()
        # 验证目标 API 被调用
        mock_api_target.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_transfer_data_transform(self, mapping_config):
        """验证: 数据转换正确"""
        source_data = {"user_name": "张三", "user_email": "zhangsan@example.com", "age": "25"}
        result = transform_data(source_data, mapping_config)
        assert result["customerName"] == "张三"
        assert result["email"] == "zhangsan@example.com"
        assert result["age"] == 25  # 转换为 int

    @pytest.mark.asyncio
    async def test_sync_transfer_p99_latency(self, mock_redis, mock_api_source, mock_api_target, mapping_config):
        """验证: 单条数据同步处理 P99 <1秒"""
        from app.services.sync_handler import sync_transfer

        task = MagicMock(id=uuid4(), fallback_to_async=False, timeout_ms=30000, conflict_strategy=None)
        mapping = MagicMock(source_endpoint="/users", target_endpoint="/api/customers",
                            mapping_config=mapping_config)
        source_conn = MagicMock(db_type=None, auth_type=None, auth_config=None, base_url="https://source.example.com", headers={}, timeout_ms=30000)
        target_conn = MagicMock(id=uuid4(), db_type=None, auth_type=None, auth_config=None, base_url="https://target.example.com", headers={}, timeout_ms=30000)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        start = time.time()
        with patch("app.services.sync_handler.DataBuffer") as MockBuffer:
            MockBuffer.return_value.id = uuid4()
            await sync_transfer(session, task, mapping, source_conn, target_conn)
        elapsed = time.time() - start

        assert elapsed < 1.0, f"同步处理耗时 {elapsed:.3f}s 超过 1 秒"


class TestIdempotency:
    """7.4 幂等性测试（重复提交相同数据）"""

    @pytest.mark.asyncio
    async def test_duplicate_data_skipped(self, mock_redis):
        """验证: 仅处理一次，不产生重复同步"""
        task_id = str(uuid4())
        data = {"user_name": "张三", "user_email": "zhangsan@example.com"}

        # 幂等检查是只读 GET：首次未标记，不重复
        mock_redis.get = AsyncMock(return_value=None)
        is_dup = await is_duplicate(task_id, data)
        assert is_dup is False  # 首次，不重复

        # 推送成功后标记（mark_processed 写入占位键）
        from app.cache.idempotency import mark_processed
        store = {}
        mock_redis.get = AsyncMock(side_effect=lambda k: store.get(k))
        mock_redis.set = AsyncMock(side_effect=lambda k, v, nx=False, ex=None:
                                   store.setdefault(k, v))
        await mark_processed(task_id, data)

        # 第二次提交：已标记 → 重复
        is_dup = await is_duplicate(task_id, data)
        assert is_dup is True  # 重复

    @pytest.mark.asyncio
    async def test_hash_consistency(self):
        """验证: 相同数据生成相同哈希"""
        data1 = {"a": 1, "b": 2}
        data2 = {"b": 2, "a": 1}  # 顺序不同
        assert compute_hash(data1) == compute_hash(data2)

        data3 = {"a": 1, "b": 3}
        assert compute_hash(data1) != compute_hash(data3)
