# -*- coding: utf-8 -*-
"""BI 明细分页查询测试（add-bi-detail-query）

覆盖：DB 源分页/排序（复用 fetch_data 参数化原语）、has_more 探测、
keyword 降级内存（fallback 标记）、HAP 源缓存内分页、旧调用兼容、
格式化辅助（数值/文本混合排序、关键字过滤）。
"""
import asyncio
import json
import sqlite3
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.sync import ApiConnection, Base
from app.services import bi_sources
from app.services.bi_sources import (
    DETAIL_PAGE_MAX,
    _filter_rows_in_memory,
    _sort_rows_in_memory,
    fetch_form_rows_page,
)

CONN_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TABLE = "page_items"


@pytest.fixture(autouse=True)
def _clear_form_cache():
    bi_sources._form_cache.clear()
    yield
    bi_sources._form_cache.clear()


@pytest.fixture
def page_db(tmp_path):
    """SQLite 库：250 行测试表 + 指向它的 ApiConnection 记录"""
    db_path = tmp_path / "paged.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE page_items (id INTEGER, name TEXT, qty REAL)")
    conn.executemany("INSERT INTO page_items VALUES (?,?,?)",
                     [(i, f"项目-{i:03d}", float((i * 7) % 100)) for i in range(250)])
    conn.commit()
    conn.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/meta.db")

    async def _init():
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        async with maker() as s:
            s.add(ApiConnection(
                id=uuid.UUID(CONN_ID), name="分页测试库", connection_type="target",
                protocol="sqlite", db_type="sqlite",
                db_config={"database": str(db_path).replace("\\", "/")},
                status="active"))
            await s.commit()

    maker = async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(_init())
    yield maker
    asyncio.run(engine.dispose())


def _page(maker, **kw):
    async def _run():
        async with maker() as s:
            return await fetch_form_rows_page(s, CONN_ID, TABLE, **kw)
    return asyncio.run(_run())


class TestDbPaging:
    def test_first_page_and_has_more(self, page_db):
        out = _page(page_db, page=1, page_size=100)
        assert len(out["rows"]) == 100
        assert out["has_more"] is True
        assert out["total"] is None            # COUNT 不下推（大表友好）
        assert out["fallback"] is None
        assert out["rows"][0]["name"] == "项目-000"

    def test_last_page_no_more(self, page_db):
        out = _page(page_db, page=3, page_size=100)
        assert len(out["rows"]) == 50
        assert out["has_more"] is False

    def test_sort_desc_falls_back_to_memory(self, page_db):
        # 驱动 fetch_data 的 order_by 仅升序 → desc 降级内存路径并标记
        out = _page(page_db, page=1, page_size=5, sort_field="id", order="desc")
        assert out["fallback"] == "sort_memory"
        assert [r["id"] for r in out["rows"]] == [249, 248, 247, 246, 245]

    def test_sort_asc_pushed_down(self, page_db):
        out = _page(page_db, page=1, page_size=5, sort_field="id", order="asc")
        assert out["fallback"] is None
        assert [r["id"] for r in out["rows"]] == [0, 1, 2, 3, 4]

    def test_page_size_capped(self, page_db):
        out = _page(page_db, page=1, page_size=10_000)
        assert out["page_size"] == DETAIL_PAGE_MAX

    def test_keyword_falls_back_to_memory(self, page_db):
        out = _page(page_db, page=1, page_size=10, keyword="项目-01")
        assert out["fallback"] == "keyword_memory"
        assert out["total"] is not None and out["total"] == 10  # 010~019
        assert all("项目-01" in r["name"] for r in out["rows"])


class TestHapMemoryPath:
    def test_memory_paging_marks_fallback(self, page_db):
        fake_rows = [{"name": f"行-{i}", "v": i} for i in range(30)]

        async def _fake_fetch(session, connection_id, form_key):
            return fake_rows

        with patch.object(bi_sources, "fetch_form_rows", _fake_fetch), \
             patch.object(bi_sources, "_load_connection",
                          AsyncMockConnection("hap")):
            out = _page(page_db, page=2, page_size=10, sort_field="v", order="desc")
        assert out["fallback"] == "memory_paging"
        assert out["total"] == 30
        assert [r["v"] for r in out["rows"]] == [19, 18, 17, 16, 15, 14, 13, 12, 11, 10]

    def test_memory_keyword_filter(self, page_db):
        fake_rows = [{"name": "A线", "v": 1}, {"name": "B线", "v": 2},
                     {"name": "ax", "v": 3}]

        async def _fake_fetch(session, connection_id, form_key):
            return fake_rows

        with patch.object(bi_sources, "fetch_form_rows", _fake_fetch), \
             patch.object(bi_sources, "_load_connection",
                          AsyncMockConnection("hap")):
            out = _page(page_db, page=1, page_size=10, keyword="a线")
        assert out["total"] == 1
        assert out["rows"][0]["name"] == "A线"


class AsyncMockConnection:
    """patch 用：伪造 _load_connection 返回 HAP 连接对象"""

    def __init__(self, protocol):
        self._protocol = protocol

    async def __call__(self, session, connection_id):
        from types import SimpleNamespace
        return SimpleNamespace(db_type=None, protocol=self._protocol,
                               db_config={}, id=CONN_ID, name="mock")


class TestHelpers:
    def test_sort_mixed_types(self):
        rows = [{"v": "b"}, {"v": 10}, {"v": None}, {"v": 2}, {"v": "a"}]
        asc = [r["v"] for r in _sort_rows_in_memory(rows, "v")]
        assert asc[0] == 2 and asc[1] == 10 and asc[-1] == "b"
        desc = [r["v"] for r in _sort_rows_in_memory(rows, "v", "desc")]
        assert desc[0] == "b"

    def test_sort_no_field_identity(self):
        rows = [{"v": 2}, {"v": 1}]
        assert _sort_rows_in_memory(rows, None) is rows

    def test_filter_case_insensitive_any_value(self):
        rows = [{"a": "Hello", "b": 1}, {"a": "x", "b": "WORLD"}]
        assert len(_filter_rows_in_memory(rows, "world")) == 1
        assert len(_filter_rows_in_memory(rows, "hello")) == 1
        assert len(_filter_rows_in_memory(rows, "  ")) == 2  # 空关键字不过滤
