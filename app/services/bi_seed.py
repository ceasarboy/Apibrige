"""内置 BI 指标定义的幂等种子写入"""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bi import BiMetricDef, BiScreenSettings
from app.services.bi_defs import BUILTIN_METRICS, DEFAULT_TOP_N

logger = logging.getLogger(__name__)


async def ensure_default_board(session: AsyncSession) -> None:
    """确保默认看板（id='default'）存在（幂等）

    多看板模型下 bi_screen_settings 每行一块画布；default 是
    /bi 与 /bi/screen 不带 board 参数时的兜底看板，必须始终可用。
    """
    if await session.get(BiScreenSettings, "default") is None:
        session.add(BiScreenSettings(id="default", data={
            "name": "默认看板", "layout": {}, "filter_dims": [], "cells": {}}))
        await session.commit()
        logger.info("已创建默认看板")


async def seed_builtin_metrics(session: AsyncSession) -> dict:
    """写入内置指标定义（幂等）

    - 仅插入缺失的 metric_key；
    - 已存在的不覆盖——保留用户对维度/度量/启停状态的修改；
    - 重复调用不产生副作用（不重复插入、不覆盖）。
    """
    existing = set(
        (await session.execute(select(BiMetricDef.metric_key))).scalars().all()
    )
    added: list[str] = []
    for defn in BUILTIN_METRICS:
        key = defn["metric_key"]
        if key in existing:
            continue
        session.add(BiMetricDef(
            metric_key=key,
            name=defn["name"],
            description=defn.get("description"),
            source_type=defn["source_type"],
            source_ref=defn.get("source_ref"),
            time_field=defn.get("time_field", "created_at"),
            granularity=defn.get("granularity", "1min"),
            dimensions=defn.get("dimensions") or [],
            measures=defn.get("measures") or [],
            filters=defn.get("filters"),
            chart_type=defn.get("chart_type", "line"),
            top_n=defn.get("top_n") or DEFAULT_TOP_N,
            enabled=False,
            builtin=True,
        ))
        added.append(key)

    if added:
        await session.commit()
        logger.info("BI 内置指标定义已写入 %d 条: %s", len(added), added)

    return {"added": added, "existing": sorted(existing)}
