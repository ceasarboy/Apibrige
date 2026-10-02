"""连接测试服务 — 对未入库/已入库的连接配置执行连通性测试并生成运行日志

task（用户反馈）：连接测试要有运行日志，否则无法判断成功与否、问题在哪里。
日志自动脱敏（sign/password/token 等值打码），可在前端直接展示。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from app.adapters.factory import ProtocolAdapterFactory
from app.connectors.database import get_database_connector

# 日志中需要脱敏的参数名（值替换为 ***）
_SENSITIVE_PARAMS = ("sign", "appkey", "password", "token", "secret", "key")


def _mask_params(params: dict | None) -> dict:
    masked = {}
    for k, v in (params or {}).items():
        if any(h in k.lower() for h in _SENSITIVE_PARAMS):
            masked[k] = str(v)[:6] + "***" if v else v
        else:
            masked[k] = v
    return masked


def _now_line(message: str, level: str = "info") -> dict:
    return {
        "time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "level": level,
        "message": message,
    }


def _probe_path(protocol: str) -> str:
    """各协议探测请求的说明路径（与适配器内部实现一致）"""
    return {
        "rest": "GET /health",
        "http": "GET /health",
        "hap": "GET /v1/open/app/get（appKey/sign 鉴权）",
        "graphql": "POST（连通性探测）",
        "soap": "WSDL 解析 + 连通性",
        "websocket": "WS 握手",
        "grpc": "gRPC 通道探测",
        "dubbo": "注册中心/直连探测",
    }.get(protocol, "适配器内置探测")


async def test_connection_config(cfg: dict, target_table: str | None = None) -> dict:
    """对（未入库的）连接配置执行连通性测试

    Returns:
        {"result": ConnectionTestResult 兼容 dict, "log": [运行日志行...]}
    """
    log: list[dict] = []
    db_type = cfg.get("db_type")

    if db_type:
        from app.services.db_client import test_db_connection

        _dbc = cfg.get("db_config") or {}
        # Oracle 用 sid/服务名，其余数据库用 database；日志形如 host:port/sid
        add_target = _dbc.get("database") or _dbc.get("sid") or ""
        if _dbc.get("host"):
            add_target = f"{_dbc['host']}:{_dbc.get('port', '')}/{add_target}" if add_target else str(_dbc["host"])
        log.append(_now_line(f"数据库类型: {db_type}，目标: {add_target or '(未指定)'}"))
        log.append(_now_line("执行探测查询: SELECT 1"))
        t0 = time.time()
        result = await test_db_connection(db_type, cfg.get("db_config") or {})
        latency = int((time.time() - t0) * 1000)
        level = "success" if result.get("status") == "ok" else "error"
        log.append(_now_line(
            f"探测结果: status={result.get('status')}，"
            f"总耗时 {latency}ms，驱动报错: {result.get('error') or '无'}", level))
        return {"result": result, "log": log}

    # API 连接：构造与运行时一致的适配器配置
    auth_config = cfg.get("auth_config") or {}
    adapter_config = {
        "base_url": cfg.get("base_url") or "",
        "auth_type": cfg.get("auth_type"),
        "auth_config": auth_config,
        "headers": cfg.get("headers") or {},
        "timeout_ms": cfg.get("timeout_ms", 30000),
    }
    protocol = str(cfg.get("protocol") or "rest").lower()
    log.append(_now_line(f"协议: {protocol}，目标: {adapter_config['base_url']}"))
    log.append(_now_line(f"探测请求: {_probe_path(protocol)}，超时 {adapter_config['timeout_ms']}ms"))
    if cfg.get("auth_type"):
        masked_auth = _mask_params(auth_config)
        log.append(_now_line(f"鉴权: {cfg.get('auth_type')}，凭证字段已脱敏 "
                             f"({', '.join(sorted(masked_auth)) or '无'})"))

    from app.adapters.factory import ProtocolAdapterFactory

    t0 = time.time()
    try:
        adapter = ProtocolAdapterFactory.create(protocol, adapter_config)
        result = await adapter.test_connection()
    except ValueError as e:
        log.append(_now_line(f"适配器创建失败: {e}", "error"))
        return {"result": {"status": "failed", "latency_ms": 0, "error": str(e)},
                "log": log}
    latency = int((time.time() - t0) * 1000)
    level = "success" if result.get("status") == "ok" else "error"
    log.append(_now_line(
        f"探测结果: status={result.get('status')}，"
        f"延迟 {result.get('latency_ms')}ms，报错: {result.get('error') or '无'}", level))
    # 原始请求/响应透出（用户反馈：要能看到字段内容与原始回复）
    if result.get("request"):
        log.append(_now_line(f"原始请求: {result['request']}"))
    if result.get("response_excerpt"):
        log.append(_now_line(f"原始响应: {result['response_excerpt'][:600]}"))
    result = dict(result)
    result["target"] = adapter_config["base_url"]
    return {"result": result, "log": log}
