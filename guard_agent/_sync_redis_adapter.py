import asyncio
import inspect
from collections.abc import Awaitable
from typing import TypeVar

from typing_extensions import TypeIs

from guard_agent.protocols import RedisHandlerProtocol, SyncRedisHandlerProtocol

T = TypeVar("T")


async def _settled(result: T | Awaitable[T]) -> T:
    # A plain function may hand back an awaitable (the coroutine of an async client).
    if isinstance(result, Awaitable):
        return await result
    return result


class AsyncRedisFromSync:
    """Presents a blocking Redis handler to the async buffer.

    Each call runs on a worker thread: the agent's loop thread also serves
    ``send_event`` for request threads, so a slow Redis must not stall it.
    """

    def __init__(self, sync_handler: SyncRedisHandlerProtocol) -> None:
        self._sync_handler = sync_handler

    async def get_key(self, namespace: str, key: str) -> str | None:
        return await _settled(
            await asyncio.to_thread(self._sync_handler.get_key, namespace, key)
        )

    async def set_key(
        self, namespace: str, key: str, value: str, ttl: int | None = None
    ) -> bool | None:
        return await _settled(
            await asyncio.to_thread(
                self._sync_handler.set_key, namespace, key, value, ttl
            )
        )

    async def delete(self, namespace: str, key: str) -> int | None:
        return await _settled(
            await asyncio.to_thread(self._sync_handler.delete, namespace, key)
        )

    async def keys(self, pattern: str) -> list[str] | None:
        return await _settled(await asyncio.to_thread(self._sync_handler.keys, pattern))

    async def initialize(self) -> None:
        await _settled(await asyncio.to_thread(self._sync_handler.initialize))

    def get_connection(self) -> object:
        return self._sync_handler.get_connection()


def _is_async_redis_handler(
    handler: SyncRedisHandlerProtocol | RedisHandlerProtocol,
) -> TypeIs[RedisHandlerProtocol]:
    return inspect.iscoroutinefunction(handler.get_key)


def as_async_redis_handler(
    handler: SyncRedisHandlerProtocol | RedisHandlerProtocol,
) -> RedisHandlerProtocol:
    if _is_async_redis_handler(handler):
        return handler
    return AsyncRedisFromSync(handler)
