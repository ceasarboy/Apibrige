"""端到端冒烟：外部 agent（CLI）从裸 URL 走到清单、批准、真实同步（task 6.17 / 8.1 / 8.2 / 8.3）

前置：临时 mock 源/目标系统 + 真实 APIBridge 服务（uvicorn + sqlite + 本机 Redis）。

场景:
    A/B. 无模型：CLI probe → infer → draft(退化) → verify L2 → protocol apply
         → 人批准(HTTP) → 跑同步成功（全程无 LLM 参与）
    C.   认证桥：源带 Bearer Token（mock 校验），凭据解密回归（task 1.1）

用法: python scripts/smoke_agent_flow.py
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
SERVER = "http://127.0.0.1:18080"
TOKEN = "smoke-admin-token"
CLI = [sys.executable, str(ROOT / "app" / "probe" / "cli.py")]


def http(method: str, url: str, body: dict | None = None,
         token: str = TOKEN) -> tuple[int, dict]:
    req = urllib.request.Request(url, method=method)
    req.add_header("X-Admin-Token", token)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode() or "{}"
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:400]}


def cli(*args: str, expect: int = 0) -> dict:
    env = dict(os.environ)
    env["APIBRIDGE_SERVER"] = SERVER
    env["APIBRIDGE_ADMIN_TOKEN"] = TOKEN
    proc = subprocess.run(CLI + list(args), capture_output=True, text=True, env=env)
    if proc.returncode != expect or os.environ.get("SMOKE_DEBUG"):
        print("[cli]", args, "exit=", proc.returncode)
        print("stdout=", proc.stdout)
        print("stderr=", proc.stderr[:300])
    out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    assert proc.returncode == expect, \
        f"CLI {args} exit={proc.returncode} (期望 {expect}): {proc.stdout} {proc.stderr}"
    return out


def wait_ready(url: str, timeout: float = 40.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"等待超时: {url}")


def start_server(db_file: str, encrypt_key: str = "", stderr_path: str | None = None):
    env = dict(os.environ)
    env.update({
        "APIBRIDGE_DATABASE_URL": f"sqlite+aiosqlite:///./{db_file}",
        "APIBRIDGE_ADMIN_TOKEN": TOKEN,
        "APIBRIDGE_ENCRYPT_KEY": encrypt_key,
    })
    stderr_handle = open(stderr_path, "w", encoding="utf-8") if stderr_path \
        else subprocess.DEVNULL
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", "18080", "--log-level", "info"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=stderr_handle)
    wait_ready(f"{SERVER}/health")
    return proc


def scenario(db_file: str, require_token: bool, encrypt_key: str = "") -> dict:
    mock_port = 18082 if require_token else 18081
    mock = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "mock_target.py"),
         "--port", str(mock_port),
         "--require-token", "src-secret" if require_token else ""],
        cwd=ROOT, stdout=subprocess.DEVNULL)
    wait_ready(f"http://127.0.0.1:{mock_port}/health")
    server = None
    try:
        server = start_server(db_file, encrypt_key)
        src_url = f"http://127.0.0.1:{mock_port}"
        token_args = ["--source-token", "src-secret"] if require_token else []

        # ---- agent 从裸 URL 走到清单（6.17 / 8.1 / 8.2）----
        out = cli("probe", "--source-url", src_url, "--source-endpoint", "/api/users",
                  "--target-url", src_url, "--target-endpoint", "/api/customers",
                  *token_args)
        session_id = out["data"]["session_id"]
        assert out["data"]["status"] == "L1_reachable", out

        out = cli("infer", "--session", session_id)
        paths = [f["path"] for f in out["data"]["fields"]]
        assert "user_name" in paths, out

        if require_token:
            # 场景 C：显式映射 + 目标契约
            out = cli("draft", "--session", session_id,
                      "--fields", json.dumps([
                          {"source": "user_name", "target": "customer_name",
                           "transform": "string"},
                          {"source": "age", "target": "age", "transform": "int"}]),
                      "--target-contract-json", json.dumps([
                          {"name": "customer_name", "type": "string"},
                          {"name": "age", "type": "integer", "required": False}]))
        else:
            # 场景 A/B：无模型退化——声明与源同名的目标契约，退化草案即可命中
            contract = [{"name": "user_name", "type": "string"},
                        {"name": "age", "type": "integer"}]
            out = cli("draft", "--session", session_id,
                      "--target-contract-json", json.dumps(contract))
        assert out["ok"] is True, out
        assert out["data"]["fields"], "退化草案不应为空"

        out = cli("verify", "--session", session_id, "--level", "L2")
        assert out["data"]["valid"] is True, out

        out = cli("protocol", "apply", "--session", session_id)
        assert out["data"]["status"] == "pending_review", out

        out = cli("review", "list")
        assert any(i["session_id"] == session_id for i in out["data"]["items"]), out

        # ---- 人批准（管理界面等价 HTTP 调用）----
        def _conn_cfg(auth: bool) -> dict:
            cfg = {"protocol": "rest", "base_url": src_url}
            if auth:
                cfg.update({"auth_type": "bearer",
                            "auth_config": {"token": "src-secret"}})
            return cfg

        status, approved = http("POST", f"{SERVER}/api/v1/probes/{session_id}/approve", {
            "decided_by": "工程师-王", "via": "ui", "sync_mode": "sync",
            "source_connection": _conn_cfg(require_token),
            "target_connection": _conn_cfg(require_token),
        })
        assert status == 200, approved
        assert approved["status"] == "committed"
        task_id = approved["bridge_ids"]["task_id"]

        # ---- 真跑一次同步 ----
        status, run_result = http("POST", f"{SERVER}/api/v1/tasks/{task_id}/run")
        assert status in (200, 201), run_result
        time.sleep(1)  # 等同步完成

        # 目标系统收到数据（按 customer_name / user_name 键回读）
        read_key = "customer_name" if require_token else "user_name"
        req = urllib.request.Request(
            f"http://127.0.0.1:{mock_port}/api/customers/{urllib.parse.quote('张三')}")
        if require_token:
            req.add_header("Authorization", "Bearer src-secret")
        with urllib.request.urlopen(req) as resp:
            got = json.loads(resp.read().decode())
        if got.get(read_key) != "张三":
            status, run_detail = http("GET", f"{SERVER}/api/v1/tasks/{task_id}")
            status2, log_dump = http("GET", f"{SERVER}/api/v1/logs?task_id={task_id}")
            print("目标未收到数据: got=", got)
            print("run_task resp=", json.dumps(run_result, ensure_ascii=False, default=str))
            print("task=", json.dumps(run_detail, ensure_ascii=False, default=str)[:600])
            print("logs=", json.dumps(log_dump, ensure_ascii=False, default=str)[:1000])
            raise AssertionError("target missing data")
        assert got.get(read_key) == "张三", got

        # 执行日志全链路可查
        status, logs = http("GET", f"{SERVER}/api/v1/logs?task_id={task_id}")
        assert status == 200, logs

        return {"scenario": "C(认证桥)" if require_token else "A/B(无模型退化)",
                "session": session_id, "task": str(task_id),
                "target_received": got, "log_entries": len(logs)}
    finally:
        mock.terminate()
        if server:
            server.terminate()


def main() -> int:
    results = []
    print("== 场景 A/B：无模型退化草案 ==")
    results.append(scenario("data/smoke_a.db", require_token=False))
    print("== 场景 C：认证桥（Bearer）==")
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    results.append(scenario("data/smoke_c.db", require_token=True, encrypt_key=key))
    print(json.dumps(results, ensure_ascii=False, indent=1))
    print("SMOKE ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
