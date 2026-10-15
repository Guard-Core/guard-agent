import asyncio
import contextvars
import logging
import time
import uuid
from collections import deque

from guard_agent.models import SecurityEvent, SecurityMetric
from guard_agent.protocols import RedisHandlerProtocol
from guard_agent.utils import safe_json_deserialize, safe_json_serialize

_PERSISTING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "guard_agent_redis_persisting", default=False
)


class BufferRedisMixin:
    logger: logging.Logger
    redis_handler: RedisHandlerProtocol | None
    event_buffer: "deque[SecurityEvent]"
    metric_buffer: "deque[SecurityMetric]"
    events_buffered: int
    metrics_buffered: int
    redis_persist_failures: int
    _event_redis_keys: dict[int, str]
    _metric_redis_keys: dict[int, str]
    _pending_event_persists: dict[int, bool]
    _pending_metric_persists: dict[int, bool]

    def _get_event_condition(self) -> asyncio.Condition:
        raise NotImplementedError

    def _get_metric_condition(self) -> asyncio.Condition:
        raise NotImplementedError

    def _is_event_buffer_full(self) -> bool:
        raise NotImplementedError

    def _is_metric_buffer_full(self) -> bool:
        raise NotImplementedError

    def _forget_oldest_event_key(self) -> str | None:
        raise NotImplementedError

    def _forget_oldest_metric_key(self) -> str | None:
        raise NotImplementedError

    async def _persist_event_to_redis(self, event: SecurityEvent) -> str | None:
        """Persist event to Redis under a globally-unique key; return that key."""
        if not self.redis_handler:
            return None

        try:
            key = f"event_{time.time_ns()}_{uuid.uuid4().hex[:8]}"
            data = event.model_dump() if hasattr(event, "model_dump") else vars(event)
            serialized = await safe_json_serialize(data)
            await self.redis_handler.set_key(
                "agent_events",
                key,
                serialized,
                ttl=3600,
            )
            return key
        except Exception as e:
            self.redis_persist_failures += 1
            self.logger.warning(f"Failed to persist event to Redis: {str(e)}")
            return None

    async def _persist_metric_to_redis(self, metric: SecurityMetric) -> str | None:
        """Persist metric to Redis under a globally-unique key; return that key."""
        if not self.redis_handler:
            return None

        try:
            key = f"metric_{time.time_ns()}_{uuid.uuid4().hex[:8]}"
            if hasattr(metric, "model_dump"):
                data = metric.model_dump()
            else:
                data = vars(metric)
            serialized = await safe_json_serialize(data)
            await self.redis_handler.set_key(
                "agent_metrics",
                key,
                serialized,
                ttl=3600,
            )
            return key
        except Exception as e:
            self.redis_persist_failures += 1
            self.logger.warning(f"Failed to persist metric to Redis: {str(e)}")
            return None

    async def _persist_and_track_event(self, event: SecurityEvent) -> None:
        """Persist an already-buffered event, outside the buffer lock.

        Skipped while a persist is in flight in this context, so a Redis
        failure that emits an event cannot recurse into Redis. If the event
        left the buffer meanwhile (flushed), the new key is deleted instead
        of tracked, so it neither orphans nor replays after a restart. An
        event already gone before the task starts is never written.
        """
        if not self._pending_event_persists.get(id(event)):
            self._pending_event_persists.pop(id(event), None)
            return
        token = _PERSISTING.set(True)
        try:
            key = await self._persist_event_to_redis(event)
        except Exception as e:
            self.logger.error(f"Failed to buffer event: {str(e)}")
            return
        finally:
            _PERSISTING.reset(token)
        if key is None:
            return
        if self._pending_event_persists.pop(id(event), False):
            self._event_redis_keys[id(event)] = key
        else:
            await self.confirm_event_redis_keys([key])

    async def _persist_and_track_metric(self, metric: SecurityMetric) -> None:
        """Metric twin of _persist_and_track_event."""
        if not self._pending_metric_persists.get(id(metric)):
            self._pending_metric_persists.pop(id(metric), None)
            return
        token = _PERSISTING.set(True)
        try:
            key = await self._persist_metric_to_redis(metric)
        except Exception as e:
            self.logger.error(f"Failed to buffer metric: {str(e)}")
            return
        finally:
            _PERSISTING.reset(token)
        if key is None:
            return
        if self._pending_metric_persists.pop(id(metric), False):
            self._metric_redis_keys[id(metric)] = key
        else:
            await self.confirm_metric_redis_keys([key])

    async def _load_from_redis(self) -> None:
        """Load persisted events/metrics from Redis on startup; track keys.

        Each batch loads under its buffer's condition lock, so a startup
        load cannot race live add_event/add_metric traffic touching the
        same deque and Redis-key map.
        """
        if not self.redis_handler:
            return

        try:
            async with self._get_event_condition():
                await self._load_events_from_redis()
            async with self._get_metric_condition():
                await self._load_metrics_from_redis()

            if self.event_buffer or self.metric_buffer:
                loaded_events = f"Loaded {len(self.event_buffer)} events"
                loaded_metrics = f"Loaded {len(self.metric_buffer)} metrics"
                self.logger.info(f"{loaded_events} and {loaded_metrics} from Redis")

        except Exception as e:
            self.logger.warning(f"Failed to load from Redis: {str(e)}")

    async def _load_events_from_redis(self) -> None:
        """Load persisted events from Redis."""
        assert self.redis_handler is not None
        event_keys = await self.redis_handler.keys("agent_events:*") or []
        for key in event_keys:
            await self._load_one_event_from_redis(key)

    async def _load_one_event_from_redis(self, key: str) -> None:
        """Load a single event from Redis, recording the key on success.

        If the buffer is already at capacity, the oldest item's key is
        forgotten first so the deque's own maxlen eviction never orphans
        a still-tracked Redis record.
        """
        assert self.redis_handler is not None
        try:
            short_key = key.split(":")[-1]
            event_data = await self.redis_handler.get_key("agent_events", short_key)
            if not event_data:
                self.logger.warning(
                    f"Failed to load event from Redis key {key}: No data found for key"
                )
                return
            event_dict = await safe_json_deserialize(event_data)
            if not event_dict:
                return
            event = SecurityEvent(**event_dict)
            if self._is_event_buffer_full():
                self._forget_oldest_event_key()
            self.event_buffer.append(event)
            self.events_buffered += 1
            self._event_redis_keys[id(event)] = short_key
        except Exception as e:
            self.logger.warning(f"Failed to load event from Redis key {key}: {e}")

    async def _load_metrics_from_redis(self) -> None:
        """Load persisted metrics from Redis."""
        assert self.redis_handler is not None
        metric_keys = await self.redis_handler.keys("agent_metrics:*") or []
        for key in metric_keys:
            await self._load_one_metric_from_redis(key)

    async def _load_one_metric_from_redis(self, key: str) -> None:
        """Load a single metric from Redis, recording the key on success.

        If the buffer is already at capacity, the oldest item's key is
        forgotten first so the deque's own maxlen eviction never orphans
        a still-tracked Redis record.
        """
        assert self.redis_handler is not None
        try:
            short_key = key.split(":")[-1]
            metric_data = await self.redis_handler.get_key("agent_metrics", short_key)
            if not metric_data:
                self.logger.warning(
                    f"Failed to load metric from Redis key {key}: No data found for key"
                )
                return
            metric_dict = await safe_json_deserialize(metric_data)
            if not metric_dict:
                return
            metric = SecurityMetric(**metric_dict)
            if self._is_metric_buffer_full():
                self._forget_oldest_metric_key()
            self.metric_buffer.append(metric)
            self.metrics_buffered += 1
            self._metric_redis_keys[id(metric)] = short_key
        except Exception as e:
            self.logger.warning(f"Failed to load metric from Redis key {key}: {e}")

    async def _delete_matching_redis_keys(
        self, namespace: str, pattern: str, limit: int | None = None
    ) -> None:
        assert self.redis_handler is not None
        keys = await self.redis_handler.keys(pattern) or []
        if limit is not None:
            keys = sorted(keys)[:limit]
        for key in keys:
            key_name = key.split(":")[-1]
            await self.redis_handler.delete(namespace, key_name)

    async def _clear_events_from_redis(self, count: int) -> None:
        """Clear flushed events from Redis."""
        if not self.redis_handler:
            return

        try:
            await self._delete_matching_redis_keys(
                "agent_events", "agent_events:*", count
            )
        except Exception as e:
            self.logger.warning(f"Failed to clear events from Redis: {str(e)}")

    async def _clear_metrics_from_redis(self, count: int) -> None:
        """Clear flushed metrics from Redis."""
        if not self.redis_handler:
            return

        try:
            await self._delete_matching_redis_keys(
                "agent_metrics", "agent_metrics:*", count
            )
        except Exception as e:
            self.logger.warning(f"Failed to clear metrics from Redis: {str(e)}")

    async def _clear_redis_buffers(self) -> None:
        """Clear all Redis buffers."""
        if not self.redis_handler:
            return

        try:
            await self._delete_matching_redis_keys("agent_events", "agent_events:*")
            await self._delete_matching_redis_keys("agent_metrics", "agent_metrics:*")
            self.logger.info("Cleared all Redis buffers")
        except Exception as e:
            self.logger.warning(f"Failed to clear Redis buffers: {str(e)}")

    async def confirm_event_redis_keys(self, keys: list[str]) -> None:
        """Delete the given event keys from Redis after the transport confirms."""
        if not self.redis_handler:
            return
        for key in keys:
            if not key:
                continue
            try:
                await self.redis_handler.delete("agent_events", key)
            except Exception as e:
                self.logger.warning(f"Failed to delete confirmed event key {key}: {e}")

    async def confirm_metric_redis_keys(self, keys: list[str]) -> None:
        """Delete the given metric keys from Redis after the transport confirms."""
        if not self.redis_handler:
            return
        for key in keys:
            if not key:
                continue
            try:
                await self.redis_handler.delete("agent_metrics", key)
            except Exception as e:
                self.logger.warning(f"Failed to delete confirmed metric key {key}: {e}")
