import asyncio
import contextvars
import logging
import time
import uuid
from collections import deque

from guard_agent.exceptions import GuardAgentError
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
    instance_id: str

    @property
    def _event_namespace(self) -> str:
        """Where this instance persists its events: no other instance shares it."""
        return f"agent_events:{self.instance_id}"

    @property
    def _metric_namespace(self) -> str:
        return f"agent_metrics:{self.instance_id}"

    def _require_redis_handler(self) -> RedisHandlerProtocol:
        if self.redis_handler is None:
            raise GuardAgentError("Redis handler is not initialized")
        return self.redis_handler

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
                self._event_namespace,
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
                self._metric_namespace,
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
            key = None
        finally:
            _PERSISTING.reset(token)
            still_buffered = self._pending_event_persists.pop(id(event), False)
        if key is None:
            return
        if still_buffered:
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
            key = None
        finally:
            _PERSISTING.reset(token)
            still_buffered = self._pending_metric_persists.pop(id(metric), False)
        if key is None:
            return
        if still_buffered:
            self._metric_redis_keys[id(metric)] = key
        else:
            await self.confirm_metric_redis_keys([key])

    async def list_persisted_instances(self) -> dict[str, dict[str, int]]:
        """Count the persisted items of every OTHER buffer instance on this Redis.

        Returns ``{instance_id: {"events": n, "metrics": m}}``. Nothing is loaded
        or changed. A live worker is listed too: only the operator knows which
        instance is dead, so the choice to recover one stays explicit.
        """
        redis_handler = self._require_redis_handler()
        found: dict[str, dict[str, int]] = {}
        for kind, namespace in (
            ("events", "agent_events"),
            ("metrics", "agent_metrics"),
        ):
            for key in await redis_handler.keys(f"{namespace}:*") or []:
                instance_id = key.split(":")[-2]
                if instance_id == self.instance_id or instance_id == namespace:
                    continue
                counts = found.setdefault(instance_id, {"events": 0, "metrics": 0})
                counts[kind] += 1
        return found

    async def recover_orphaned_instance(self, instance_id: str) -> tuple[int, int]:
        """Adopt the persisted events and metrics of a dead buffer instance.

        This is the only path that loads items another instance persisted: the
        buffer never does it on its own, so a worker never re-sends what a live
        worker still holds. Each item is moved (written under this instance,
        then deleted from the dead one's namespace). Returns the adopted
        ``(events, metrics)`` counts; this instance's own id adopts nothing.
        """
        if not self.redis_handler or instance_id == self.instance_id:
            return 0, 0

        events_before, metrics_before = self.events_buffered, self.metrics_buffered
        try:
            async with self._get_event_condition():
                await self._adopt_events_from_instance(instance_id)
            async with self._get_metric_condition():
                await self._adopt_metrics_from_instance(instance_id)
        except Exception as e:
            self.logger.warning(f"Failed to load from Redis: {str(e)}")
        adopted = (
            self.events_buffered - events_before,
            self.metrics_buffered - metrics_before,
        )
        if any(adopted):
            loaded_events = f"Loaded {adopted[0]} events"
            loaded_metrics = f"Loaded {adopted[1]} metrics"
            self.logger.info(
                f"{loaded_events} and {loaded_metrics} from Redis "
                f"(instance {instance_id})"
            )
        return adopted

    async def _adopt_events_from_instance(self, instance_id: str) -> None:
        redis_handler = self._require_redis_handler()
        source = f"agent_events:{instance_id}"
        for key in await redis_handler.keys(f"{source}:*") or []:
            await self._load_one_event_from_redis(key, source)

    async def _load_one_event_from_redis(self, key: str, source_namespace: str) -> None:
        """Move a single event of ``source_namespace`` into this instance.

        If the buffer is already at capacity, the oldest item's key is
        forgotten first so the deque's own maxlen eviction never orphans
        a still-tracked Redis record.
        """
        redis_handler = self._require_redis_handler()
        try:
            short_key = key.split(":")[-1]
            event_data = await redis_handler.get_key(source_namespace, short_key)
            if not event_data:
                self.logger.warning(
                    f"Failed to load event from Redis key {key}: No data found for key"
                )
                return
            event_dict = await safe_json_deserialize(event_data)
            if not event_dict:
                return
            event = SecurityEvent.model_validate(event_dict)
            await redis_handler.set_key(
                self._event_namespace, short_key, event_data, ttl=3600
            )
            await redis_handler.delete(source_namespace, short_key)
            if self._is_event_buffer_full():
                self._forget_oldest_event_key()
            self.event_buffer.append(event)
            self.events_buffered += 1
            self._event_redis_keys[id(event)] = short_key
        except Exception as e:
            self.logger.warning(f"Failed to load event from Redis key {key}: {e}")

    async def _adopt_metrics_from_instance(self, instance_id: str) -> None:
        redis_handler = self._require_redis_handler()
        source = f"agent_metrics:{instance_id}"
        for key in await redis_handler.keys(f"{source}:*") or []:
            await self._load_one_metric_from_redis(key, source)

    async def _load_one_metric_from_redis(
        self, key: str, source_namespace: str
    ) -> None:
        """Metric twin of _load_one_event_from_redis."""
        redis_handler = self._require_redis_handler()
        try:
            short_key = key.split(":")[-1]
            metric_data = await redis_handler.get_key(source_namespace, short_key)
            if not metric_data:
                self.logger.warning(
                    f"Failed to load metric from Redis key {key}: No data found for key"
                )
                return
            metric_dict = await safe_json_deserialize(metric_data)
            if not metric_dict:
                return
            metric = SecurityMetric.model_validate(metric_dict)
            await redis_handler.set_key(
                self._metric_namespace, short_key, metric_data, ttl=3600
            )
            await redis_handler.delete(source_namespace, short_key)
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
        redis_handler = self._require_redis_handler()
        keys = await redis_handler.keys(pattern) or []
        if limit is not None:
            keys = sorted(keys)[:limit]
        for key in keys:
            key_name = key.split(":")[-1]
            await redis_handler.delete(namespace, key_name)

    async def _clear_events_from_redis(self, count: int) -> None:
        """Clear flushed events from Redis."""
        if not self.redis_handler:
            return

        try:
            await self._delete_matching_redis_keys(
                self._event_namespace, f"{self._event_namespace}:*", count
            )
        except Exception as e:
            self.logger.warning(f"Failed to clear events from Redis: {str(e)}")

    async def _clear_metrics_from_redis(self, count: int) -> None:
        """Clear flushed metrics from Redis."""
        if not self.redis_handler:
            return

        try:
            await self._delete_matching_redis_keys(
                self._metric_namespace, f"{self._metric_namespace}:*", count
            )
        except Exception as e:
            self.logger.warning(f"Failed to clear metrics from Redis: {str(e)}")

    async def _clear_redis_buffers(self) -> None:
        """Clear all Redis buffers."""
        if not self.redis_handler:
            return

        try:
            await self._delete_matching_redis_keys(
                self._event_namespace, f"{self._event_namespace}:*"
            )
            await self._delete_matching_redis_keys(
                self._metric_namespace, f"{self._metric_namespace}:*"
            )
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
                await self.redis_handler.delete(self._event_namespace, key)
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
                await self.redis_handler.delete(self._metric_namespace, key)
            except Exception as e:
                self.logger.warning(f"Failed to delete confirmed metric key {key}: {e}")
