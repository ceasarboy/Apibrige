"""数据校验器测试 — 必填/类型/范围/正则/枚举 + 集成测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from app.services.validator import validate_record, validate_batch, ValidationError


class TestValidateRecord:
    """单条记录校验"""

    def test_required_pass(self):
        errors = validate_record({"name": "张三"}, {"name": {"required": True}})
        assert errors == []

    def test_required_fail_none(self):
        errors = validate_record({"name": None}, {"name": {"required": True}})
        assert len(errors) == 1
        assert errors[0].field == "name"
        assert errors[0].rule == "required"

    def test_required_fail_empty_string(self):
        errors = validate_record({"name": "  "}, {"name": {"required": True}})
        assert len(errors) == 1

    def test_required_not_required_none_skipped(self):
        errors = validate_record({"name": None}, {"name": {"required": False}})
        assert errors == []

    def test_type_int_pass(self):
        errors = validate_record({"age": 25}, {"age": {"type": "int"}})
        assert errors == []

    def test_type_int_fail(self):
        errors = validate_record({"age": "25"}, {"age": {"type": "int"}})
        assert len(errors) == 1
        assert errors[0].rule == "type"

    def test_type_float_accepts_int(self):
        errors = validate_record({"score": 85}, {"score": {"type": "float"}})
        assert errors == []

    def test_min_pass(self):
        errors = validate_record({"age": 18}, {"age": {"min": 0}})
        assert errors == []

    def test_min_fail(self):
        errors = validate_record({"age": -5}, {"age": {"min": 0}})
        assert len(errors) == 1
        assert errors[0].rule == "min"

    def test_max_pass(self):
        errors = validate_record({"age": 150}, {"age": {"max": 200}})
        assert errors == []

    def test_max_fail(self):
        errors = validate_record({"age": 250}, {"age": {"max": 200}})
        assert len(errors) == 1
        assert errors[0].rule == "max"

    def test_range_combined(self):
        errors = validate_record({"age": 30}, {"age": {"min": 0, "max": 200}})
        assert errors == []

    def test_min_length_pass(self):
        errors = validate_record({"name": "张三"}, {"name": {"min_length": 1}})
        assert errors == []

    def test_min_length_fail(self):
        errors = validate_record({"name": "A"}, {"name": {"min_length": 3}})
        assert len(errors) == 1

    def test_max_length_pass(self):
        errors = validate_record({"name": "张三"}, {"name": {"max_length": 10}})
        assert errors == []

    def test_max_length_fail(self):
        errors = validate_record({"name": "张三李四王五赵六"}, {"name": {"max_length": 3}})
        assert len(errors) == 1

    def test_pattern_pass(self):
        errors = validate_record({"email": "test@example.com"}, {"email": {"pattern": r"^[\w.-]+@[\w.-]+\.[a-zA-Z]{2,}$"}})
        assert errors == []

    def test_pattern_fail(self):
        errors = validate_record({"email": "not-an-email"}, {"email": {"pattern": r"^[\w.-]+@[\w.-]+\.[a-zA-Z]{2,}$"}})
        assert len(errors) == 1
        assert errors[0].rule == "pattern"

    def test_enum_pass(self):
        errors = validate_record({"status": "active"}, {"status": {"enum": ["active", "inactive"]}})
        assert errors == []

    def test_enum_fail(self):
        errors = validate_record({"status": "deleted"}, {"status": {"enum": ["active", "inactive"]}})
        assert len(errors) == 1
        assert errors[0].rule == "enum"

    def test_multiple_rules_on_one_field(self):
        errors = validate_record({"name": "A"}, {
            "name": {"required": True, "min_length": 3, "max_length": 10}
        })
        assert len(errors) == 1  # min_length fails

    def test_no_validations(self):
        errors = validate_record({"any": "thing"}, {})
        assert errors == []


class TestValidateBatch:
    """批量校验"""

    def test_all_pass(self):
        result = validate_batch(
            [{"name": "A", "age": 25}, {"name": "B", "age": 30}],
            {"name": {"required": True}, "age": {"type": "int"}},
        )
        assert result["valid"] is True
        assert result["total"] == 2
        assert result["passed"] == 2
        assert result["failed"] == 0

    def test_partial_fail(self):
        result = validate_batch(
            [{"name": "A", "age": 25}, {"name": "", "age": "abc"}],
            {"name": {"required": True}, "age": {"type": "int"}},
        )
        assert result["valid"] is False
        assert result["passed"] == 1
        assert result["failed"] == 1
        assert len(result["errors"]) > 0
        assert result["errors"][0]["index"] == 1


# ============================================================
# 集成测试：sync_handler 中校验失败
# ============================================================

class TestValidationInSyncHandler:
    """验证 sync_handler 中校验流程"""

    @pytest.mark.asyncio
    async def test_validation_failed_blocks_push(self, mock_redis, mock_api_source, mock_api_target):
        """校验失败时阻止推送，返回 validation_failed 状态"""
        from app.services.sync_handler import sync_transfer

        # mock_api_source 返回 {"user_name": "张三", "user_email": "zhangsan@example.com", "age": "25"}
        # age 转换后为 int 25, customerName="张三"(2字符)
        # 设置 max_length=1 使 "张三" 必失败
        mapping_config = {
            "fields": [
                {"source": "user_name", "target": "customerName", "transform": "string"},
                {"source": "age", "target": "age", "transform": "int"},
            ],
            "validations": {
                "customerName": {"required": True, "max_length": 1},
            },
        }

        task = MagicMock(id=uuid4(), fallback_to_async=False, timeout_ms=30000, conflict_strategy=None)
        mapping = MagicMock(
            source_endpoint="/users", target_endpoint="/api/customers",
            source_table=None, target_table=None,
            mapping_config=mapping_config,
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

        assert result["status"] == "validation_failed"
        assert "数据校验失败" in result["error"]
        mock_api_target.assert_not_called()  # 校验失败不应推送

    @pytest.mark.asyncio
    async def test_validation_pass_allows_push(self, mock_redis, mock_api_source, mock_api_target):
        """校验通过时正常推送"""
        from app.services.sync_handler import sync_transfer

        mapping_config = {
            "fields": [
                {"source": "user_name", "target": "customerName", "transform": "string"},
            ],
            "validations": {
                "customerName": {"required": True},
            },
        }

        task = MagicMock(id=uuid4(), fallback_to_async=False, timeout_ms=30000, conflict_strategy=None)
        mapping = MagicMock(
            source_endpoint="/users", target_endpoint="/api/customers",
            mapping_config=mapping_config,
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
        mock_api_target.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_validations_skips_check(self, mock_redis, mock_api_source, mock_api_target):
        """无校验规则时跳过校验"""
        from app.services.sync_handler import sync_transfer

        mapping_config = {
            "fields": [{"source": "user_name", "target": "customerName", "transform": "string"}],
            # 无 validations 键
        }

        task = MagicMock(id=uuid4(), fallback_to_async=False, timeout_ms=30000, conflict_strategy=None)
        mapping = MagicMock(
            source_endpoint="/users", target_endpoint="/api/customers",
            mapping_config=mapping_config,
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

    @pytest.mark.asyncio
    async def test_validation_with_correct_data(self, mock_redis, mock_api_source, mock_api_target):
        """校验规则匹配 mock 数据时通过（mock 返回 user_name='张三', age='25'）"""
        from app.services.sync_handler import sync_transfer

        mapping_config = {
            "fields": [
                {"source": "user_name", "target": "customerName", "transform": "string"},
                {"source": "user_email", "target": "email", "transform": "string"},
                {"source": "age", "target": "age", "transform": "int"},
            ],
            "validations": {
                "customerName": {"required": True, "min_length": 1, "max_length": 100},
                "email": {"required": True, "pattern": r"^[\w.-]+@[\w.-]+\.[a-zA-Z]{2,}$"},
                "age": {"required": True, "type": "int", "min": 0, "max": 200},
            },
        }

        task = MagicMock(id=uuid4(), fallback_to_async=False, timeout_ms=30000, conflict_strategy=None)
        mapping = MagicMock(
            source_endpoint="/users", target_endpoint="/api/customers",
            mapping_config=mapping_config,
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
        mock_api_target.assert_called_once()
