"""联调冒烟用模拟目标/源系统（task 6.17 / 8.1 / 8.3）

GET  /health           → 200 {"status":"ok"}
GET  /api/users        → 200 {"user_name":"张三","age":"25"}（可选校验 Bearer Token）
POST /api/customers    → 201 回显收到的 JSON（可选校验 Bearer Token）
GET  /api/customers/<id> → 200 回显最近一次创建的记录（模拟按 ID 回读）

用法: python scripts/mock_target.py --port 18081 [--require-token SECRET]
"""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LATEST = {}


def make_handler(require_token: str | None):
    class Handler(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            if not require_token:
                return True
            auth = self.headers.get("Authorization", "")
            return auth == f"Bearer {require_token}"

        def _deny(self):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "unauthorized"}).encode())

        def _send(self, code: int, payload):
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"status": "ok"})
            if not self._authorized():
                return self._deny()
            if self.path == "/api/users":
                return self._send(200, {"user_name": "张三", "age": "25"})
            if self.path.startswith("/api/customers/"):
                from urllib.parse import unquote
                key = unquote(self.path.rsplit("/", 1)[-1])
                return self._send(200, LATEST.get(key, {}))
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._deny()
            if self.path == "/api/customers":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b"{}"
                payload = json.loads(body)
                # 批量语义：数组与单对象都支持；存储键为首个非空字段值 + latest
                items = payload if isinstance(payload, list) else [payload]
                for item in items:
                    if isinstance(item, dict):
                        first_val = next((str(v) for v in item.values() if v), "latest")
                        LATEST[first_val] = item
                        LATEST["latest"] = item
                return self._send(201, payload)
            self._send(404, {"error": "not found"})

        def log_message(self, *a):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18081)
    ap.add_argument("--require-token", default=None)
    args = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.require_token))
    print(f"mock target on 127.0.0.1:{args.port} require_token={bool(args.require_token)}",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
