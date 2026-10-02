# -*- coding: utf-8 -*-
"""探针时间戳口径 + 失败详情契约测试（发行包线特性，原本无测试）

- `app/probe/timestamps.py`：无时区值一律按 **UTC+08:00** 解释；`ProbeTimestamp`
  写库时存"东八区墙钟"，回读时恢复 +08:00 偏移（naive 与 aware 输入都不改变瞬时）。
  这一口径与"无时区=UTC"的旧假设混用会产生 8 小时偏移，故用测试钉住。
- `app/services/failure_logs.py`：对外错误详情只保留可排查信息（异常类型/阶段/字段/行号），
  地址与 `enc:` 密文一律脱敏，issue 数量有上限；源数据定位只读本地缓冲。
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.adapters.errors import AdapterError
from app.models.sync import Base, DataBuffer, ExecutionLog, FieldMapping, SyncTask
from app.probe.timestamps import EAST_EIGHT, ProbeTimestamp, as_east_eight
from app.services.failure_logs import (
    MAX_ISSUES, error_detail, normalize_detail, safe_message, safe_source, source_records,
    validation_detail,
)

UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8))


# ============================================================
# 探针时间戳
# ============================================================

class TestProbeTimestamps:

    def test_naive_is_east_eight(self):
        naive = datetime(2026, 9, 30, 12, 0, 0)
        got = as_east_eight(naive)
        assert got.utcoffset() == timedelta(hours=8)
        # 12:00 东八区 = 04:00 UTC
        assert got.astimezone(UTC).hour == 4

    def test_aware_is_converted_not_relabeled(self):
        aware = datetime(2026, 9, 30, 4, 0, 0, tzinfo=UTC)
        got = as_east_eight(aware)
        assert got.hour == 12 and got.utcoffset() == timedelta(hours=8)
        assert got == aware                       # 瞬时不变

    def test_iso_string_and_z_suffix(self):
        assert as_east_eight("2026-09-30T12:00:00").utcoffset() == timedelta(hours=8)
        got = as_east_eight("2026-09-30T04:00:00Z")
        assert got.hour == 12

    def test_rejects_non_datetime(self):
        with pytest.raises(TypeError):
            as_east_eight(12345)                  # type: ignore[arg-type]

    def test_type_decorator_round_trip_keeps_instant(self):
        """写库存东八区墙钟（naive），回读恢复 +08:00：瞬时不变"""
        deco = ProbeTimestamp()
        aware = datetime(2026, 9, 30, 4, 0, 0, tzinfo=UTC)      # 北京 12:00
        stored = deco.process_bind_param(aware, None)
        assert stored == datetime(2026, 9, 30, 12, 0, 0)        # 东八区墙钟、无偏移
        assert stored.tzinfo is None
        back = deco.process_result_value(stored, None)
        assert back == aware                                    # 瞬时一致
        assert back.utcoffset() == timedelta(hours=8)

    def test_type_decorator_handles_none(self):
        deco = ProbeTimestamp()
        assert deco.process_bind_param(None, None) is None
        assert deco.process_result_value(None, None) is None


# ============================================================
# 失败详情契约
# ============================================================

class TestSafeMessage:

    def test_adapter_error_keeps_reason(self):
        err = AdapterError('source_field_missing', '源字段不存在: qty', 'field_resolve')
        assert '源字段不存在' in safe_message(err)

    def test_driver_error_hides_details(self):
        class Boom(Exception):
            pass
        message = safe_message(Boom("password=hunter2 host=db.internal:1521"))
        assert 'hunter2' not in message and 'db.internal' not in message
        assert message.startswith('Boom:')

    def test_urls_and_ciphertext_redacted(self):
        err = AdapterError('upstream_failed',
                           'POST https://api.example.com/v2?sign=abc 失败 enc:Zm9vYmFy',
                           'execute')
        message = safe_message(err)
        assert 'api.example.com' not in message
        assert 'enc:Zm9vYmFy' not in message
        assert '[地址已隐藏]' in message


class TestNormalizeDetail:

    def test_shape_and_defaults(self):
        out = normalize_detail(None)
        assert out['schema_version'] == '1.0'
        assert out['code'] == 'execution_failed'
        assert out['stage'] == 'execute'

    def test_keeps_locators_drops_unknown_keys(self):
        raw = {'code': 'transform_failed', 'stage': 'transform', 'target_field': 'qty',
               'target_field_alias': 'QTY', 'row_index': 3, 'status_code': 502,
               'message': 'https://x.example.com 拒绝', 'secret_extra': 'should-drop',
               'exception_type': 'AdapterError'}
        out = normalize_detail(raw, 'transform')
        assert out['code'] == 'transform_failed'
        assert out['target_field'] == 'qty' and out['row_index'] == 3
        assert out['status_code'] == 502
        assert 'secret_extra' not in out
        assert 'x.example.com' not in out['message']

    def test_issues_truncated_with_flag(self):
        issues = [{'row_index': i, 'rule': 'required', 'target_field': 'qty'}
                  for i in range(MAX_ISSUES + 5)]
        out = normalize_detail({'issues': issues})
        assert len(out['issues']) == MAX_ISSUES
        assert out['issue_count'] == MAX_ISSUES + 5
        assert out['issues_truncated'] is True

    def test_frames_trimmed_to_last_eight(self):
        frames = [{'file': f'f{i}.py', 'line': i, 'function': 'fn'} for i in range(20)]
        out = normalize_detail({'frames': frames})
        assert len(out['frames']) == 8
        assert out['frames'][-1]['line'] == 19

    def test_error_detail_includes_trace_frames(self):
        def inner():
            raise RuntimeError('boom')
        try:
            inner()
        except RuntimeError as exc:
            out = error_detail(exc, 'execute')
        assert out['exception_type'] == 'RuntimeError'
        assert out['frames'] and out['frames'][-1]['function'] == 'inner'

    def test_validation_detail_has_no_raw_values(self):
        result = {'errors': [{'index': 2, 'field': 'qty', 'rule': 'required',
                              'message': '值为 secret-value'}]}
        out = validation_detail(result)
        assert out['code'] == 'validation_failed'
        assert out['issues'][0] == {'row_index': 2, 'target_field': 'qty', 'rule': 'required'}
        assert 'secret-value' not in str(out)


class TestSourceRecords:

    def test_list_passthrough(self):
        rows, path = source_records([{"a": 1}])
        assert rows == [{"a": 1}] and path == ''

    def test_wrapper_keys_detected(self):
        for key in ('data', 'items', 'results', 'list', 'records', 'rows'):
            rows, path = source_records({key: [{"a": 1}], 'other': 2})
            assert rows == [{"a": 1}]
            assert path == '/' + key

    def test_single_dict_is_one_record(self):
        rows, path = source_records({"a": 1, "b": 2})
        assert rows == [{"a": 1, "b": 2}] and path == ''

    def test_safe_source_desensitizes_and_scrubs_ciphertext(self):
        data = {"password": "hunter2", "token": "enc:Zm9v", "row": [{"pwd": "x"}]}
        out = safe_source(data)
        assert out["password"] != "hunter2"
        assert out["token"] == "***"
        assert out["row"][0]["pwd"] != "x"


# ============================================================
# 失败上下文：源行定位（只读本地缓冲）
# ============================================================

@pytest.fixture
def fl_env(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/fl.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    asyncio.run(engine.dispose())


class TestFailureContext:

    def test_without_batch_gives_notice_not_error(self, fl_env):
        from app.services.failure_logs import failure_context

        async def _run():
            async with fl_env() as s:
                from app.models.sync import ApiConnection
                src = ApiConnection(name="源", connection_type="source", protocol="rest",
                                    base_url="https://s", auth_type=None, auth_config={},
                                    status="active")
                tgt = ApiConnection(name="目标", connection_type="target", protocol="rest",
                                    base_url="https://t", auth_type=None, auth_config={},
                                    status="active")
                s.add_all([src, tgt])
                await s.commit()
                await s.refresh(src)
                await s.refresh(tgt)
                mapping = FieldMapping(name="桥", source_connection_id=src.id,
                                       target_connection_id=tgt.id,
                                       mapping_config={"fields": []})
                s.add(mapping)
                await s.commit()
                await s.refresh(mapping)
                task = SyncTask(mapping_id=mapping.id, task_type="scheduled", status="active")
                s.add(task)
                await s.commit()
                await s.refresh(task)
                log = ExecutionLog(task_id=task.id, action='push', status='failed',
                                   error_message='boom', buffer_id=None,
                                   response_data={'error_detail': {'code': 'x'}})
                s.add(log)
                await s.commit()
                await s.refresh(log)
                ctx = await failure_context(s, log)
                return ctx
        ctx = asyncio.run(_run())
        assert ctx['records'] == []
        assert '无法定位源行' in ctx['notice']
        assert ctx['task_name'] == '桥'
