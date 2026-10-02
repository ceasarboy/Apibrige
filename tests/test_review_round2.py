# -*- coding: utf-8 -*-
"""第二轮代码审查修复回归（2026-10-01，审查报告 P0/P1/已确认 P2）

覆盖：
- P0-1  删除对账源范围守卫（validate_policy 顶部 + sync_handler 双层）
- P1-2  data_hash 只认成功批次（_completed_record_hashes）
- P1-3  auto × delete_sync 显式拒绝
- P1-5  compute_hash 对 datetime/Decimal 安全
- P1-7  SQLite delete_rows 分块
- P1-8  ON CONFLICT 无约束 → upsert_key_not_unique
- P1-9  探针 SideInput 白名单 + 敏感字段 fail-closed
- P1-10 绝对 endpoint 封堵（rest_contract v1）
- P1-11 BI「其他」归并哨兵（dim_hash 不撞真实值）
- P2    scope_metric_list fail-closed / scope_defn 三口径 / scope_buckets 值白名单
- P2    redact_text 增强 / 维度筛选数值归一 / cron 重算
"""
import base64
import asyncio
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.adapters.errors import AdapterError
from app.cache.idempotency import compute_hash
from app.models.sync import Base, DataBuffer
from app.security.desensitize import redact_text

TABLE = "shift_metrics"


# ============================================================
# P0-1 / P1-3：删除对账守卫
# ============================================================

class TestDeleteSyncGuards:

    def _mapping(self, cfg, sync_mode="full"):
        return SimpleNamespace(mapping_config=cfg, sync_mode=sync_mode)

    def test_filtered_source_rejected_for_db_target(self):
        """P0-1：DB 目标 + delete_sync + 过滤源 → 一律拒绝（原守卫只护 HAP 目标）"""
        from app.services.hap_target import validate_policy
        # v2 形状：protocol.source.filters
        mapping = self._mapping({
            "version": 2, "delete_sync": True, "key_field": "k",
            "protocol": {"source": {"filters": [{"field": "line", "op": "eq", "value": "A"}]}},
        })
        target = SimpleNamespace(protocol="sqlite", db_type="sqlite")
        with pytest.raises(AdapterError) as ei:
            validate_policy(mapping, target)
        assert ei.value.code == "delete_sync_filtered_source"
        # v1 形状：protocol 平铺（side_config 对 v1 返回整个 protocol dict）
        mapping_v1 = self._mapping({
            "delete_sync": True, "key_field": "k",
            "protocol": {"filters": [{"field": "line", "op": "eq", "value": "A"}]},
        })
        with pytest.raises(AdapterError):
            validate_policy(mapping_v1, target)

    def test_filtered_source_rejected_for_hap_target(self):
        from app.services.hap_target import validate_policy
        mapping = self._mapping({
            "version": 2, "hap_delete_sync": True, "key_field": "k",
            "protocol": {"source": {"view_id": "v1"}},
        })
        target = SimpleNamespace(protocol="hap", db_type=None)
        with pytest.raises(AdapterError):
            validate_policy(mapping, target)

    def test_clean_source_still_allowed(self):
        from app.services.hap_target import validate_policy
        mapping = self._mapping({"delete_sync": True, "key_field": "k"})
        target = SimpleNamespace(protocol="sqlite", db_type="sqlite")
        validate_policy(mapping, target)   # 不抛即通过

    def test_auto_mode_rejects_delete_sync(self, monkeypatch):
        """P1-3：auto × delete_sync 入口显式拒绝（不再分数据量两种错法）"""
        from app.services import router

        async def _boom(*a, **kw):
            raise AssertionError("delete_sync 的 auto 任务不应取源")

        monkeypatch.setattr(router, "_fetch_with_failure_log", _boom)
        task = SimpleNamespace(sync_mode="auto", auto_threshold=100,
                               depends_on_task_id=None, id="t1",
                               webhook_config=None, conflict_strategy=None)
        mapping = SimpleNamespace(mapping_config={"delete_sync": True, "key_field": "k"},
                                  sync_mode="full", id="m1")
        out = asyncio.run(router.route_transfer(
            AsyncSessionStub(), task, mapping,
            SimpleNamespace(protocol="sqlite", db_type="sqlite"),
            SimpleNamespace(protocol="sqlite", db_type="sqlite")))
        assert out["status"] == "failed"
        assert "sync" in out["error"]


class AsyncSessionStub:
    async def execute(self, *a, **kw):
        raise AssertionError("auto×delete_sync 应在取源前拒绝")

    async def get(self, *a, **kw):
        return None


# ============================================================
# P1-2：data_hash 只认成功批次
# ============================================================

class TestCompletedRecordHashes:
    def test_only_completed_hashes_counted(self, tmp_path):
        from app.services.sync_handler import _completed_record_hashes

        async def _run():
            engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/h.db")
            async with engine.begin() as c:
                await c.run_sync(Base.metadata.create_all)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                tid = uuid.uuid4()
                s.add(DataBuffer(task_id=tid, status="completed",
                                 data_hash="h_ok1,h_ok2", source_data={}))
                s.add(DataBuffer(task_id=tid, status="failed",
                                 data_hash="h_bad", source_data={}))
                s.add(DataBuffer(task_id=tid, status="pending",
                                 data_hash="h_pending", source_data={}))
                await s.commit()
                self_id = uuid.uuid4()
                return await _completed_record_hashes(s, tid, self_id), self_id

        hashes, _ = asyncio.run(_run())
        assert hashes == {"h_ok1", "h_ok2"}   # failed/pending 不参与冲突比对


# ============================================================
# P1-5：JSON 安全哈希
# ============================================================

def test_compute_hash_handles_driver_types():
    h1 = compute_hash({"d": datetime(2026, 1, 1, tzinfo=timezone.utc)})
    h2 = compute_hash({"n": Decimal("1.50")})
    assert h1 and h2 and h1 != h2


# ============================================================
# P1-7 / P1-8：SQLite 连接器
# ============================================================

@pytest.fixture()
def sqlite_conn(tmp_path):
    from app.connectors.database import SqliteConnector
    db = str(tmp_path / "conn.db")
    c = SqliteConnector({"database": db})
    return c, db


class TestSqliteConnectorGuards:

    def test_delete_rows_chunked(self, sqlite_conn):
        """P1-7：>500 个键分块删除（旧实现单条 IN，大集合直接抛变量上限）"""
        conn, db = sqlite_conn
        raw = __import__("sqlite3").connect(db)
        raw.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v INT)")
        raw.executemany("INSERT INTO t VALUES (?, 1)",
                        [(f"k{i:05d}",) for i in range(1234)])
        raw.commit()
        raw.close()
        deleted = asyncio.run(conn.delete_rows(
            "t", "k", [f"k{i:05d}" for i in range(1234)]))
        assert deleted == 1234

    def test_upsert_without_unique_constraint_actionable(self, sqlite_conn):
        """P1-8：key_field 无唯一约束 → 中文可行动错误（而非驱动原文）"""
        conn, db = sqlite_conn
        raw = __import__("sqlite3").connect(db)
        raw.execute("CREATE TABLE t2 (k TEXT, v INT)")   # 无 PK/UNIQUE
        raw.commit()
        raw.close()
        with pytest.raises(AdapterError) as ei:
            asyncio.run(conn.push_data("t2", [{"k": "a", "v": 1}],
                                       key_field="k", mode="upsert"))
        assert ei.value.code == "upsert_key_not_unique"

    def test_upsert_with_pk_still_works(self, sqlite_conn):
        conn, db = sqlite_conn
        raw = __import__("sqlite3").connect(db)
        raw.execute("CREATE TABLE t3 (k TEXT PRIMARY KEY, v INT)")
        raw.commit()
        raw.close()
        n = asyncio.run(conn.push_data("t3", [{"k": "a", "v": 1}, {"k": "a", "v": 2}],
                                       key_field="k", mode="upsert"))
        assert n == 2
        raw = __import__("sqlite3").connect(db)
        assert raw.execute("SELECT v FROM t3 WHERE k='a'").fetchone()[0] == 2


# ============================================================
# P1-9 / P1-10：探针输入与端点封堵
# ============================================================

class TestProbeInputGuards:
    def _side(self, **over):
        from app.api.probes import SideInput
        base = dict(protocol="rest", base_url="https://src.example.com")
        base.update(over)
        return SideInput(**base)

    def test_auth_type_whitelist(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            self._side(auth_type="Bearer", auth_config={"token": "t"})
        assert self._side(auth_type="bearer").auth_type == "bearer"
        assert self._side(auth_type=None).auth_type is None

    def test_endpoint_must_be_relative(self):
        from pydantic import ValidationError
        for bad in ("http://169.254.169.254/x", "//evil.example/x",
                    "https://attacker.example/push"):
            with pytest.raises(ValidationError):
                self._side(endpoint=bad)
        assert self._side(endpoint="/api/items").endpoint == "/api/items"

    def test_unknown_auth_type_fail_closed(self, with_encrypt_key):
        """P1-9：未知 auth_type 的配置按 custom 全量脱敏/加密（不再静默放行）"""
        from app.security.crypto import encrypt_auth_config, sensitive_auth_fields
        assert sensitive_auth_fields("Bearer", {"token": "t"}) == ["token"]
        out = encrypt_auth_config(None, {"token": "S3CRET"})
        assert out["token"].startswith("enc:")

    def test_v1_absolute_endpoint_rejected(self):
        """P1-10：v1 也不再放行绝对 endpoint（凭据外送向量）"""
        from app.services.rest_contract import prepare_request
        config = {"base_url": "https://api.example.com", "config_version": 1}
        with pytest.raises(AdapterError) as ei:
            prepare_request(config, "http://169.254.169.254/latest", [{"a": 1}])
        assert ei.value.code == "invalid_target_endpoint"

    def test_v1_relative_endpoint_unchanged(self):
        from app.services.rest_contract import prepare_request
        config = {"base_url": "https://api.example.com/v1", "config_version": 1}
        out = prepare_request(config, "items", [{"a": 1}])
        assert out["url"] == "https://api.example.com/v1/items"


# ============================================================
# P1-11：BI「其他」归并哨兵
# ============================================================

class TestOthersSentinel:
    def test_display_dims_restores_label(self):
        from app.services.bi_aggregator import OTHERS_KEY, OTHERS_LABEL, display_dims
        assert display_dims({"产线": OTHERS_KEY}) == {"产线": OTHERS_LABEL}
        assert display_dims({"产线": "其他"}) == {"产线": "其他"}
        assert display_dims("not-a-dict") == "not-a-dict"

    def test_real_others_value_no_hash_collision(self, tmp_path):
        """P1-11：真实维度值「其他」+ Top N 归并 → 写桶不再撞唯一索引"""
        from app.models.bi import BiMetricBucket
        from app.services.bi_aggregator import aggregate_rows, write_window
        from app.models.bi import Base as BiBase

        async def _run():
            engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/bi.db")
            async with engine.begin() as c:
                await c.run_sync(Base.metadata.create_all)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                defn = {"metric_key": "m1",
                        "dimensions": [{"name": "类别", "field": "cat"}],
                        "measures": [{"key": "n", "agg": "count"}],
                        "top_n": 1}
                # 101 组，其中一组维度值就是字面量「其他」
                rows = [{"cat": f"g{i}", "x": 1} for i in range(100)]
                rows += [{"cat": "其他", "x": 1}]
                scanned = [{"data": r, "fetched_at": datetime.now(timezone.utc)}
                           for r in rows]
                entries = aggregate_rows(scanned, defn)
                assert any(e.get("is_others") for e in entries)
                await write_window(s, "m1", datetime(2026, 10, 1), entries)
                return len(entries)

        assert asyncio.run(_run()) >= 2   # 不抛 IntegrityError 即通过

    def test_load_buckets_translates_sentinel(self, tmp_path):
        """读取出口：哨兵还原为「其他」+ is_others 标记 + 数值维度归一匹配"""
        from app.api.bi import _load_buckets
        from app.models.bi import BiMetricBucket

        async def _run():
            engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/bi2.db")
            async with engine.begin() as c:
                await c.run_sync(Base.metadata.create_all)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                s.add(BiMetricBucket(metric_key="m", bucket_start=datetime(2026, 10, 1),
                                     dim_hash="a", dimensions={"编号": 5.0},
                                     values={"n": 1}))
                s.add(BiMetricBucket(metric_key="m", bucket_start=datetime(2026, 10, 1),
                                     dim_hash="b", dimensions={"编号": "__others__"},
                                     values={"n": 2}))
                await s.commit()
                defn = SimpleNamespace(metric_key="m", granularity="1min")
                out_all = await _load_buckets(s, defn, None, None, None)
                out_num = await _load_buckets(s, defn, None, None, {"编号": "5"})
                return out_all, out_num

        out_all, out_num = asyncio.run(_run())
        assert out_all[1]["dimensions"] == {"编号": "其他"}
        assert out_all[1]["is_others"] is True
        assert len(out_num) == 1 and out_num[0]["dimensions"]["编号"] == 5.0


# ============================================================
# P2：scope_* 与杂项
# ============================================================

from unittest.mock import patch  # noqa: E402

from tests.test_bi_access_scope import make_viewer  # noqa: E402

_TEST_KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()


@pytest.fixture
def with_encrypt_key():
    """注入测试用加密密钥（与 test_security.py 同款）"""
    with patch("app.security.crypto.settings") as mock_settings:
        mock_settings.encrypt_key = _TEST_KEY
        yield mock_settings


class TestScopeHardening:
    DEFN = {
        "metric_key": "m", "chart_type": "bar",
        "dimensions": [{"name": "产线", "field": "line"}],
        "measures": [{"key": "产量", "agg": "sum", "field": "qty"},
                     {"key": "总成本", "agg": "sum", "field": "cost"}],
        "computed": [{"key": "c1", "name": "c1", "expr": "[cost]*1.1"}],
    }

    def test_scope_metric_list_fail_closed_on_empty_board(self):
        """P2 #12：授权看板没有摆放任何卡 → 列表全不可见（不再放行）"""
        from app.services.bi_access import scope_metric_list
        visible, hidden = scope_metric_list(
            [dict(self.DEFN)], make_viewer(boards=["board_empty"]),
            {"board_empty": set()}, ["board_empty"])
        assert visible == [] and hidden == ["m"]

    def test_scope_defn_strips_by_field_and_computed(self):
        """P2 #13：key≠field 的度量与引用隐藏字段的派生列都被剥离"""
        from app.services.bi_access import scope_defn
        out = scope_defn(self.DEFN, make_viewer(hidden=["cost"]))
        assert [m["key"] for m in out["measures"]] == ["产量"]
        assert out.get("computed") in (None, [],)   # c1 引用了 [cost]

    def test_scope_buckets_values_whitelist(self):
        """P2 #13 值层：raw 输出不再带改名隐藏度量的值"""
        from app.services.bi_access import scope_buckets
        buckets = [{"bucket_start": None, "dimensions": {"产线": "A线"},
                    "values": {"产量": 3, "总成本": 9}}]
        out = scope_buckets(buckets, make_viewer(hidden=["cost"]), self.DEFN)
        assert out[0]["values"] == {"产量": 3}


class TestMiscP2:
    def test_redact_text_json_and_bearer(self):
        assert redact_text('{"token": "abc"}') == '{"token": "***"}'
        out = redact_text('Authorization: Bearer eyJ.a.b')
        assert "eyJ" not in out
        assert redact_text("app-key: sk-1") == "app-key: ***"
        assert redact_text("普通文本") == "普通文本"
