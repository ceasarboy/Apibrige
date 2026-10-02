"""运行时特性端到端冒烟（task 2.2/2.3/2.4 的真实链路验证）

复用 mock 目标系统 + 真实 APIBridge 服务（uvicorn + sqlite + 本机 Redis）：
    1. Webhook：带 secret 的任务接收外部推送 → 同步到目标（含错误 secret 401）
    2. 增量同步：sqlite 源两次运行——第二次只搬运新增行，last_sync_value 推进
    3. Cron：scheduled 任务每分钟调度，等待调度器派发后验证目标收到数据

用法: python scripts/smoke_runtime_features.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.smoke_agent_flow import (  # noqa: E402
    SERVER, TOKEN, http, start_server, wait_ready,
)


def cli(*args: str, expect: int = 0) -> dict:
    env = dict(os.environ)
    env["APIBRIDGE_SERVER"] = SERVER
    env["APIBRIDGE_ADMIN_TOKEN"] = TOKEN
    proc = subprocess.run([sys.executable, str(ROOT / "app" / "probe" / "cli.py")] +
                          list(args), capture_output=True, text=True, env=env)
    out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    assert proc.returncode == expect, \
        f"CLI {args} exit={proc.returncode}: {proc.stdout} {proc.stderr}"
    return out


def make_sqlite_source(path: str, rows: list[tuple]):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL)")
    conn.executemany("INSERT INTO orders VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def create_bridge(base_url: str, src_url: str, mapping_config: dict,
                  incremental: bool = False, sqlite_db: str = None,
                  src_table: str = None, source_endpoint: str = None,
                  target_endpoint: str = "/api/customers"):
    """建 连接×2 + 映射，返回 (source_conn_id, target_conn_id, mapping_id)"""
    _, src = http("POST", f"{SERVER}/api/v1/connections", {
        "name": "src", "connection_type": "source", "protocol": "rest",
        "base_url": src_url, "db_type": "sqlite" if sqlite_db else None,
        "db_config": {"database": sqlite_db} if sqlite_db else None,
    })
    _, tgt = http("POST", f"{SERVER}/api/v1/connections", {
        "name": "tgt", "connection_type": "target", "protocol": "rest",
        "base_url": src_url,
    })
    _, mapping = http("POST", f"{SERVER}/api/v1/mappings", {
        "name": "smoke-mapping",
        "source_connection_id": src["id"], "target_connection_id": tgt["id"],
        "source_endpoint": source_endpoint,
        "source_table": src_table, "target_endpoint": target_endpoint,
        "mapping_config": mapping_config,
        "sync_mode": "incremental" if incremental else "full",
        "incremental_field": "id" if incremental else None,
    })
    return src["id"], tgt["id"], mapping["id"]


def create_task(mapping_id: str, task_type: str, sync_mode: str,
                cron: str = None, webhook_secret: str = None) -> str:
    _, task = http("POST", f"{SERVER}/api/v1/tasks", {
        "mapping_id": mapping_id, "task_type": task_type,
        "schedule_cron": cron, "sync_mode": sync_mode,
        "webhook_config": {"secret": webhook_secret} if webhook_secret else None,
    })
    return task["id"]


def mock_get(mock_port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{mock_port}{path}", timeout=10) as r:
        return json.loads(r.read().decode())


def wait_for(condition, timeout=150, interval=3, desc=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if condition():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def scenario_webhook(mock_port: int = 18081) -> dict:
    """Webhook：带 secret 的任务接收外部推送"""
    src_url = f"http://127.0.0.1:{mock_port}"
    payload_fields = [{"source": "order_id", "target": "customer_name",
                       "transform": "string"}]
    _, _, mapping_id = create_bridge(
        SERVER, src_url,
        mapping_config={"fields": payload_fields},
        target_endpoint="/api/customers")
    task_id = create_task(mapping_id, task_type="realtime", sync_mode="sync",
                          webhook_secret="wh-s3cret")

    # 错误 secret → 401
    req = urllib.request.Request(
        f"{SERVER}/api/v1/webhook/{task_id}",
        data=json.dumps({"order_id": "bad"}).encode(), method="POST")
    req.add_header("X-Admin-Token", TOKEN)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Webhook-Secret", "wrong")
    try:
        urllib.request.urlopen(req)
        raise AssertionError("错误 secret 未被拒绝")
    except urllib.error.HTTPError as e:
        assert e.code == 401, e.code

    # 正确 secret → 同步 → 目标收到
    req = urllib.request.Request(
        f"{SERVER}/api/v1/webhook/{task_id}",
        data=json.dumps({"order_id": "wh-1001"}).encode(), method="POST")
    req.add_header("X-Admin-Token", TOKEN)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Webhook-Secret", "wh-s3cret")
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode())
    assert result["status"] == "success", result

    got = mock_get(mock_port, f"/api/customers/wh-1001")
    assert got.get("customer_name") == "wh-1001", got
    return {"scenario": "webhook", "task": task_id, "received": got}


def scenario_incremental(mock_port: int) -> dict:
    """增量同步：两次运行，第二次只搬新增行"""
    src_url = f"http://127.0.0.1:{mock_port}"
    db_file = "data/smoke_inc.db"
    make_sqlite_source(str(ROOT / db_file), [(i, float(i) * 10) for i in range(1, 6)])

    mapping_config = {"fields": [
        {"source": "id", "target": "customer_name", "transform": "string"},
        {"source": "amount", "target": "amount", "transform": "float"},
    ]}
    _, _, mapping_id = create_bridge(
        SERVER, src_url, mapping_config=mapping_config,
        sqlite_db=db_file, src_table="orders", incremental=True)

    task_id = create_task(mapping_id, task_type="realtime", sync_mode="sync")

    # 第一次运行：搬 1..5
    status, run1 = http("POST", f"{SERVER}/api/v1/tasks/{task_id}/run")
    assert status in (200, 201), run1
    time.sleep(1)
    if mock_get(mock_port, "/api/customers/5").get("customer_name") != "5":
        _, logs = http("GET", f"{SERVER}/api/v1/logs?task_id={task_id}")
        print("run1 resp:", json.dumps(run1, ensure_ascii=False, default=str)[:400])
        print("logs:", json.dumps(logs, ensure_ascii=False, default=str)[:800])
        raise AssertionError("第一轮未搬数据")
    assert mock_get(mock_port, "/api/customers/5").get("customer_name") == "5"

    # 源追加 6..7，第二次运行只搬新增
    conn = sqlite3.connect(str(ROOT / db_file))
    conn.executemany("INSERT INTO orders VALUES (?, ?)", [(6, 60.0), (7, 70.0)])
    conn.commit()
    conn.close()
    status, _ = http("POST", f"{SERVER}/api/v1/tasks/{task_id}/run")
    assert status in (200, 201)
    time.sleep(1)
    assert mock_get(mock_port, "/api/customers/7").get("customer_name") == "7"

    # last_sync_value 推进到 7
    _, task_detail = http("GET", f"{SERVER}/api/v1/tasks/{task_id}")
    assert task_detail["last_sync_value"] == "7"
    return {"scenario": "incremental", "task": task_id, "last_sync_value": "7"}


def scenario_cron(mock_port: int) -> dict:
    """Cron：scheduled 任务每分钟调度，等待派发后目标收到数据"""
    src_url = f"http://127.0.0.1:{mock_port}"
    mapping_config = {"fields": [
        {"source": "user_name", "target": "customer_name", "transform": "string"},
    ]}
    _, _, mapping_id = create_bridge(
        SERVER, src_url, mapping_config=mapping_config,
        source_endpoint="/api/users", target_endpoint="/api/customers")
    task_id = create_task(mapping_id, task_type="scheduled",
                          sync_mode="sync", cron="* * * * *")

    def received():
        got = mock_get(mock_port,
                       "/api/customers/" + urllib.parse.quote("张三"))
        return got.get("customer_name") == "张三"

    if not wait_for(received, timeout=150, desc="cron 派发"):
        _, task_detail = http("GET", f"{SERVER}/api/v1/tasks/{task_id}")
        _, log_dump = http("GET", f"{SERVER}/api/v1/logs?task_id={task_id}")
        print("cron 超时诊断:")
        print("  task:", json.dumps(task_detail, ensure_ascii=False, default=str)[:500])
        print("  logs:", json.dumps(log_dump, ensure_ascii=False, default=str)[:500])
        raise AssertionError("150s 内未收到 cron 同步数据")
    _, task_detail = http("GET", f"{SERVER}/api/v1/tasks/{task_id}")
    assert task_detail["last_run_at"], "last_run_at 未更新"
    return {"scenario": "cron", "task": task_id,
            "last_run_at": task_detail["last_run_at"]}


# 兼容类型注解
class str_port(int):
    pass


def main() -> int:
    mock = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "mock_target.py"),
         "--port", "18081"],
        cwd=ROOT, stdout=subprocess.DEVNULL)
    wait_ready("http://127.0.0.1:18081/health")
    server = None
    results = []
    try:
        server = start_server("data/smoke_runtime.db", stderr_path="data/server_err.log")
        print("== 1. Webhook ==")
        results.append(scenario_webhook(mock_port=18081))
        print("== 2. 增量同步 ==")
        results.append(scenario_incremental(mock_port=18081))
        print("== 3. Cron 调度（等待最多 150s）==")
        results.append(scenario_cron(mock_port=18081))
        print(json.dumps(results, ensure_ascii=False, indent=1))
        print("RUNTIME FEATURES ALL PASS")
        return 0
    finally:
        mock.terminate()
        if server:
            server.terminate()


if __name__ == "__main__":
    sys.exit(main())
