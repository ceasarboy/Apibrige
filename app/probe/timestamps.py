"""Probe timestamps: preserve offsets; interpret unzoned values as UTC+08:00."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import TIMESTAMP
from sqlalchemy.types import TypeDecorator

EAST_EIGHT = timezone(timedelta(hours=8))


def as_east_eight(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise TypeError("Expected an ISO timestamp or datetime")
    if value.utcoffset() is None:
        return value.replace(tzinfo=EAST_EIGHT)
    return value.astimezone(EAST_EIGHT)


class ProbeTimestamp(TypeDecorator):
    """Keep existing TIMESTAMP columns; persist East-eight wall time and restore its offset.

    SQLite and the existing PostgreSQL columns don't retain offsets. Normalize before
    binding so an aware input never changes its instant after a database round trip.
    Existing naive rows are read as East-eight without rewriting historical data.
    """
    impl = TIMESTAMP
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else as_east_eight(value).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return None if value is None else as_east_eight(value)


class UtcTimestamp(TypeDecorator):
    """TIMESTAMP 列的绑定归一：aware → naive UTC 后入库；读取不加工。

    背景（审查）：asyncpg 对 TIMESTAMP WITHOUT TIME ZONE 拒绝 aware 绑定，
    PostgreSQL 部署"能建表不能写行"。与 ProbeTimestamp（探针域 +08 语义）
    不同，本类型保持既有存储语义——naive UTC 墙钟进出，SQLite/PG 与
    历史数据完全一致，读取路径零变化。
    """
    impl = TIMESTAMP
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
