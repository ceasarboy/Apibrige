"""APIBridge FastAPI 应用入口"""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.api import tasks, connections, mappings, logs, metrics, testing, probes, webhook, forms, field_dictionary
from app.config import settings
from app.database import close_engine
from app.redis_client import close_redis
from app.rabbitmq import close_rabbitmq
from app.connectors.database import close_all_pools
from app.metrics_collector import get_metrics
from app.security.auth import check_admin_token, extract_token

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动与关闭"""
    from app.config import settings

    # task 2.3：生产模式空 token 拒绝启动；开发模式打印醒目告警
    if settings.env == "production" and not settings.admin_token:
        raise RuntimeError(
            "admin_token_missing: 生产环境必须配置 APIBRIDGE_ADMIN_TOKEN，拒绝启动"
        )
    if not settings.admin_token:
        logger.warning(
            "⚠️  APIBRIDGE_ADMIN_TOKEN 未配置，管理 API 处于无鉴权模式 —— 仅允许开发环境使用！"
        )

    # 启动时自动创建数据库表
    from app.database import engine
    from app.models.sync import Base
    from app.models import bi as _bi_models
    async with engine.begin() as conn:
        from app.migrations import check_schema
        await conn.run_sync(check_schema)
        if settings.bi_enabled:
            from app.migrations import check_bi_schema
            await conn.run_sync(check_bi_schema)
        tables = [t for t in Base.metadata.sorted_tables
                  if settings.bi_enabled or not t.name.startswith("bi_")]
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))

    if settings.bi_enabled:
        from app.database import async_session
        from app.services.bi_seed import seed_builtin_metrics, ensure_default_board
        async with async_session() as session:
            await seed_builtin_metrics(session)
            await ensure_default_board(session)

    # task 2.3：进程内定时调度器
    from app.config import settings
    from app.services import scheduler
    if settings.scheduler_enabled:
        scheduler.start()
    from app.services import bi_scheduler
    if settings.bi_enabled and settings.bi_aggregation_enabled:
        bi_scheduler.start()
    yield
    await bi_scheduler.stop()
    # 关闭连接
    await close_engine()
    await close_redis()
    await close_rabbitmq()
    await close_all_pools()
    from app.services import scheduler
    await scheduler.stop()


app = FastAPI(
    title="APIBridge",
    description="API数据桥接平台",
    version="1.0.0",
    lifespan=lifespan,
    # 生产环境关闭交互文档（审查 P3）：未认证的 /docs /openapi.json 会暴露
    # 管理面全部端点与模型结构，降低外部探测成本；开发环境保留
    **({} if settings.env == "production"
       else {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}),
)

# task 2.1/2.2：管理面共享 token 鉴权；/health 与 /metrics 豁免；
# /api/v1/webhook/ 走自己的 secret 校验（外部系统不应持有管理 token）
@app.middleware("http")
async def admin_token_guard(request: Request, call_next):
    if request.url.path.startswith("/api/v1"):
        if not request.url.path.startswith("/api/v1/webhook/"):
            # BI 只读访问令牌（add-bi-access-scope）：带 X-BI-Token 的请求放行到路由，
            # 由 bi 路由依赖做真实校验与范围控制（中间件里读库层级不当、代价高）；
            # 未带该头的请求行为不变（仍只认管理令牌）
            bi_token = request.headers.get("x-bi-token")
            if bi_token and request.url.path.startswith("/api/v1/bi/"):
                return await call_next(request)
            ok, code = check_admin_token(extract_token(request.headers))
            if not ok:
                return JSONResponse(
                    status_code=401, content={"detail": {"code": code}}
                )
    return await call_next(request)

# 注册路由
app.include_router(connections.router, prefix="/api/v1/connections", tags=["连接管理"])
app.include_router(mappings.router, prefix="/api/v1/mappings", tags=["字段映射"])
app.include_router(tasks.router, prefix="/api/v1/tasks", tags=["同步任务"])
app.include_router(logs.router, prefix="/api/v1/logs", tags=["执行日志"])
app.include_router(metrics.router, prefix="/api/v1/metrics", tags=["监控统计"])
app.include_router(testing.router, prefix="/api/v1", tags=["测试工具"])
app.include_router(probes.router, prefix="/api/v1/probes", tags=["联调探针"])
app.include_router(webhook.router, prefix="/api/v1/webhook", tags=["Webhook"])


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "healthy", "version": "1.0.0"}


@app.get("/metrics")
async def metrics():
    """Prometheus 指标端点"""
    from app.services.metrics_refresh import refresh_cached
    await refresh_cached()
    return Response(content=get_metrics(), media_type="text/plain; version=0.0.4")


# HTTP 请求指标中间件
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    """自动记录 HTTP 请求指标（路径模板化，避免 UUID 级高基数标签爆炸）"""
    import time
    from app.metrics_collector import http_requests, http_duration

    start = time.time()
    response = await call_next(request)
    elapsed = time.time() - start

    # 命中路由时用路由模板（/api/v1/connections/{conn_id}），未命中归为 not_found
    route = request.scope.get("route")
    path_label = getattr(route, "path", None) or "<not_found>"

    http_requests.labels(
        method=request.method,
        endpoint=path_label,
        status=str(response.status_code),
    ).inc()

    http_duration.labels(
        method=request.method,
        endpoint=path_label,
    ).observe(elapsed)

    return response

app.include_router(forms.router, prefix="/api/v1/forms", tags=["表单注册表"])

app.include_router(field_dictionary.router, prefix="/api/v1/connections", tags=["字段字典"])

from app.api import watermarks
app.include_router(watermarks.router, prefix="/api/v1/tasks", tags=["水位管理"] )

from app.api import bi
app.include_router(bi.router, prefix="/api/v1/bi", tags=["BI"])

from app.api import monitoring
app.include_router(monitoring.router, prefix="/api/v1/metrics", tags=["运行健康度"])


# CORS 配置：默认只放开本机前端开发端口，可用 APIBRIDGE_CORS_ORIGINS 覆盖
# （逗号分隔）。不使用 allow_origins=["*"]：与 allow_credentials 组合会回显
# 任意 Origin，扩大 CSRF/跨站读取面。
# 注意注册顺序：Starlette 后注册者在外层——CORS 必须最后注册才能包住
# 上面两个 http 中间件，否则 401/OPTIONS 预检响应不带 CORS 头，浏览器侧不可诊断。
from app.config import settings as _settings

_cors_origins = [o.strip() for o in _settings.cors_origins.split(",") if o.strip()] \
    or ["http://localhost:3000", "http://localhost:3010", "http://127.0.0.1:3000"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
