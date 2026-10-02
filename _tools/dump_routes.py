"""导出 FastAPI 路由表（路径 + 方法），用于三方对比。

用 OpenAPI schema 枚举（当前 FastAPI 版本的 include_router 为惰性实现，
app.routes 不再展平；schema 路径不受影响，且顺带校验 Pydantic 模型）。

用法：python dump_routes.py <项目根目录> <输出 json>
"""
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
out = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(root))

if not os.environ.get("APIBRIDGE_ENCRYPT_KEY"):
    sys.exit("需要环境变量 APIBRIDGE_ENCRYPT_KEY（解密连接凭据用，见本地 .env）")
os.environ.setdefault("APIBRIDGE_ADMIN_TOKEN", "dev-admin-token")
os.environ.setdefault("APIBRIDGE_DATABASE_URL", "sqlite+aiosqlite:///./data/_route_dump.db")

from app.main import app  # noqa: E402

schema = app.openapi()
routes = sorted([path, sorted(m.upper() for m in ops)] for path, ops in (schema.get("paths") or {}).items())
out.write_text(json.dumps(routes, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"{len(routes)} paths -> {out}")
