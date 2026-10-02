# -*- coding: utf-8 -*-
"""字段字典 / 字段策略（合并后实现：services/field_policy.py + api/field_dictionary.py）

合并背景：发行包（主分支）把原 app/services/field_dict.py 重构为 field_policy：
- 保存端点 PUT /connections/{id}/fields 移到 api/field_dictionary.py（同路径，前端零改动）
- 规则**生效方式**变为"映射显式声明 field_policy=inherit"：执行/建轮时解析并冻结，
  输出规则变化不再被整批幂等标记短路（field_policy.execution_key）
- 行过滤（源 readable=false 剔除、目标 writable=false 剔除）仍在转换输出层生效

本文件按合并后的实现重写；原 field_dict 纯函数级用例（compute_drop_keys /
filter_records / load_field_defs）与其同步内部断言已随实现下线。
"""
import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.adapters.errors import AdapterError
from app.database import get_session
from app.main import app
from app.models.sync import ApiConnection, Base, ConnectionField
from app.services.field_policy import (
    definitions, effective_config, execution_key, identity,
)
from tests.conftest import TEST_ADMIN_TOKEN


# ============================================================
# 纯函数：标识归一 / 幂等键
# ============================================================

class TestIdentityAndLookup:

    def test_db_identity_lowercases_oracle_and_sqlite(self):
        assert identity(SimpleNamespace(db_type="oracle"), "  ABC ") == "abc"
        assert identity(SimpleNamespace(db_type="sqlite"), "SKU") == "sku"

    def test_db_identity_keeps_case_for_mysql_pg(self):
        assert identity(SimpleNamespace(db_type="mysql"), "SKU") == "SKU"

    def test_hap_identity_is_exact(self):
        assert identity(SimpleNamespace(db_type=None), "SKU") == "SKU"
        assert identity(SimpleNamespace(db_type=None), None) == ""

    def test_execution_key_depends_on_rules(self):
        a = execution_key("t1", {"fields": [{"source": "a", "target": "b"}]})
        b = execution_key("t1", {"fields": [{"source": "a", "target": "c"}]})
        assert a != b and a.startswith("t1:fields:")


# ============================================================
# API：保存 / 回显（field_dictionary.py + connections.py 的 enrich）
# ============================================================

_HAP_CONTROLS = [
    {"controlId": "ctrl_name", "alias": "name", "controlName": "名称", "type": 2},
    {"controlId": "ctrl_qty", "alias": "qty", "controlName": "数量", "type": 6},
    {"controlId": "rowid", "alias": "rowid", "controlName": "记录ID", "type": 2,
     "readOnly": True, "primaryKey": True},
]


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    """临时库 + 依赖覆盖的 TestClient；HAP 控件定义用假适配器替代（不打网络）"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/fd.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _seed():
        async with maker() as session:
            hap = ApiConnection(name="HAP", connection_type="source", protocol="hap",
                                auth_type="custom", auth_config={}, status="active")
            db = ApiConnection(name="库", connection_type="source", protocol="sqlite",
                               db_type="sqlite",
                               db_config={"database": "x"}, status="active")
            session.add_all([hap, db])
            await session.commit()
            await session.refresh(hap)
            await session.refresh(db)
            return str(hap.id), str(db.id)

    hap_id, db_id = asyncio.run(_seed())

    class FakeAdapter:
        def __init__(self, cfg):
            pass

        async def get_worksheet_controls(self, worksheet_id):
            return [dict(c) for c in _HAP_CONTROLS]

    # 合并后 connections.py 在函数内 `from app.adapters.hap import HapV3Adapter`，
    # 故替换定义模块上的类
    monkeypatch.setattr("app.adapters.hap.HapV3Adapter", FakeAdapter)
    monkeypatch.setattr("app.services.adapter_config.build_adapter_config",
                        lambda conn: {})

    async def override():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = override
    client = TestClient(app, headers={"X-Admin-Token": TEST_ADMIN_TOKEN})
    yield client, hap_id, db_id
    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


class TestFieldDictionaryApi:

    def test_get_fields_uses_alias_as_path(self, api_env):
        """HAP 行键口径：path = alias||controlId（AGENTS 经验 19）"""
        client, hap_id, _db_id = api_env
        resp = client.get(f"/api/v1/connections/{hap_id}/fields",
                          params={"worksheet_id": "ws1"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        paths = [f["path"] for f in body["fields"]]
        assert paths[0] == "name" and "qty" in paths
        rowid = next(f for f in body["fields"] if f["path"] == "rowid")
        assert rowid["readable"] is True and rowid["writable"] is False

    def test_put_saves_and_get_echoes(self, api_env):
        client, hap_id, _db_id = api_env
        payload = {"fields": [
            {"path": "qty", "field_id": "ctrl_qty", "name": "数量（件）",
             "readable": True, "writable": True},
            {"path": "name", "field_id": "ctrl_name", "name": "名称",
             "readable": False, "writable": True},
        ]}
        resp = client.put(f"/api/v1/connections/{hap_id}/fields", json=payload,
                          params={"worksheet_id": "ws1"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["saved"] == 2

        got = client.get(f"/api/v1/connections/{hap_id}/fields",
                         params={"worksheet_id": "ws1"}).json()
        by_path = {f["path"]: f for f in got["fields"]}
        assert by_path["qty"]["saved"] is True
        assert by_path["qty"]["name"] == "数量（件）"      # 字典改名生效
        assert by_path["name"]["readable"] is False       # 禁读开关生效
        assert by_path["rowid"]["saved"] is False         # 未保存项

    def test_put_rejects_system_field_writable(self, api_env):
        client, hap_id, _db_id = api_env
        resp = client.put(f"/api/v1/connections/{hap_id}/fields", json={"fields": [
            {"path": "rowid", "field_id": "rowid", "name": "记录ID",
             "readable": True, "writable": True},
        ]}, params={"worksheet_id": "ws1"})
        assert resp.status_code == 422

    def test_put_requires_scope(self, api_env):
        client, hap_id, _db_id = api_env
        assert client.put(f"/api/v1/connections/{hap_id}/fields",
                          json={"fields": []}).status_code == 422


# ============================================================
# 策略解析：effective_config（inherit 语义）
# ============================================================

def _hap_conn(conn_id, name="源"):
    return ApiConnection(id=conn_id, name=name, connection_type="source",
                         protocol="hap", auth_type="custom", auth_config={},
                         status="active")


def _mapping(source_id, target_id, **cfg):
    base = {"fields": [
        {"source": "name", "target": "name"},
        {"source": "secret", "target": "secret"},
        {"source": "qty", "target": "locked"},
    ]}
    base.update(cfg)
    return SimpleNamespace(id=uuid.uuid4(), source_connection_id=source_id,
                           target_connection_id=target_id, source_table=None,
                           target_table=None, source_endpoint="ws1",
                           target_endpoint="ws2", incremental_field=None,
                           sync_mode="full", mapping_config=base)


@pytest.fixture
def policy_env(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/fp.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    asyncio.run(engine.dispose())


class TestEffectiveConfig:

    def test_inherit_drops_unreadable_source_and_unwritable_target(self, policy_env):
        src_id, tgt_id = uuid.uuid4(), uuid.uuid4()

        async def _run():
            async with policy_env() as s:
                src, tgt = _hap_conn(src_id), _hap_conn(tgt_id, "目标")
                s.add_all([src, tgt])
                s.add_all([
                    ConnectionField(connection_id=src_id, scope="ws1", path="secret",
                                    field_id="ctrl_secret", name="秘密", readable=False,
                                    writable=False),
                    ConnectionField(connection_id=tgt_id, scope="ws2", path="locked",
                                    field_id="ctrl_locked", name="锁定", readable=True,
                                    writable=False),
                ])
                await s.commit()
                mapping = _mapping(src_id, tgt_id, field_policy="inherit")
                return await effective_config(s, mapping, src, tgt)

        cfg = asyncio.run(_run())
        assert [f["target"] for f in cfg["fields"]] == ["name"]   # 源禁读/目标禁写都剔除
        assert cfg["field_policy"] == "off"                        # 解析后冻结

    def test_inherit_refuses_to_drop_required_key(self, policy_env):
        src_id, tgt_id = uuid.uuid4(), uuid.uuid4()

        async def _run():
            async with policy_env() as s:
                src, tgt = _hap_conn(src_id), _hap_conn(tgt_id, "目标")
                s.add_all([src, tgt])
                s.add(ConnectionField(connection_id=tgt_id, scope="ws2", path="name",
                                      field_id="ctrl_name", name="名称", readable=True,
                                      writable=False))
                await s.commit()
                mapping = _mapping(src_id, tgt_id, field_policy="inherit",
                                   key_field="name")
                with pytest.raises(AdapterError) as ei:
                    await effective_config(s, mapping, src, tgt)
                return ei.value

        assert asyncio.run(_run()).code == "field_policy_required_conflict"

    def test_off_policy_returns_config_asis(self, policy_env):
        async def _run():
            async with policy_env() as s:
                mapping = _mapping(uuid.uuid4(), uuid.uuid4())   # 未声明 inherit
                return await effective_config(s, mapping)

        cfg = asyncio.run(_run())
        assert [f["target"] for f in cfg["fields"]] == ["name", "secret", "locked"]

    def test_definitions_is_scoped(self, policy_env):
        conn_id = uuid.uuid4()

        async def _run():
            async with policy_env() as s:
                s.add(_hap_conn(conn_id))
                s.add(ConnectionField(connection_id=conn_id, scope="ws1", path="a",
                                      field_id="ctrl_a", name="A"))
                s.add(ConnectionField(connection_id=conn_id, scope="ws2", path="b",
                                      field_id="ctrl_b", name="B"))
                await s.commit()
                return await definitions(s, _hap_conn(conn_id), "ws1")

        scoped = asyncio.run(_run())
        assert "ctrl_a" in scoped and "a" in scoped
        assert "ctrl_b" not in scoped                # 其它 scope 不串
