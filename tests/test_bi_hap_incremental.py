# -*- coding: utf-8 -*-
"""HAP 增量拉取（add-bi-hap-incremental）测试：策略矩阵 + 合并正确性

全部用**假适配器**（patch `_build_adapter`），不打网络；真实端点验证见
`scripts/hap_incremental_verify.py`。
"""
import asyncio
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.sync import ApiConnection, Base
from app.services import bi_sources

TABLE = "ws_incremental"


def _hap_row(rowid: str, utime: str, name: str, qty: float) -> dict:
    """模拟明道云行：业务键 + 系统字段（rowid/utime 是增量所需的两个）"""
    return {"rowid": rowid, "utime": utime, "name": name, "qty": qty,
            "ctime": utime, "autoid": rowid}


class FakeHapAdapter:
    """假 HAP 适配器：维护"服务端表"，实现两个读接口的真语义

    - get_worksheet_rows（全量 + return_total）
    - get_rows_changed_since（utime gt (since − overlap)，按 utime 降序）
    另记录每次调用，供断言"是否真的只拉了增量"。
    """

    def __init__(self, rows: list[dict], *, total_override: int | None = None):
        self.server: dict[str, dict] = {r["rowid"]: dict(r) for r in rows}
        self.total_override = total_override
        self.calls: list[tuple] = []

    # --- 测试期用的"服务端变更"操作 ---
    def add(self, rowid: str, utime: str, name: str, qty: float) -> None:
        self.server[rowid] = _hap_row(rowid, utime, name, qty)

    def update(self, rowid: str, utime: str, **fields) -> None:
        self.server[rowid].update({"utime": utime, **fields})

    def delete(self, rowid: str) -> None:
        self.server.pop(rowid, None)

    # --- 适配器接口 ---
    def _rows_sorted(self) -> list[dict]:
        return sorted(self.server.values(), key=lambda r: r["utime"], reverse=True)

    async def get_worksheet_rows(self, worksheet_id, view_id=None, page_size=1000,
                                 max_pages=100, filters=None, sort=None,
                                 return_total=False):
        self.calls.append(("full", None))
        rows = self._rows_sorted()
        if return_total:
            return rows, self.total_override if self.total_override is not None \
                else len(rows)
        return rows

    async def get_rows_since(self, worksheet_id, since, *, inclusive=False,
                             view_id=None, page_size=1000, max_pages=10):
        """严格大于（真增量）/ 大于等于（边界刷新）两模式，按 utime 降序"""
        self.calls.append(("delta" if not inclusive else "boundary", since))
        rows = list(self.server.values())
        if since:
            rows = [r for r in rows
                    if (str(r.get("utime") or "") >= str(since) if inclusive
                        else str(r.get("utime") or "") > str(since))]
        delta = sorted(rows, key=lambda r: r["utime"], reverse=True)
        return delta, len(delta)


@pytest.fixture
def app_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/hap_inc.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def _clear_state():
    bi_sources._form_cache.clear()
    bi_sources._hap_snapshots.clear()
    bi_sources._snapshot_info.clear()
    yield
    bi_sources._form_cache.clear()
    bi_sources._hap_snapshots.clear()
    bi_sources._snapshot_info.clear()


async def _add_hap_conn(maker) -> str:
    async with maker() as s:
        conn = ApiConnection(name="HAP 增量源", connection_type="target",
                             protocol="hap", auth_type="custom", auth_config={},
                             status="active")
        s.add(conn)
        await s.commit()
        await s.refresh(conn)
        return str(conn.id)


def _ref(conn_id: str) -> str:
    return json.dumps({"connection_id": conn_id, "form_key": TABLE})


async def _fetch(maker, adapter, conn_id, *, min_rows=0, enabled=True):
    """一次 form 拉取（patch 适配器与阈值；绕过 60s 缓存以逐轮观测）

    合并后取数还会经字段端点取元数据（行键归一到字段字典 path + 脱敏）——
    这里给出与假行同键的可读字段清单，本模块只关心增量策略。
    """
    bi_sources._form_cache.clear()
    fields = [{"path": k, "name": k, "field_id": k, "type": "string", "readable": True}
              for k in ("rowid", "utime", "ctime", "autoid", "name", "qty")]
    with patch.object(bi_sources, "_build_adapter", return_value=adapter), \
         patch.object(bi_sources, "HAP_INCREMENTAL_MIN_ROWS", min_rows), \
         patch.object(bi_sources, "HAP_INCREMENTAL_ENABLED", enabled), \
         patch("app.api.connections.get_connection_fields",
               AsyncMock(return_value={"kind": "hap", "worksheet_id": TABLE,
                                       "fields": fields})):
        async with maker() as s:
            rows = await bi_sources.fetch_form_rows(s, conn_id, TABLE)
    return rows


# 基线行同处一个较早的秒（模拟"批量导入的历史数据"）：水位线 09:00:00；
# 后续变更用 10:05:00 等更晚的秒——如此增量窗口（水位线 −1s）不再覆盖基线秒，
# 若把基线放在水位线同秒，重叠窗口会把基线整批重拉（真机语义也是如此，故夹具按真实分布造）
BASE_ROWS = [
    _hap_row("r1", "2026-09-29 09:00:00", "甲", 1.0),
    _hap_row("r2", "2026-09-29 09:00:00", "乙", 2.0),      # 与 r1/r3 同秒（批量导入场景）
    _hap_row("r3", "2026-09-29 09:00:00", "丙", 3.0),
]


class TestIncrementalFlow:

    def test_bootstrap_full_then_incremental_delta(self, app_db):
        conn_id = asyncio.run(_add_hap_conn(app_db))
        adapter = FakeHapAdapter(BASE_ROWS)

        rows = asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))
        assert len(rows) == 3
        info = bi_sources.snapshot_info(conn_id, TABLE)
        assert info["mode"] == "full" and info["enabled"] is True
        assert adapter.calls == [("full", None)]

        # 新增 2 行 + 修改 1 行 → 增量轮次只回传 3 行
        adapter.add("r4", "2026-09-29 10:05:00", "丁", 4.0)
        adapter.add("r5", "2026-09-29 10:05:00", "戊", 5.0)
        adapter.update("r1", "2026-09-29 10:05:00", qty=99.0)

        rows = asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))
        info = bi_sources.snapshot_info(conn_id, TABLE)
        assert info["mode"] == "incremental"
        # 两段式：真增量（utime 严格大于水位线 09:00:00）= r1 变更 + r4 + r5 = 3；
        # 边界刷新（utime >= 09:00:00）= 基线 3 行（防同秒漏采的有界代价，幂等合并）
        # 边界集 = ge(09:00) 的 5 行 − 真增量 3 行 = 同秒基线 2 行（r2/r3）
        assert info["delta_rows"] == 3 and info["boundary_rows"] == 2
        assert info["boundary_skipped"] is False
        assert info["total_rows"] == 5

        # 再改一行（更晚的秒）：真增量 1 行 + 边界刷新（上轮水位线 10:05 同秒 3 行）
        adapter.update("r2", "2026-09-29 10:09:00", qty=22.0)
        rows = asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))
        info = bi_sources.snapshot_info(conn_id, TABLE)
        assert info["delta_rows"] == 1 and info["boundary_rows"] == 3
        assert info["watermark"] == "2026-09-29 10:09:00"
        assert {r["rowid"]: r["qty"] for r in rows}["r2"] == 22.0

        # 稳态（无变化）：真增量为 0，只有边界刷新回传"最新秒"的 1 行
        rows = asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))
        info = bi_sources.snapshot_info(conn_id, TABLE)
        assert info["delta_rows"] == 0 and info["boundary_rows"] == 1
        assert len(rows) == 5, "稳态重复回传不得改变快照行数"
        by_id = {r["rowid"]: r for r in rows}
        assert by_id["r1"]["qty"] == 99.0          # 修改已合并（幂等 upsert）
        assert by_id["r4"]["name"] == "丁" and by_id["r5"]["name"] == "戊"
        # 每轮增量 = 严格大于一趟（delta）+ 边界刷新一趟（boundary），同一水位线
        assert adapter.calls[-2:] == [("delta", "2026-09-29 10:09:00"),
                                      ("boundary", "2026-09-29 10:09:00")]
        assert info["watermark"] == "2026-09-29 10:09:00"

    def test_merged_snapshot_equals_full_pull(self, app_db):
        """核心不变式：增量合并后的快照 == 全量拉取结果（逐行逐字段）"""
        conn_id = asyncio.run(_add_hap_conn(app_db))
        adapter = FakeHapAdapter(BASE_ROWS)
        asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))
        adapter.add("r9", "2026-09-29 11:00:00", "壬", 9.0)
        adapter.update("r2", "2026-09-29 11:00:00", name="乙改")
        merged = asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))

        # 强制全量（对账）后再比
        bi_sources._hap_snapshots.clear()
        full = asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))
        assert sorted(merged, key=lambda r: r["rowid"]) == \
               sorted(full, key=lambda r: r["rowid"])

    def test_same_second_overlap_is_idempotent(self, app_db):
        """重叠窗口重复回传同秒行 → 幂等（行数不变、值不变）"""
        conn_id = asyncio.run(_add_hap_conn(app_db))
        adapter = FakeHapAdapter(BASE_ROWS)
        asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))
        # 只改 r3，且 utime 停在**水位线同秒** → 水位线不前进，重叠窗口把同秒行整批回传
        adapter.update("r3", "2026-09-29 09:00:00", qty=33.0)
        rows = asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))
        info = bi_sources.snapshot_info(conn_id, TABLE)
        assert info["delta_rows"] == 0          # 未超过水位线 → 无真增量
        assert info["boundary_rows"] == 3       # 边界刷新整批回传（预期）
        assert len(rows) == 3                   # 幂等合并后行数不变
        assert {r["rowid"]: r["qty"] for r in rows}["r3"] == 33.0

    def test_delete_waits_for_reconcile(self, app_db):
        """删除感知的让渡语义：增量轮次不感知，对账轮后消失"""
        conn_id = asyncio.run(_add_hap_conn(app_db))
        adapter = FakeHapAdapter(BASE_ROWS)
        asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))
        adapter.delete("r2")
        rows = asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))
        assert len(rows) == 3, "增量轮次不感知删除（预期行为）"

        # 拨老 full_at → 触发对账
        snap = bi_sources._hap_snapshots[f"{conn_id}:{TABLE}"]
        snap["full_at"] -= bi_sources.HAP_INCREMENTAL_RECONCILE_SECONDS + 1
        rows = asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))
        info = bi_sources.snapshot_info(conn_id, TABLE)
        assert info["mode"] == "full" and info["reason"] == "reconcile_due"
        assert len(rows) == 2 and {r["rowid"] for r in rows} == {"r1", "r3"}

    def test_delta_too_large_falls_back_to_full(self, app_db):
        conn_id = asyncio.run(_add_hap_conn(app_db))
        adapter = FakeHapAdapter(BASE_ROWS)
        asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))
        for i in range(10):
            adapter.add(f"x{i}", "2026-09-29 12:00:00", f"新{i}", float(i))
        with patch.object(bi_sources, "HAP_INCREMENTAL_DELTA_ABS_CAP", 5):
            rows = asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))
        info = bi_sources.snapshot_info(conn_id, TABLE)
        assert info["mode"] == "full" and info["reason"] == "delta_too_large"
        assert len(rows) == 13

    def test_small_table_stays_full(self, app_db):
        conn_id = asyncio.run(_add_hap_conn(app_db))
        adapter = FakeHapAdapter(BASE_ROWS)
        rows = asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=1000))
        info = bi_sources.snapshot_info(conn_id, TABLE)
        assert info["mode"] == "full" and info["reason"] == "below_threshold"
        assert len(rows) == 3
        assert all(c[0] == "full" for c in adapter.calls), "小表不得走增量"
        assert f"{conn_id}:{TABLE}" not in bi_sources._hap_snapshots

    def test_disabled_switch(self, app_db):
        conn_id = asyncio.run(_add_hap_conn(app_db))
        adapter = FakeHapAdapter(BASE_ROWS)
        asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=0, enabled=False))
        info = bi_sources.snapshot_info(conn_id, TABLE)
        assert info["mode"] == "full" and info["reason"] == "disabled"
        assert all(c[0] == "full" for c in adapter.calls)

    def test_missing_utime_degrades_to_full(self, app_db):
        conn_id = asyncio.run(_add_hap_conn(app_db))
        rows = [dict(r) for r in BASE_ROWS]
        rows[1]["utime"] = ""                       # 有一行缺 utime → 不可增量
        adapter = FakeHapAdapter(rows)
        asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=0))
        info = bi_sources.snapshot_info(conn_id, TABLE)
        assert info["mode"] == "full"
        assert info["reason"] == "degrade:missing_rowid_or_utime"
        assert f"{conn_id}:{TABLE}" not in bi_sources._hap_snapshots

    def test_incremental_failure_falls_back_and_self_heals(self, app_db):
        conn_id = asyncio.run(_add_hap_conn(app_db))
        adapter = FakeHapAdapter(BASE_ROWS)
        asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))

        boom = AsyncMock(side_effect=RuntimeError("源端 5xx"))
        with patch.object(adapter, "get_rows_since", boom):
            rows = asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))
        info = bi_sources.snapshot_info(conn_id, TABLE)
        assert info["mode"] == "full"
        assert info["reason"] == "incremental_failed:RuntimeError"
        assert len(rows) == 3
        # 下一轮自愈：快照已由全量重建 → 又能走增量
        adapter.add("r7", "2026-09-29 13:00:00", "庚", 7.0)
        asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))
        assert bi_sources.snapshot_info(conn_id, TABLE)["mode"] == "incremental"

    def test_over_cap_rejects(self, app_db, monkeypatch):
        """HAP 仍受 10 万硬闸：合并后超上限**拒绝**返回（不把部分数据当完整结果）"""
        from app.services.bi_sources import SourceError

        conn_id = asyncio.run(_add_hap_conn(app_db))
        adapter = FakeHapAdapter(BASE_ROWS)
        monkeypatch.setattr(bi_sources, "MAX_FORM_ROWS", 2)
        with pytest.raises(SourceError) as ei:
            asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=0))
        assert "上限" in str(ei.value)


class TestAdapterIncrementalCall:
    """适配器参数形状（真实端点语义：filters 有效、filterControls 被忽略）"""

    def test_filters_and_sort_payload(self):
        from app.adapters.hap import HapV3Adapter

        adapter = HapV3Adapter({"app_key": "k", "sign": "s",
                                "base_url": "https://api.example.com"})
        captured: dict = {}

        async def fake_post(path: str, body: dict) -> dict:
            captured["path"] = path
            captured["body"] = body
            return {"success": True, "data": {"rows": [{"rowid": "r1"}], "total": 1}}

        adapter._post_worksheet = fake_post                     # type: ignore[assignment]
        rows, total = asyncio.run(adapter.get_rows_since(
            "ws1", "2026-09-29 10:00:00"))
        assert rows == [{"rowid": "r1"}] and total == 1
        body = captured["body"]
        assert captured["path"] == "/v2/open/worksheet/getFilterRows"
        # 参数名必须是 filters（实测 filterControls 被服务端静默忽略）
        assert "filters" in body and "filterControls" not in body
        f = body["filters"][0]
        assert f["controlId"] == "utime" and f["dataType"] == 16
        assert f["filterType"] == 13 and f["value"] == "2026-09-29 10:00:00"  # 严格大于
        assert body["sortId"] == "utime" and body["sortType"] == "desc"
        assert body["listType"] == "1" and body["getSystemControl"] is True

        # 边界刷新：filterType 14（大于等于），值同为水位线
        asyncio.run(adapter.get_rows_since("ws1", "2026-09-29 10:00:00",
                                           inclusive=True))
        f2 = captured["body"]["filters"][0]
        assert f2["filterType"] == 14 and f2["value"] == "2026-09-29 10:00:00"

    def test_no_since_sends_no_filters(self):
        from app.adapters.hap import HapV3Adapter

        adapter = HapV3Adapter({"app_key": "k", "sign": "s",
                                "base_url": "https://api.example.com"})
        captured: dict = {}

        async def fake_post(path: str, body: dict) -> dict:
            captured["body"] = body
            return {"success": True, "data": {"rows": [], "total": 0}}

        adapter._post_worksheet = fake_post                     # type: ignore[assignment]
        rows, total = asyncio.run(adapter.get_rows_since("ws1", None))
        assert rows == [] and total == 0
        assert "filters" not in captured["body"]


class TestFormRowsApiSnapshot:
    """GET /bi/form-rows 透出快照信息（模式/增量行数/水位线/上次全量时间）"""

    def test_snapshot_field_present(self, app_db):
        from app.api.bi import snapshot_info as api_snapshot_info

        conn_id = asyncio.run(_add_hap_conn(app_db))
        adapter = FakeHapAdapter(BASE_ROWS)
        asyncio.run(_fetch(app_db, adapter, conn_id, min_rows=3))
        info = api_snapshot_info(conn_id, TABLE)
        assert info["mode"] == "full"
        # DB 源无快照信息 → None（前端不显示增量徽标）
        assert api_snapshot_info(str(uuid.uuid4()), "t") is None
