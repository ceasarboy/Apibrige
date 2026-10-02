# -*- coding: utf-8 -*-
"""HAP V2 批量新增更新（upsert）测试：值规范化 / 计划构建 / 执行与恢复语义

覆盖（原本无测试）：
- `scalar` 仅规范化受支持标量控件，复杂控件显式拒绝（不猜测写入表示）；
- `catalog`/`prepare_records`：字段名解析（controlId 与 alias 双认）、只读/不可写/未知字段拒绝、
  源业务键空或重复拒绝、必填清空拒绝、`null_policy=omit` 跳过非键空值；
- `build_operations`：add/edit/unchanged 判定、按更新内容分组、delete 差额；
- `execute`：首次执行建计划并写目标 → completed；**重放已完成计划不再写**（幂等）；
  目标身份变化拒绝重放；租约/计划互斥语义。
"""
import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.adapters.errors import AdapterError
from app.models.sync import Base, DataBuffer, FieldMapping, HapWritePlan, SyncTask
from app.services import hap_bulk
from app.services.incremental import unseal
from app.services.hap_bulk import (
    build_operations, catalog, prepare_records, scalar, summary,
)

UTC = timezone.utc
WS = "ws_target"

# 控件目录：业务键（文本）、数量（数值）、日期（日期）、单选、只读字段、分段（布局）
_CONTROLS = [
    {"controlId": "ctrl_key", "alias": "code", "controlName": "编码", "type": 2, "required": True},
    {"controlId": "ctrl_qty", "alias": "qty", "controlName": "数量", "type": 6},
    {"controlId": "ctrl_day", "alias": "day", "controlName": "日期", "type": 15},
    {"controlId": "ctrl_pick", "alias": "pick", "controlName": "单选", "type": 9,
     "options": [{"key": "k1", "value": "甲"}, {"key": "k2", "value": "乙"}]},
    {"controlId": "ctrl_ro", "alias": "ro", "controlName": "只读", "type": 2, "readOnly": True},
    {"controlId": "ctrl_div", "alias": "div", "controlName": "分段", "type": 22},
]

_OPTIONS = {"create_batch_size": 100, "update_batch_size": 100,
            "trigger_workflow": False, "null_policy": "omit"}


def _mapping_config(**extra):
    # v1 映射配置形状：protocol 下直接放 target 侧键（side_config 对 version=1 返回整个 protocol）
    cfg = {"fields": [{"source": "code", "target": "code"},
                      {"source": "qty", "target": "qty"}],
           "key_field": "code",
           "hap_write_mode": "upsert",
           "protocol": {"worksheet_id": WS, "hap_bulk": {}}}
    cfg.update(extra)
    return cfg


# ============================================================
# 纯函数：值规范化与目录解析
# ============================================================

class TestScalar:

    def test_number_normalized(self):
        assert scalar("1.50", {"type": 6}) == "1.5"
        assert scalar(3, {"type": 6}) == "3"

    def test_number_invalid_rejected(self):
        with pytest.raises(AdapterError) as ei:
            scalar("abc", {"type": 6})
        assert ei.value.code == "hap_value_invalid"

    def test_date_and_datetime(self):
        assert scalar("2026-09-30", {"type": 15}) == "2026-09-30"
        assert scalar("2026-09-30T12:30:00", {"type": 16}) == "2026-09-30 12:30:00"

    def test_text_and_empty(self):
        assert scalar(" 甲 ", {"type": 2}) == " 甲 "
        assert scalar(None, {"type": 2}) == ""
        assert scalar("", {"type": 6}) == ""

    def test_complex_control_rejected(self):
        with pytest.raises(AdapterError) as ei:
            scalar([1, 2], {"type": 29})          # 关联记录不支持
        assert ei.value.code == "hap_control_unsupported"

    def test_option_normalized_to_key(self):
        out = scalar("甲", {"type": 9, "options": _CONTROLS[3]["options"]})
        assert out == "k1"


class TestCatalogAndPrepare:

    def test_catalog_accepts_id_and_alias(self):
        names = catalog(_CONTROLS)
        assert names["code"]["controlId"] == "ctrl_key"
        assert names["ctrl_key"]["controlId"] == "ctrl_key"

    def test_catalog_rejects_ambiguous_alias(self):
        controls = _CONTROLS + [{"controlId": "ctrl_other", "alias": "code",
                                 "controlName": "另一编码", "type": 2}]
        with pytest.raises(AdapterError) as ei:
            catalog(controls)
        assert ei.value.code == "hap_field_ambiguous"

    def test_prepare_records_resolves_and_types(self):
        key_id, types, desired = prepare_records(
            [{"code": "A", "qty": 2}], _CONTROLS, "code", _OPTIONS)
        assert key_id == "ctrl_key"
        assert desired[0]["key"] == "A"
        assert desired[0]["values"]["ctrl_qty"] == "2"
        assert types["ctrl_qty"]["type"] == 6

    def test_prepare_rejects_readonly_and_unknown(self):
        for record in ({"code": "A", "ro": "x"}, {"code": "A", "nope": 1}):
            with pytest.raises(AdapterError) as ei:
                prepare_records([record], _CONTROLS, "code", _OPTIONS)
            assert ei.value.code == "hap_field_invalid"

    def test_prepare_rejects_duplicate_or_empty_key(self):
        with pytest.raises(AdapterError) as ei:
            prepare_records([{"code": "A"}, {"code": "A"}], _CONTROLS, "code", _OPTIONS)
        assert ei.value.code == "hap_key_duplicate"
        # 键字段为必填且被清空 → 先命中"必填不能清空"
        with pytest.raises(AdapterError) as ei2:
            prepare_records([{"code": ""}], _CONTROLS, "code", _OPTIONS)
        assert ei2.value.code == "hap_required_missing"

    def test_prepare_omits_null_but_keeps_key_and_clear_policy(self):
        _key, _types, desired = prepare_records(
            [{"code": "A", "qty": None}], _CONTROLS, "code", _OPTIONS)
        assert "ctrl_qty" not in desired[0]["values"]      # omit：空值不下发
        _key, _types, cleared = prepare_records(
            [{"code": "A", "qty": None}], _CONTROLS, "code",
            {**_OPTIONS, "null_policy": "clear"})
        assert cleared[0]["values"]["ctrl_qty"] == ""

    def test_prepare_rejects_required_cleared(self):
        controls = [dict(_CONTROLS[0]), dict(_CONTROLS[1])]
        controls[1] = {"controlId": "ctrl_qty", "alias": "qty", "controlName": "数量",
                       "type": 6, "required": True}
        with pytest.raises(AdapterError) as ei:
            prepare_records([{"code": "A", "qty": None}], controls, "code",
                            {**_OPTIONS, "null_policy": "clear"})
        assert ei.value.code == "hap_required_missing"

    def test_prepare_rejects_readonly_key(self):
        controls = [dict(_CONTROLS[0], readOnly=True)]
        with pytest.raises(AdapterError) as ei:
            prepare_records([{"code": "A"}], controls, "code", _OPTIONS)
        assert ei.value.code == "hap_key_invalid"


class TestBuildOperations:

    def _desired(self, rows):
        return prepare_records(rows, _CONTROLS, "code", _OPTIONS)

    def test_add_edit_unchanged_classification(self):
        _key, types, desired = self._desired(
            [{"code": "A", "qty": 1}, {"code": "B", "qty": 2}, {"code": "C", "qty": 3}])
        index = {"A": {"rowid": "rA", "values": {"ctrl_key": "A", "ctrl_qty": "1"}},
                 "B": {"rowid": "rB", "values": {"ctrl_key": "B", "ctrl_qty": "9"}}}
        ops, requests = build_operations(desired, index, "ctrl_key", types, _OPTIONS)
        kinds = {op["key"]: op["kind"] for op in ops}
        assert kinds == {"A": "unchanged", "B": "edit", "C": "add"}
        assert ops[1]["patch"] == {"ctrl_qty": "2"}
        assert ops[0]["status"] == "confirmed"          # unchanged 直接确认
        assert sorted(r["kind"] for r in requests) == ["add", "edit"]

    def test_batching_respects_batch_size(self):
        rows = [{"code": f"K{i}", "qty": i} for i in range(5)]
        _key, types, desired = self._desired(rows)
        opts = {**_OPTIONS, "create_batch_size": 2}
        _ops, requests = build_operations(desired, {}, "ctrl_key", types, opts)
        assert [len(r["members"]) for r in requests] == [2, 2, 1]

    def test_delete_diff_only_when_requested(self):
        _key, types, desired = self._desired([{"code": "A", "qty": 1}])
        index = {"A": {"rowid": "rA", "values": {"ctrl_key": "A", "ctrl_qty": "1"}},
                 "Z": {"rowid": "rZ", "values": {"ctrl_key": "Z", "ctrl_qty": "9"}}}
        _ops, requests = build_operations(desired, index, "ctrl_key", types, _OPTIONS)
        assert all(r["kind"] != "delete" for r in requests)
        _ops2, requests2 = build_operations(desired, index, "ctrl_key", types, _OPTIONS,
                                           delete=True)
        deletes = [r for r in requests2 if r["kind"] == "delete"]
        assert len(deletes) == 1
        assert _ops2[deletes[0]["members"][0]]["rowid"] == "rZ"

    def test_readback_unavailable_blocks_edit_decision(self):
        _key, types, desired = self._desired([{"code": "A", "qty": 1}])
        index = {"A": {"rowid": "rA", "values": {"ctrl_key": "A", "ctrl_qty": None}}}
        with pytest.raises(AdapterError) as ei:
            build_operations(desired, index, "ctrl_key", types, _OPTIONS)
        assert ei.value.code == "hap_readback_unavailable"

    def test_summary_counts_confirmed_only(self):
        payload = {"desired": [1, 2, 3], "ops": [
            {"kind": "add", "status": "confirmed"},
            {"kind": "add", "status": "unknown"},
            {"kind": "edit", "status": "confirmed"},
            {"kind": "unchanged", "status": "confirmed"},
        ]}
        assert summary(payload) == {"added": 1, "updated": 1, "unchanged": 1,
                                    "deleted": 0, "pending": 0, "unknown": 1, "total": 3}


# ============================================================
# execute：计划落库 + 写入 + 幂等重放
# ============================================================

class FakeHap:
    """最小 HAP 目标替身：内存行集 + 批量接口，记录调用次数"""

    def __init__(self, rows=None):
        self.rows = {r["rowid"]: dict(r) for r in (rows or [])}
        self.base_url = "https://hap.example.com"
        self._appkey = "ak-1"
        self.config = {}
        self.timeout = 10
        self.before_request = None
        self.calls = {"add": 0, "edit": 0, "delete": 0}

    # --- 适配器接口 ---
    async def get_worksheet_controls(self, worksheet):
        return [dict(c) for c in _CONTROLS]

    async def get_worksheet_rows(self, worksheet):
        return [dict(r) for r in self.rows.values()]

    async def get_worksheet_row(self, worksheet, rowid):
        return dict(self.rows[rowid])

    async def add_worksheet_rows(self, worksheet, rows, trigger):
        # 入参形状：list[list[{'controlId','value'}]]，每个元素是一行的控件列表
        self.calls["add"] += 1
        ids = []
        for row in rows:
            values = {item["controlId"]: item["value"] for item in row}
            rid = f"r{uuid.uuid4().hex[:8]}"
            self.rows[rid] = {**values, "rowid": rid}
            ids.append(rid)
        return ids

    async def edit_worksheet_rows(self, worksheet, rowids, controls, trigger):
        # 入参形状：所有 rowIds 共用同一份 controls（list[{'controlId','value'}]）
        self.calls["edit"] += 1
        patch = {item["controlId"]: item["value"] for item in controls}
        for rid in rowids:
            self.rows[rid].update(patch)

    async def delete_worksheet_row(self, worksheet, rowid):
        self.calls["delete"] += 1
        self.rows.pop(rowid, None)


@pytest.fixture
def bulk_env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/bulk.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _rate_ok(*a, **k):
        return True

    monkeypatch.setattr("app.cache.rate_limiter.rate_limit", _rate_ok)

    async def _seed():
        async with maker() as s:
            from app.models.sync import ApiConnection
            src = ApiConnection(name="源", connection_type="source", protocol="rest",
                                base_url="https://s", auth_type=None, auth_config={},
                                status="active")
            tgt = ApiConnection(name="HAP 目标", connection_type="target", protocol="hap",
                                base_url="https://hap.example.com", auth_type="custom",
                                auth_config={"HAP-Appkey": "ak-1", "HAP-Sign": "sg"},
                                status="active")
            s.add_all([src, tgt])
            await s.commit()
            await s.refresh(src)
            await s.refresh(tgt)
            mapping = FieldMapping(name="桥", source_connection_id=src.id,
                                   target_connection_id=tgt.id, source_table="t",
                                   target_table=WS, sync_mode="full",
                                   mapping_config=_mapping_config())
            s.add(mapping)
            await s.commit()
            await s.refresh(mapping)
            task = SyncTask(mapping_id=mapping.id, task_type="scheduled", status="active",
                            sync_mode="sync")
            s.add(task)
            await s.commit()
            await s.refresh(task)
            buffer = DataBuffer(task_id=task.id, source_data={}, status="processing")
            s.add(buffer)
            await s.commit()
            await s.refresh(buffer)
            mapping_id, task_id, buffer_id = mapping.id, task.id, buffer.id
        return SimpleMapping(mapping_id), task_id, buffer_id

    yield maker, _seed


class SimpleMapping:
    def __init__(self, mapping_id):
        self.id = mapping_id

    @property
    def target_endpoint(self):
        return WS


def _mapping_ns(cfg):
    from types import SimpleNamespace
    return SimpleNamespace(id=uuid.uuid4(), source_table="t", target_table=WS,
                           source_endpoint=None, target_endpoint=WS,
                           mapping_config=cfg, source_headers=None, target_headers=None,
                           incremental_field=None, batch_size=100, sync_mode="full")


class TestExecute:

    def _plan(self, maker, buffer_id):
        async def _run():
            async with maker() as s:
                return await s.scalar(select(HapWritePlan).where(
                    HapWritePlan.buffer_id == buffer_id))
        return asyncio.run(_run())

    def test_first_run_writes_and_completes(self, bulk_env):
        maker, _seed = bulk_env

        async def _run():
            _mapping_obj, task_id, buffer_id = await _seed()
            adapter = FakeHap([{"rowid": "rA", "code": "A", "qty": "1"}])
            cfg = _mapping_config()
            mapping = _mapping_ns(cfg)
            result = await hap_bulk.execute(maker, buffer_id, adapter, mapping,
                                            [{"code": "A", "qty": "5"},
                                             {"code": "B", "qty": "7"}],
                                            task_id=task_id)
            return result, adapter

        result, adapter = asyncio.run(_run())
        assert result["body"]["updated"] == 1        # A 数量变化
        assert result["body"]["added"] == 1          # B 新增
        assert adapter.calls == {"add": 1, "edit": 1, "delete": 0}
        keys = {r.get("ctrl_key") or r.get("code") for r in adapter.rows.values()}
        assert keys == {"A", "B"}
        row_a = next(r for r in adapter.rows.values()
                     if (r.get("ctrl_key") or r.get("code")) == "A")
        assert (row_a.get("ctrl_qty") or row_a.get("qty")) == "5"

    def test_completed_plan_replays_without_writes(self, bulk_env):
        maker, _seed = bulk_env

        async def _run():
            _mapping_obj, task_id, buffer_id = await _seed()
            adapter = FakeHap([{"rowid": "rA", "code": "A", "qty": "1"}])
            mapping = _mapping_ns(_mapping_config())
            first = await hap_bulk.execute(maker, buffer_id, adapter, mapping,
                                           [{"code": "B", "qty": "7"}], task_id=task_id)
            calls_after_first = dict(adapter.calls)
            second = await hap_bulk.execute(maker, buffer_id, adapter, mapping,
                                            [{"code": "B", "qty": "7"}], task_id=task_id)
            return first, second, adapter, calls_after_first

        first, second, adapter, calls_after_first = asyncio.run(_run())
        assert second["plan_id"] == first["plan_id"]
        assert second["body"] == first["body"]
        assert adapter.calls == calls_after_first, "已完成计划重放不得再写目标"

    def test_target_identity_change_blocks_replay(self, bulk_env):
        maker, _seed = bulk_env

        async def _run():
            _mapping_obj, task_id, buffer_id = await _seed()
            adapter = FakeHap([{"rowid": "rA", "code": "A", "qty": "1"}])
            mapping = _mapping_ns(_mapping_config())
            await hap_bulk.execute(maker, buffer_id, adapter, mapping,
                                   [{"code": "B", "qty": "7"}], task_id=task_id)
            adapter.base_url = "https://other.example.com"     # 目标身份变化
            with pytest.raises(AdapterError) as ei:
                await hap_bulk.execute(maker, buffer_id, adapter, mapping,
                                       [{"code": "B", "qty": "7"}], task_id=task_id)
            return ei.value

        err = asyncio.run(_run())
        assert err.code in ("hap_target_changed", "hap_target_busy")

    def test_plan_is_encrypted_and_sealed(self, bulk_env):
        maker, _seed = bulk_env

        async def _run():
            _mapping_obj, task_id, buffer_id = await _seed()
            adapter = FakeHap([{"rowid": "rA", "code": "A", "qty": "1"}])
            mapping = _mapping_ns(_mapping_config())
            await hap_bulk.execute(maker, buffer_id, adapter, mapping,
                                   [{"code": "B", "qty": "7"}], task_id=task_id)
            async with maker() as s:
                return await s.scalar(select(HapWritePlan).where(
                    HapWritePlan.buffer_id == buffer_id))

        plan = asyncio.run(_run())
        assert plan.status == "completed"
        assert plan.active_target is None            # 完成后释放目标互斥
        assert plan.payload.startswith("enc:")       # 计划内容加密落库
        assert not plan.payload.startswith("{")      # 非明文 JSON
        saved = unseal(plan.payload)                 # 密钥可解回（计划可恢复）
        assert saved["config"]["key_field"] == "code"

    def test_scope_config_rejected(self, bulk_env):
        maker, _seed = bulk_env

        async def _run():
            _mapping_obj, task_id, buffer_id = await _seed()
            adapter = FakeHap()
            cfg = _mapping_config()
            cfg["protocol"]["view_id"] = "v1"     # 目标视图 → 拒绝
            mapping = _mapping_ns(cfg)
            with pytest.raises(AdapterError) as ei:
                await hap_bulk.execute(maker, buffer_id, adapter, mapping,
                                       [{"code": "B", "qty": "7"}], task_id=task_id)
            return ei.value

        assert asyncio.run(_run()).code == "hap_scope_invalid"
