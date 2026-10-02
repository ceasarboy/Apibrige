"""压轴端到端：探针全链路 × 真实 Oracle 11g（task：探针+DB连接器合流）

流程（对应真实使用剧本）：
    1. 建会话：源=REST mock，目标=真实 Oracle（内联 db_config，凭证不落库）
    2. probe → L1_reachable（源 HTTP + Oracle 双侧可达）
    3. infer → 源 Schema 推断 + 目标 user_tab_columns 真实表结构
    4. draft → 提交字段对应关系（大小写互认：user_name→CUSTOMER_NAME）
    5. verify L2 → 真实转换 + 与 Oracle 表结构契约比对，零写入（行数不变断言）
    6. submit → 待确认清单
    7. authorize-e2e → verify L3 → 向 Oracle 写 1 条 + 回读比对（行数 +1 断言）
    8. approve → 单事务建桥（目标连接带 Oracle 凭据）
    9. run task → 真实同步 REST→Oracle → SELECT 核验数据落库

前置：Oracle 测试容器就绪（1521/XE system/oracle），Instant Client 19 就位。
用法: python scripts/smoke_oracle_probe.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.smoke_agent_flow import (  # noqa: E402
    SERVER, TOKEN, http, start_server, wait_ready,
)

import oracledb  # noqa: E402

ORA = {"host": "127.0.0.1", "port": 1521, "sid": "XE",
       "username": "system", "password": "oracle"}
LIB_DIR = os.environ.get("APIBRIDGE_ORACLE_CLIENT_LIB_DIR",
                         r"E:\tools\instantclient\instantclient_19_25")
TABLE = "PROBE_TARGET"


def ora_connect():
    return oracledb.connect(user=ORA["username"], password=ORA["password"],
                            dsn=f"{ORA['host']}:{ORA['port']}/{ORA['sid']}")


def ora_exec(sql, binds=None):
    conn = ora_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, binds or [])
        conn.commit()
    finally:
        conn.close()


def ora_fetch(sql, binds=None) -> list[dict]:
    conn = ora_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, binds or [])
            cols = [d[0].lower() for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def row_count() -> int:
    return ora_fetch(f"SELECT COUNT(*) AS n FROM {TABLE}")[0]["n"]


def setup_table():
    oracledb.init_oracle_client(lib_dir=LIB_DIR)
    ora_exec(f"BEGIN EXECUTE IMMEDIATE 'DROP TABLE {TABLE}'; "
             "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -942 THEN RAISE; END IF; END;")
    ora_exec(f"CREATE TABLE {TABLE} ("
             "CUSTOMER_NAME VARCHAR2(100) PRIMARY KEY, AGE NUMBER(10))")
    assert row_count() == 0


def wait_oracle_ready(timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ora_fetch("SELECT 1 AS ok FROM DUAL")
            return True
        except Exception:
            time.sleep(5)
    return False


def cli(*args: str, expect: int = 0) -> dict:
    env = dict(os.environ)
    env["APIBRIDGE_SERVER"] = SERVER
    env["APIBRIDGE_ADMIN_TOKEN"] = TOKEN
    proc = subprocess.run([sys.executable, str(ROOT / "app" / "probe" / "cli.py")] +
                          list(args), capture_output=True, text=True, env=env)
    out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    assert proc.returncode == expect, \
        f"CLI {args} exit={proc.returncode}: {proc.stdout[:400]} {proc.stderr[:200]}"
    return out


def main() -> int:
    # 0. 环境准备
    print("[0] Instant Client + Oracle 就绪检查…")
    oracledb.init_oracle_client(lib_dir=LIB_DIR)
    mock = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "mock_target.py"), "--port", "18081"],
        cwd=ROOT, stdout=subprocess.DEVNULL)
    wait_ready("http://127.0.0.1:18081/health")
    server = None
    try:
        setup_table()

        env = dict(os.environ)
        env.update({
            "APIBRIDGE_DATABASE_URL": "sqlite+aiosqlite:///./data/smoke_oraprobe.db",
            "APIBRIDGE_ADMIN_TOKEN": TOKEN,
            "APIBRIDGE_ORACLE_CLIENT_MODE": "thick",
            "APIBRIDGE_ORACLE_CLIENT_LIB_DIR": LIB_DIR,
        })
        err_f = open("data/oraprobe_server_err.log", "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", "18080", "--log-level", "info"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=err_f)
        wait_ready(f"{SERVER}/health")

        def post(path, body=None, extra=None):
            headers = {"X-Admin-Token": TOKEN}
            if body is not None:
                headers["Content-Type"] = "application/json"
            if extra:
                headers.update(extra)
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(f"{SERVER}{path}", data=data,
                                         headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode())

        def get(path):
            req = urllib.request.Request(f"{SERVER}{path}",
                                         headers={"X-Admin-Token": TOKEN})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())

        # 1. 建会话（源 REST mock；目标真实 Oracle 内联配置）
        print("[1] 建探针会话…")
        session = post("/api/v1/probes", {
            "source": {"protocol": "rest", "base_url": "http://127.0.0.1:18081",
                       "endpoint": "/api/users"},
            "target": {"db_type": "oracle", "protocol": "oracle",
                       "db_config": {"host": "127.0.0.1", "port": 1521,
                                     "sid": "XE", "username": "system",
                                     "password": "oracle"},
                       "table": TABLE},
            "via": "cli"})
        sid = session["id"]
        assert session["source_snapshot"]["auth_config"] is None  # 无凭证场景

        # 2. L1
        print("[2] L1 探测…")
        s = post(f"/api/v1/probes/{sid}/probe", {}, {"via": "cli"})
        assert s["status"] == "L1_reachable", s
        assert s["l1_result"]["target"]["status"] == "ok"

        # 3. infer（目标 Schema 来自真实 user_tab_columns）
        print("[3] Schema 推断…")
        inf = post(f"/api/v1/probes/{sid}/infer", {}, {"via": "cli"})
        src_paths = [f["path"] for f in inf["source_schema"]["fields"]]
        assert "user_name" in src_paths, inf
        tgt_names = [f["name"] for f in inf["target_schema"]["fields"]]
        assert "CUSTOMER_NAME" in tgt_names and "AGE" in tgt_names, inf

        # 4. draft（大写目标 + int 转换）
        print("[4] 映射草案…")
        draft = post(f"/api/v1/probes/{sid}/draft", {
            "fields": [
                {"source": "user_name", "target": "CUSTOMER_NAME",
                 "transform": "string"},
                {"source": "age", "target": "AGE", "transform": "int"}],
            "via": "cli"})
        assert draft["valid"] is True, draft

        # 5. L2 零写入
        print("[5] L2 验证（零写入断言）…")
        before = row_count()
        s = post(f"/api/v1/probes/{sid}/verify", {"level": "L2", "via": "cli"})
        assert s["status"] == "transform_verified", s
        assert s["l2_result"]["valid"] is True, s["l2_result"]
        assert row_count() == before, "L2 违规写入了目标！"

        # 6. 提交清单
        print("[6] 提交待确认清单…")
        s = post(f"/api/v1/probes/{sid}/submit", {"via": "cli"})
        assert s["status"] == "pending_review", s

        # 7. L3 授权 + 验证（真写 1 条）
        print("[7] L3 授权 + 端到端验证…")
        s = post(f"/api/v1/probes/{sid}/authorize-e2e",
                 {"authorized_by": "工程师-王", "via": "ui"})
        assert s["e2e_authorized"] is True
        before = row_count()
        s = post(f"/api/v1/probes/{sid}/verify", {"level": "L3", "via": "cli"})
        assert s["l3_result"]["level"] == "L3" and s["l3_result"]["valid"], s["l3_result"]
        assert row_count() == before + 1, "L3 应恰好写入 1 条"

        # 8. 批准建桥（目标连接由批准人提供 Oracle 凭据）
        print("[8] 批准建桥…")
        status, approved = http("POST", f"{SERVER}/api/v1/probes/{sid}/approve", {
            "decided_by": "工程师-王", "via": "ui", "sync_mode": "sync",
            "source_connection": {"protocol": "rest",
                                  "base_url": "http://127.0.0.1:18081"},
            "target_connection": {"protocol": "oracle", "db_type": "oracle",
                                  "db_config": {"host": "127.0.0.1", "port": 1521,
                                                "sid": "XE", "username": "system",
                                                "password": "oracle"}},
        })
        assert status == 200, approved
        assert approved["status"] == "committed"
        task_id = approved["bridge_ids"]["task_id"]

        # 清掉 L3 探针数据，让同步从零核验
        ora_exec(f"DELETE FROM {TABLE}")
        assert row_count() == 0

        # 9. 真实同步 REST → Oracle（mock 源数据：张三/25）
        print("[9] 运行同步任务（REST → Oracle）…")
        status, run_result = http("POST", f"{SERVER}/api/v1/tasks/{task_id}/run")
        assert status in (200, 201), run_result
        time.sleep(1)
        rows = ora_fetch(f"SELECT customer_name, age FROM {TABLE} ORDER BY customer_name")
        assert rows == [{"customer_name": "张三", "age": 25}], rows

        print(json.dumps({
            "scenario": "oracle-probe-e2e",
            "session": sid, "task": task_id,
            "oracle_rows": rows,
            "verdict": "L1+L2(零写入)+L3(写1读1)+真实同步 全部通过",
        }, ensure_ascii=False, indent=1))
        print("ORACLE PROBE E2E ALL PASS")
        return 0
    finally:
        mock.terminate()
        if server:
            server.terminate()


if __name__ == "__main__":
    sys.exit(main())
