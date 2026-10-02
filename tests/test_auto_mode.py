"""7.3 auto 模式切换测试（混合数据量）"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from uuid import uuid4
from app.services.router import route_transfer, _estimate_count


def _patch_source_factory(data):
    """适配器路径替身：工厂返回 fetch_data 返回 data 的适配器"""
    adapter = MagicMock()
    adapter.fetch_data = AsyncMock(return_value=data)
    factory = MagicMock()
    factory.create = MagicMock(return_value=adapter)
    return patch("app.services.router.ProtocolAdapterFactory", factory)


class TestAutoMode:
    """7.3 auto 模式切换测试"""

    @pytest.mark.asyncio
    async def test_auto_small_data_uses_sync(self, mock_redis, mock_api_target, mapping_config):
        """验证: 边界值 99 条数据走同步"""
        # 源数据返回 99 条
        small_data = [{"user_name": f"user{i}"} for i in range(99)]

        task = MagicMock(id=uuid4(), sync_mode="auto", auto_threshold=100, timeout_ms=30000,
                         conflict_strategy=None, depends_on_task_id=None)
        mapping = MagicMock(source_endpoint="/users", target_endpoint="/api/customers",
                            mapping_config=mapping_config)
        source_conn = MagicMock(db_type=None, auth_type=None, auth_config=None, base_url="https://source.example.com", headers={}, timeout_ms=30000)
        target_conn = MagicMock(id=uuid4(), db_type=None, auth_type=None, auth_config=None, base_url="https://target.example.com", headers={}, timeout_ms=30000)
        session = AsyncMock()

        with _patch_source_factory(small_data):
            with patch("app.services.sync_handler.sync_transfer", new_callable=AsyncMock) as mock_sync:
                mock_sync.return_value = {"status": "success"}
                with patch("app.services.async_handler.submit_async", new_callable=AsyncMock) as mock_async:
                    result = await route_transfer(session, task, mapping, source_conn, target_conn)

        # 验证走了同步路径
        mock_sync.assert_called_once()
        mock_async.assert_not_called()
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_auto_threshold_data_uses_sync(self, mock_redis, mock_api_target, mapping_config):
        """验证: 边界值 100 条数据走同步（<100 才走同步，100 不 < 100）"""
        data_100 = [{"user_name": f"user{i}"} for i in range(100)]

        task = MagicMock(id=uuid4(), sync_mode="auto", auto_threshold=100, timeout_ms=30000,
                         conflict_strategy=None, depends_on_task_id=None)
        mapping = MagicMock(source_endpoint="/users", target_endpoint="/api/customers",
                            mapping_config=mapping_config)
        source_conn = MagicMock(db_type=None, auth_type=None, auth_config=None, base_url="https://source.example.com", headers={}, timeout_ms=30000)
        target_conn = MagicMock(id=uuid4(), db_type=None, auth_type=None, auth_config=None, base_url="https://target.example.com", headers={}, timeout_ms=30000)
        session = AsyncMock()

        with _patch_source_factory(data_100):
            with patch("app.services.sync_handler.sync_transfer", new_callable=AsyncMock) as mock_sync:
                with patch("app.services.async_handler.submit_async", new_callable=AsyncMock) as mock_async:
                    mock_async.return_value = "buffer-123"
                    result = await route_transfer(session, task, mapping, source_conn, target_conn)

        # 100 >= 100，走异步
        mock_sync.assert_not_called()
        mock_async.assert_called_once()
        assert result["status"] == "async_submitted"

    @pytest.mark.asyncio
    async def test_auto_large_data_uses_async(self, mock_redis, mapping_config):
        """验证: 边界值 101 条数据走异步"""
        data_101 = [{"user_name": f"user{i}"} for i in range(101)]

        task = MagicMock(id=uuid4(), sync_mode="auto", auto_threshold=100, timeout_ms=30000,
                         conflict_strategy=None, depends_on_task_id=None)
        mapping = MagicMock(source_endpoint="/users", target_endpoint="/api/customers",
                            mapping_config=mapping_config)
        source_conn = MagicMock(db_type=None, auth_type=None, auth_config=None, base_url="https://source.example.com", headers={}, timeout_ms=30000)
        target_conn = MagicMock(id=uuid4(), db_type=None, auth_type=None, auth_config=None, base_url="https://target.example.com", headers={}, timeout_ms=30000)
        session = AsyncMock()

        with _patch_source_factory(data_101):
            with patch("app.services.sync_handler.sync_transfer", new_callable=AsyncMock) as mock_sync:
                with patch("app.services.async_handler.submit_async", new_callable=AsyncMock) as mock_async:
                    mock_async.return_value = "buffer-456"
                    result = await route_transfer(session, task, mapping, source_conn, target_conn)

        mock_sync.assert_not_called()
        mock_async.assert_called_once()
        assert result["status"] == "async_submitted"

    def test_estimate_count_list(self):
        """验证: 数据量预估 - 列表"""
        assert _estimate_count([1, 2, 3]) == 3

    def test_estimate_count_dict_with_items(self):
        """验证: 数据量预估 - dict 含 items 字段"""
        assert _estimate_count({"items": [1, 2]}) == 2

    def test_estimate_count_dict_single(self):
        """验证: 数据量预估 - 单条 dict"""
        assert _estimate_count({"name": "test"}) == 1
