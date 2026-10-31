import asyncio
import logging
import time
from typing import Protocol

from guard_agent._transport_lifecycle import TransportLifecycleMixin
from guard_agent.buffer import EventBuffer
from guard_agent.models import AgentConfig, SecurityEvent, SecurityMetric
from guard_agent.utils import calculate_backoff_delay, fire_error_hook

_PARTIAL_FAILURE_MAX_BACKOFF_SECONDS = 300.0


def _dropped_for_good(transport: object) -> int:
    """The transport's lifetime count of items it dropped permanently."""
    count = getattr(transport, "items_dropped_permanently", 0)
    return count if isinstance(count, int) else 0


def _refused_for_quota(transport: object) -> int:
    """The transport's lifetime count of batches the server refused for quota."""
    count = getattr(transport, "batches_refused_for_quota", 0)
    return count if isinstance(count, int) else 0


def _queued_counts(buffer: object) -> tuple[int, int]:
    """Events and metrics waiting in a real buffer; a stand-in buffer holds none."""
    if not isinstance(buffer, EventBuffer):
        return 0, 0
    return len(buffer.event_buffer), len(buffer.metric_buffer)


class FlushTransport(Protocol):
    async def send_events(self, events: list[SecurityEvent]) -> bool: ...

    async def send_metrics(self, metrics: list[SecurityMetric]) -> bool: ...


class FlushMixin:
    config: AgentConfig
    logger: logging.Logger
    buffer: EventBuffer
    transport: FlushTransport
    events_sent: int
    metrics_sent: int
    events_failed: int
    metrics_failed: int
    _events_failure_streak: int
    _metrics_failure_streak: int
    _events_retry_after: float
    _metrics_retry_after: float
    _quota_refusal_streaks: dict[str, int]

    def _fire_error_hook(
        self, stage: str, exc: BaseException, context: dict[str, object]
    ) -> None:
        fire_error_hook(self.config.on_error, self.logger, stage, exc, context)

    def _retention_description(self, kind: str) -> str:
        """Describe where unsent items wait for retry, based on whether a Redis
        handler is attached to the buffer. Without Redis the items live only in
        the in-memory buffer, so claiming Redis retention would be misleading."""
        has_redis = getattr(self.buffer, "redis_handler", None) is not None
        memory_part = "requeued in memory"
        redis_part = " and retained in Redis" if has_redis else ""
        return f"{memory_part}{redis_part} ({kind})"

    def _events_retention_description(self) -> str:
        return self._retention_description("events")

    def _metrics_retention_description(self) -> str:
        return self._retention_description("metrics")

    async def flush_buffer(self) -> None:
        try:
            await self._flush_events()
            await self._flush_metrics()
        except Exception as e:
            self.logger.error(f"Error during buffer flush: {str(e)}")

    async def _flush_for_stop(self) -> None:
        """The last flush of stop(): one attempt per batch, bounded overall."""
        if isinstance(self.transport, TransportLifecycleMixin):
            self.transport.begin_shutdown()
        try:
            await asyncio.wait_for(self.flush_buffer(), self.config.stop_timeout)
        except asyncio.TimeoutError:
            self.logger.debug("stop(): the last flush hit its deadline")
        await self._account_unsent_at_stop()

    async def _account_unsent_at_stop(self) -> None:
        events, metrics = _queued_counts(self.buffer)
        if not events and not metrics:
            return
        if self.buffer.redis_handler is not None:
            self.logger.warning(
                f"stop(): {events} events and {metrics} metrics are still unsent; "
                f"they stay in Redis (instance {self.buffer.instance_id}) for "
                f"recover_orphaned_instance() until their TTL"
            )
            return
        self.events_failed += events
        self.metrics_failed += metrics
        self.logger.warning(
            f"stop(): {events} events and {metrics} metrics could not be sent "
            f"before the stop deadline and are lost"
        )

    async def _flush_events(self) -> None:
        if time.time() < self._events_retry_after:
            return

        events, event_keys = await self.buffer.flush_events_with_keys()
        if not events:
            return

        exc: BaseException | None = None
        dropped_before = _dropped_for_good(self.transport)
        refused_before = _refused_for_quota(self.transport)
        try:
            success = bool(await self.transport.send_events(events))
        except BaseException as caught:
            success = False
            exc = caught

        if success:
            await self.buffer.confirm_event_redis_keys(event_keys)
            lost = max(0, _dropped_for_good(self.transport) - dropped_before)
            self.events_sent += len(events) - lost
            self.events_failed += lost
            self.logger.debug(f"Flushed {len(events)} events")
            if self._events_failure_streak:
                self.logger.warning(
                    f"Events flush recovered after "
                    f"{self._events_failure_streak} consecutive partial failure(s)"
                )
            self._events_failure_streak = 0
            self._quota_refusal_streaks["events"] = 0
            self._events_retry_after = 0.0
            return

        refused = _refused_for_quota(self.transport) > refused_before
        if self._quota_refusal_limit_reached("events", refused):
            await self._drop_events_refused_for_quota(events, event_keys)
        else:
            await self._requeue_and_confirm_events(events, event_keys)
        self._events_failure_streak += 1
        delay = calculate_backoff_delay(
            self._events_failure_streak - 1,
            base_delay=self.config.flush_interval,
            max_delay=_PARTIAL_FAILURE_MAX_BACKOFF_SECONDS,
        )
        self._events_retry_after = time.time() + delay
        if self._events_failure_streak == 1:
            self.logger.warning(
                f"Failed to send {len(events)} events; "
                f"{self._events_retention_description()} for retry; "
                f"backing off up to {delay:.0f}s between attempts"
            )
        if exc is not None:
            self.logger.error(f"Transport raised sending events: {str(exc)}")
            self._fire_error_hook("flush_events", exc, {"batch_size": len(events)})
            raise exc

    async def _flush_metrics(self) -> None:
        if time.time() < self._metrics_retry_after:
            return

        metrics, metric_keys = await self.buffer.flush_metrics_with_keys()
        if not metrics:
            return

        exc: BaseException | None = None
        dropped_before = _dropped_for_good(self.transport)
        refused_before = _refused_for_quota(self.transport)
        try:
            success = bool(await self.transport.send_metrics(metrics))
        except BaseException as caught:
            success = False
            exc = caught

        if success:
            await self.buffer.confirm_metric_redis_keys(metric_keys)
            lost = max(0, _dropped_for_good(self.transport) - dropped_before)
            self.metrics_sent += len(metrics) - lost
            self.metrics_failed += lost
            self.logger.debug(f"Flushed {len(metrics)} metrics")
            if self._metrics_failure_streak:
                self.logger.warning(
                    f"Metrics flush recovered after "
                    f"{self._metrics_failure_streak} consecutive partial failure(s)"
                )
            self._metrics_failure_streak = 0
            self._quota_refusal_streaks["metrics"] = 0
            self._metrics_retry_after = 0.0
            return

        refused = _refused_for_quota(self.transport) > refused_before
        if self._quota_refusal_limit_reached("metrics", refused):
            await self._drop_metrics_refused_for_quota(metrics, metric_keys)
        else:
            await self._requeue_and_confirm_metrics(metrics, metric_keys)
        self._metrics_failure_streak += 1
        delay = calculate_backoff_delay(
            self._metrics_failure_streak - 1,
            base_delay=self.config.flush_interval,
            max_delay=_PARTIAL_FAILURE_MAX_BACKOFF_SECONDS,
        )
        self._metrics_retry_after = time.time() + delay
        if self._metrics_failure_streak == 1:
            self.logger.warning(
                f"Failed to send {len(metrics)} metrics; "
                f"{self._metrics_retention_description()} for retry; "
                f"backing off up to {delay:.0f}s between attempts"
            )
        if exc is not None:
            self.logger.error(f"Transport raised sending metrics: {str(exc)}")
            self._fire_error_hook("flush_metrics", exc, {"batch_size": len(metrics)})
            raise exc

    def _quota_refusal_limit_reached(self, kind: str, refused: bool) -> bool:
        """Count consecutive flushes the server refused for quota.

        Past the agent's retry limit the batch is dropped instead of requeued:
        a project over its quota stays over it until the plan changes, so a
        requeue would re-send the same items on every flush and, once the
        buffer is full, evict newer ones to keep them.
        """
        if not refused:
            self._quota_refusal_streaks[kind] = 0
            return False
        self._quota_refusal_streaks[kind] += 1
        if self._quota_refusal_streaks[kind] <= self.config.retry_attempts:
            return False
        self._quota_refusal_streaks[kind] = 0
        return True

    async def _drop_events_refused_for_quota(
        self, events: list[SecurityEvent], event_keys: list[str]
    ) -> None:
        self.logger.warning(
            f"Dropping {len(events)} events: the server refused them for quota "
            f"{self.config.retry_attempts + 1} flushes in a row"
        )
        await self.buffer.confirm_event_redis_keys(event_keys)
        self.events_failed += len(events)

    async def _drop_metrics_refused_for_quota(
        self, metrics: list[SecurityMetric], metric_keys: list[str]
    ) -> None:
        self.logger.warning(
            f"Dropping {len(metrics)} metrics: the server refused them for quota "
            f"{self.config.retry_attempts + 1} flushes in a row"
        )
        await self.buffer.confirm_metric_redis_keys(metric_keys)
        self.metrics_failed += len(metrics)

    async def _requeue_and_confirm_events(
        self, events: list[SecurityEvent], event_keys: list[str]
    ) -> None:
        evicted_event_keys = await self.buffer.requeue_events_in_memory(
            events, event_keys
        )
        if evicted_event_keys:
            await self.buffer.confirm_event_redis_keys(evicted_event_keys)

    async def _requeue_and_confirm_metrics(
        self, metrics: list[SecurityMetric], metric_keys: list[str]
    ) -> None:
        evicted_metric_keys = await self.buffer.requeue_metrics_in_memory(
            metrics, metric_keys
        )
        if evicted_metric_keys:
            await self.buffer.confirm_metric_redis_keys(evicted_metric_keys)
