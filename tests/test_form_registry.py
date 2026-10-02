# -*- coding: utf-8 -*-
"""表单注册表（ConnectionForm）测试：合并规则 + 桥引用统计 + API

覆盖 openspec/changes/add-form-registry：
- merge_forms：新 key 插入（display_name=源端名）、已存在仅刷新 original_name
  （用户命名/备注保留）、源端消失项保留、{added, refreshed, total} 统计
- attach_bridge_refs：HAP 目标按 protocol.worksheet_id、DB 按 source/target_table、
  大小写互认、源/目标双向、peer 信息、无引用为 0
- API：GET 列表/筛选、POST sync（DB mock / HAP mock / REST 400）、
  手动登记 + 409、PUT 仅改名称备注、DELETE 不碰映射
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.database import get_session
from app.models.sync import (
    Base, ApiConnection, ConnectionForm, FieldMapping,
)
from app.services.form_registry import (
    _mapping_form_refs, _ci_equal, FormSyncUnsupportedError, fetch_source_forms,
)
from tests.conftest import TEST_ADMIN_TOKEN

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

# ============================================================
# Part A：纯函数（引用口径 + 大小写互认）
# ============================================================


class TestFormRefHelpers:

    def test_ci_equal(self):
        assert _ci_equal("MTL_MATERIAL", "mtl_material")
        assert _ci_equal(" x ", "x")
        assert not _ci_equal("a", "b")

    def test_refs_db_source_and_target(self):
        """DB 映射：源用 source_table，目标用 target_table"""
        m = FieldMapping(
            name="db2db", source_connection_id=uuid4(), target_connection_id=uuid4(),
            source_table="SRC_T", target_table="TGT_T", mapping_config={})
        assert _mapping_form_refs(m) == [("source", "SRC_T"), ("target", "TGT_T")]

    def test_refs_hap_target_prefers_protocol_worksheet_id(self):
        """HAP 目标：protocol.worksheet_id 优先于 target_endpoint/target_table"""
        m = FieldMapping(
            name="db2hap", source_connection_id=uuid4(), target_connection_id=uuid4(),
            source_table="mtl", target_table="legacy", target_endpoint="/legacy",
            mapping_config={"protocol": {"worksheet_id": "ws123"}})
        assert _mapping_form_refs(m) == [("source", "mtl"), ("target", "ws123")]

    def test_refs_hap_source_uses_endpoint(self):
        """HAP 源：用 source_endpoint（ws id）"""
        m = FieldMapping(
            name="hap2db", source_connection_id=uuid4(), target_connection_id=uuid4(),
            source_endpoint="ws456", target_table="T",
            mapping_config={"protocol": {}})
        assert _mapping_form_refs(m) == [("source", "ws456"), ("target", "T")]

    def test_refs_empty(self):
        m = FieldMapping(
            name="x", source_connection_id=uuid4(), target_connection_id=uuid4(),
            mapping_config={})
        assert _mapping_form_refs(m) == []


# ============================================================
# 测试基建：真实文件 SQLite + TestClient
# ============================================================


@pytest.fixture
def fr_api_env(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/fr.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_session():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    client = TestClient(app, headers={"X-Admin-Token": TEST_ADMIN_TOKEN})
    yield client, maker
    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


def _U(id_str: str):
    from uuid import UUID as _UUID
    return _UUID(id_str)


def _insert_conn(maker, **kw) -> str:
    """插入连接行，返回 id 字符串"""
    defaults = dict(name="连接", connection_type="source", protocol="rest")
    defaults.update(kw)

    async def _run():
        async with maker() as session:
            conn = ApiConnection(**defaults)
            session.add(conn)
            await session.commit()
            return str(conn.id)

    return asyncio.run(_run())


def _insert_mapping(maker, **kw) -> str:
    defaults = dict(name="桥", source_connection_id=uuid4(),
                    target_connection_id=uuid4(), mapping_config={})
    defaults.update(kw)

    async def _run():
        async with maker() as session:
            m = FieldMapping(**defaults)
            session.add(m)
            await session.commit()
            return str(m.id)

    return asyncio.run(_run())


def _mock_db_connector(tables: list[str]):
    conn = MagicMock()
    conn.get_table_list = AsyncMock(return_value=tables)
    conn.disconnect = AsyncMock()          # 取数路径在 finally 释放连接
    return conn


# ============================================================
# Part B：API — sync 拉取合并（DB / HAP / REST）
# ============================================================


class TestFormSyncAPI:

    def test_sync_db_merge_rules(self, fr_api_env):
        """DB 拉取合并：新增/刷新原始名/保留用户命名/消失项保留"""
        client, maker = fr_api_env
        conn_id = _insert_conn(maker, protocol="mysql", db_type="mysql",
                               db_config={"host": "h", "database": "d"})
        # 预置：A（用户已改名）、D（源端将消失）
        _seed_forms(maker, conn_id, [
            {"form_key": "A", "original_name": "A旧名", "display_name": "我的A",
             "remark": "备注A", "source": "auto"},
            {"form_key": "D", "original_name": "D", "display_name": "D", "remark": "",
             "source": "auto"},
        ])
        with patch("app.connectors.database.get_database_connector",
                   return_value=_mock_db_connector(["A", "B", "C"])):
            resp = client.post(f"/api/v1/forms/{conn_id}/sync")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"added": 2, "refreshed": 1, "total": 4}

        rows = _load_forms(maker, conn_id)
        by_key = {r["form_key"]: r for r in rows}
        assert set(by_key) == {"A", "B", "C", "D"}  # D 消失仍保留
        assert by_key["A"]["display_name"] == "我的A"  # 用户命名不被覆盖
        assert by_key["A"]["remark"] == "备注A"
        assert by_key["A"]["original_name"] == "A"  # 原始名刷新为源端名
        assert by_key["B"]["display_name"] == "B"  # 新增默认 display_name=源端名
        assert by_key["B"]["source"] == "auto"

        # 再拉一次：无变化
        with patch("app.connectors.database.get_database_connector",
                   return_value=_mock_db_connector(["A", "B", "C"])):
            resp2 = client.post(f"/api/v1/forms/{conn_id}/sync")
        assert resp2.json() == {"added": 0, "refreshed": 3, "total": 4}

    def test_sync_hap_via_app_get(self, fr_api_env):
        """HAP 拉取：走适配器 list_worksheets（app/get），登记 ws id 与名称"""
        client, maker = fr_api_env
        conn_id = _insert_conn(maker, protocol="hap", connection_type="source",
                               base_url="https://api.mingdao.com",
                               auth_type="custom", auth_config=None)
        ws_list = [
            {"form_key": "6aa32f0cf46d7b7148ac6f29", "original_name": "MES物料同步",
             "remark": "开发备注"},
            {"form_key": "6a85006cda2a332216c6f288", "original_name": "物料数据",
             "remark": ""},
        ]
        with patch("app.adapters.hap.HapV3Adapter.list_worksheets",
                   new=AsyncMock(return_value=ws_list)):
            resp = client.post(f"/api/v1/forms/{conn_id}/sync")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"added": 2, "refreshed": 0, "total": 2}
        rows = _load_forms(maker, conn_id)
        by_key = {r["form_key"]: r for r in rows}
        assert by_key["6aa32f0cf46d7b7148ac6f29"]["remark"] == "开发备注"
        assert by_key["6aa32f0cf46d7b7148ac6f29"]["original_name"] == "MES物料同步"

    def test_sync_rest_unsupported(self, fr_api_env):
        """REST 连接不支持自动拉取 → 400 提示手动登记"""
        client, maker = fr_api_env
        conn_id = _insert_conn(maker, protocol="rest")
        resp = client.post(f"/api/v1/forms/{conn_id}/sync")
        assert resp.status_code == 400
        assert "手动登记" in resp.json()["detail"]

    def test_sync_connection_not_found(self, fr_api_env):
        client, _ = fr_api_env
        resp = client.post(f"/api/v1/forms/{uuid4()}/sync")
        assert resp.status_code == 404

    def test_fetch_source_forms_dispatch(self, fr_api_env):
        """fetch_source_forms 分发：无 db_type 且非 hap → FormSyncUnsupportedError"""
        conn = ApiConnection(name="r", connection_type="source", protocol="soap")
        with pytest.raises(FormSyncUnsupportedError):
            asyncio.run(fetch_source_forms(conn))


# ============================================================
# Part C：API — 手动登记 / 编辑 / 删除 / 列表与引用统计
# ============================================================


class TestFormCRUDAPI:

    def test_manual_create_and_duplicate_409(self, fr_api_env):
        client, maker = fr_api_env
        conn_id = _insert_conn(maker, protocol="rest")
        resp = client.post("/api/v1/forms", json={
            "connection_id": conn_id, "form_key": "/material/listPage",
            "display_name": "MES 物料列表", "remark": "物料主数据分页接口"})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["source"] == "manual"
        assert body["display_name"] == "MES 物料列表"
        assert body["bridge_count"] == 0

        # 重复登记（大小写不同也算重复）→ 409
        resp2 = client.post("/api/v1/forms", json={
            "connection_id": conn_id, "form_key": "/MATERIAL/ListPage",
            "display_name": "x"})
        assert resp2.status_code == 409

    def test_update_only_display_and_remark(self, fr_api_env):
        """编辑只改显示名/备注；原始名与来源不变"""
        client, maker = fr_api_env
        conn_id = _insert_conn(maker, protocol="mysql", db_type="mysql")
        form_id = _seed_forms(maker, conn_id, [
            {"form_key": "MTL", "original_name": "MTL", "display_name": "MTL",
             "remark": "", "source": "auto"}])[0]
        resp = client.put(f"/api/v1/forms/{form_id}",
                          json={"display_name": "物料表", "remark": "MES 物料"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["display_name"] == "物料表"
        assert body["remark"] == "MES 物料"
        assert body["original_name"] == "MTL"
        assert body["source"] == "auto"

    def test_delete_keeps_mappings(self, fr_api_env):
        """删除注册记录不影响已建映射"""
        client, maker = fr_api_env
        conn_id = _insert_conn(maker, protocol="hap")
        form_id = _seed_forms(maker, conn_id, [
            {"form_key": "ws1", "original_name": "表1", "display_name": "表1",
             "remark": "", "source": "auto"}])[0]
        mapping_id = _insert_mapping(maker, source_endpoint="ws1")
        resp = client.delete(f"/api/v1/forms/{form_id}")
        assert resp.status_code == 200
        # 映射仍在
        got = client.get(f"/api/v1/mappings/{mapping_id}")
        assert got.status_code == 200

    def test_list_with_bridge_refs(self, fr_api_env):
        """列表含引用统计：HAP 目标 worksheet_id 匹配 + peer 信息 + 大小写互认"""
        client, maker = fr_api_env
        src_conn = _insert_conn(maker, name="MES库", protocol="rest")
        hap_conn = _insert_conn(maker, name="明道云", protocol="hap")
        # DB 源表（大写）× 映射 source_table（小写）：大小写互认
        _insert_mapping(maker, name="物料桥",
                        source_connection_id=_U(src_conn),
                        target_connection_id=_U(hap_conn),
                        source_table="MTL_MATERIAL",
                        mapping_config={"protocol": {"worksheet_id": "ws9"}})
        # HAP 目标桥
        _insert_mapping(maker, name="MES→HAP",
                        source_connection_id=_U(src_conn),
                        target_connection_id=_U(hap_conn),
                        source_endpoint="/material/listPage",
                        mapping_config={"protocol": {"worksheet_id": "ws9"}})
        # ws9 被两条桥用作目标
        _seed_forms(maker, hap_conn, [
            {"form_key": "ws9", "original_name": "物料数据", "display_name": "物料数据",
             "remark": "", "source": "auto"},
            {"form_key": "ws_unused", "original_name": "未用表",
             "display_name": "未用表", "remark": "", "source": "auto"},
        ])

        resp = client.get(f"/api/v1/forms?connection_id={hap_conn}")
        assert resp.status_code == 200
        rows = {r["form_key"]: r for r in resp.json()}
        assert rows["ws9"]["bridge_count"] == 2
        sides = sorted(b["side"] for b in rows["ws9"]["bridges"])
        assert sides == ["target", "target"]
        assert all(b["peer_form"] for b in rows["ws9"]["bridges"])
        assert rows["ws_unused"]["bridge_count"] == 0

        # 全量列表不过滤
        resp_all = client.get("/api/v1/forms")
        assert len(resp_all.json()) == 2
        names = {r["connection_name"] for r in resp_all.json()}
        assert names == {"明道云"}

    def test_case_insensitive_bridge_ref(self, fr_api_env):
        """Oracle 大写表名 × 映射小写 source_table：计数不漏"""
        client, maker = fr_api_env
        ora_conn = _insert_conn(maker, name="Oracle", protocol="oracle",
                                db_type="oracle")
        _seed_forms(maker, ora_conn, [
            {"form_key": "MTL_MATERIAL", "original_name": "MTL_MATERIAL",
             "display_name": "物料表", "remark": "", "source": "auto"}])
        _insert_mapping(maker, source_connection_id=_U(ora_conn),
                        source_table="mtl_material")
        resp = client.get(f"/api/v1/forms?connection_id={ora_conn}")
        rows = resp.json()
        assert rows[0]["bridge_count"] == 1
        assert rows[0]["bridges"][0]["side"] == "source"


# ============================================================
# 辅助
# ============================================================


def _seed_forms(maker, conn_id: str, specs: list[dict]) -> list[str]:
    """直接落库表单行，返回 id 列表"""
    from datetime import datetime, timezone
    from uuid import UUID as _UUID
    cid = _UUID(conn_id)

    async def _run():
        async with maker() as session:
            forms = []
            for s in specs:
                f = ConnectionForm(
                    connection_id=cid, form_key=s["form_key"],
                    original_name=s.get("original_name"),
                    display_name=s.get("display_name"),
                    remark=s.get("remark", ""), source=s.get("source", "auto"),
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc))
                session.add(f)
                forms.append(f)
            await session.commit()
            return [str(f.id) for f in forms]

    return asyncio.run(_run())


def _load_forms(maker, conn_id: str) -> list[dict]:
    from sqlalchemy import select
    from uuid import UUID as _UUID

    async def _run():
        async with maker() as session:
            rows = (await session.execute(
                select(ConnectionForm).where(
                    ConnectionForm.connection_id == _UUID(conn_id))
            )).scalars().all()
            return [{"form_key": r.form_key, "original_name": r.original_name,
                     "display_name": r.display_name, "remark": r.remark,
                     "source": r.source} for r in rows]

    return asyncio.run(_run())
