"""BI budgets are isolated from synchronization."""
import asyncio
from functools import wraps
from weakref import WeakKeyDictionary
from fastapi import HTTPException
from fastapi.routing import APIRoute
from app.config import settings

_slots = WeakKeyDictionary()
_mutations = WeakKeyDictionary()

def loop_lock():
    return _mutations.setdefault(asyncio.get_running_loop(), asyncio.Lock())

def serialize_aggregation(func):
    @wraps(func)
    async def wrapped(*args, **kwargs):
        async with asyncio.timeout(settings.bi_query_timeout_seconds):
            async with loop_lock():
                return await func(*args, **kwargs)
    return wrapped

class BiRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def bounded(request):
            if request.url.path.endswith("/status"):
                return await handler(request)
            slots = _slots.setdefault(asyncio.get_running_loop(), asyncio.Semaphore(2))
            try:
                async with asyncio.timeout(settings.bi_query_timeout_seconds):
                    async with slots:
                        return await handler(request)
            except TimeoutError:
                raise HTTPException(504, "BI 查询超时，请缩小范围") from None
        return bounded
