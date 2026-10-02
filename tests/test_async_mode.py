"""7.2 异步模式端到端测试 + 7.5 故障恢复测试"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from uuid import uuid4
from app.services.async_handler import submit_async, process_async


class TestAsyncModeE2E:
    """7.2 异步模式端到端测试（DB→API 批量同步）"""

    @pytest.mark.asyncio
    async def test_async_submit_returns_buffer_id(self, mock_redis):
        """验证: 提交后立即返回任务ID，data_buffer 有 pending 记录"""
        task_id = uuid4()
        source_data = {"user_name": "张三", "age": "25"}

        # mock 数据库会话
        session = AsyncMock()
        buffer_obj = MagicMock()
        buffer_obj.id = uuid4()
        with patch("app.services.async_handler.DataBuffer") as MockBuffer:
            MockBuffer.return_value = buffer_obj
            with patch("app.celery_app.process_buffer") as mock_task:
                result = await submit_async(session, task_id, source_data)

        # 验证返回 buffer_id
        assert result == str(buffer_obj.id)
        # 验证 data_buffer 写入
        session.add.assert_called_once()
        session.commit.assert_called()
        # 验证 Celery 任务投递
        mock_task.delay.assert_called_once_with(str(buffer_obj.id))

    @pytest.mark.asyncio
    async def test_async_process_completes(self, mock_redis, mock_api_target, mapping_config):
        """验证: Worker 消费后 data_buffer 状态变为 completed"""
        buffer_id = str(uuid4())
        task_id = uuid4()

        # mock 数据库查询链
        buffer_obj = MagicMock(
            id=buffer_id, task_id=task_id, source_data={"user_name": "张三"},
            status="pending", transformed_data=None
        )
        task_obj = MagicMock(id=task_id, mapping_id=uuid4(), timeout_ms=30000)
        mapping_obj = MagicMock(mapping_config=mapping_config, target_endpoint="/api/customers")
        target_conn = MagicMock(id=uuid4(), db_type=None, auth_type=None, auth_config=None, base_url="https://target.example.com", headers={}, timeout_ms=30000)

        session = AsyncMock()
        # 按调用顺序返回: DataBuffer → SyncTask → FieldMapping → ApiConnection
        results = [
            (buffer_obj, "scalar_one_or_none"),
            (task_obj, "scalar_one"),
            (mapping_obj, "scalar_one"),
            (target_conn, "scalar_one"),
        ]
        call_idx = [0]

        def mock_execute(query):
            idx = call_idx[0]
            call_idx[0] += 1
            result = MagicMock()
            if idx < len(results):
                obj, method = results[idx]
                setattr(result, method, MagicMock(return_value=obj))
            else:
                # 后续查询（冲突检测哈希表）：返回空哈希集
                result.scalars = MagicMock(
                    return_value=MagicMock(all=MagicMock(return_value=[])))
            return result

        session.execute = AsyncMock(side_effect=mock_execute)

        with patch("app.database.async_session") as mock_session_ctx:
            mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
            await process_async(buffer_id)

        # 验证状态更新为 completed
        assert buffer_obj.status == "completed"
        assert buffer_obj.transformed_data is not None
        mock_api_target.assert_called_once()


class TestFaultRecovery:
    """7.5 故障恢复测试（Worker 异常重启）"""

    @pytest.mark.asyncio
    async def test_pending_buffer_reprocessed(self, mock_redis, mock_api_target, mapping_config):
        """验证: pending 数据被重新消费，不丢失"""
        buffer_id = str(uuid4())
        task_id = uuid4()

        # mock 一个 pending 状态的 buffer（Worker 重启前未处理完）
        buffer_obj = MagicMock(
            id=buffer_id, task_id=task_id, source_data={"user_name": "李四"},
            status="pending", transformed_data=None
        )
        task_obj = MagicMock(id=task_id, mapping_id=uuid4(), timeout_ms=30000)
        mapping_obj = MagicMock(mapping_config=mapping_config, target_endpoint="/api/customers")
        target_conn = MagicMock(id=uuid4(), db_type=None, auth_type=None, auth_config=None, base_url="https://target.example.com", headers={}, timeout_ms=30000)

        session = AsyncMock()
        # 按调用顺序返回: DataBuffer → SyncTask → FieldMapping → ApiConnection
        results = [
            (buffer_obj, "scalar_one_or_none"),
            (task_obj, "scalar_one"),
            (mapping_obj, "scalar_one"),
            (target_conn, "scalar_one"),
        ]
        call_idx = [0]

        def mock_execute(query):
            idx = call_idx[0]
            call_idx[0] += 1
            result = MagicMock()
            if idx < len(results):
                obj, method = results[idx]
                setattr(result, method, MagicMock(return_value=obj))
            else:
                # 后续查询（冲突检测哈希表）：返回空哈希集
                result.scalars = MagicMock(
                    return_value=MagicMock(all=MagicMock(return_value=[])))
            return result

        session.execute = AsyncMock(side_effect=mock_execute)

        with patch("app.database.async_session") as mock_session_ctx:
            mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
            # 重新处理 pending 数据
            await process_async(buffer_id)

        # 验证 pending 数据被成功处理
        assert buffer_obj.status == "completed"

    @pytest.mark.asyncio
    async def test_completed_buffer_not_reprocessed(self, mock_redis):
        """验证: 已完成的 buffer 不会被重复处理"""
        buffer_id = str(uuid4())
        buffer_obj = MagicMock(status="completed")

        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=buffer_obj)
        session.execute = AsyncMock(return_value=result)

        with patch("app.database.async_session") as mock_session_ctx:
            mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
            await process_async(buffer_id)

        # 已完成，不重复处理（状态不变）
        assert buffer_obj.status == "completed"
