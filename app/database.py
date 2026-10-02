"""PostgreSQL 异步数据库连接"""
import json

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.config import settings

# JSON 列序列化统一 default=str：源数据里的 datetime/Decimal/bytes 等驱动原生
# 类型进 DataBuffer.source_data / 探针快照等 JSON 列时不再抛 TypeError
# （审查 P1：Oracle/MySQL 源的 full 同步曾在此直接失败）
engine = create_async_engine(
    settings.database_url, echo=False, pool_size=10, max_overflow=5,
    json_serializer=lambda v: json.dumps(v, ensure_ascii=False, default=str),
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncSession:
    """获取数据库会话（FastAPI 依赖注入用）"""
    async with async_session() as session:
        yield session


async def close_engine():
    """关闭数据库引擎"""
    await engine.dispose()
