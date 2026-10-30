import logging
import time
from datetime import datetime, timezone
from typing import Literal

from guard_agent._json_types import JsonValue
from guard_agent._rules_staleness import rules_are_stale
from guard_agent.buffer import EventBuffer
from guard_agent.models import AgentConfig, AgentStatus, DynamicRules
from guard_agent.transport import HTTPTransport
from guard_agent.utils import get_current_timestamp


def _number_or_none(value: JsonValue) -> float | None:
    return value if isinstance(value, (int, float)) else None


# A flush that fails this many times in a row makes the agent unhealthy; the
# count resets with the first flush that delivers.
_UNHEALTHY_AFTER_CONSECUTIVE_FLUSH_FAILURES = 3


class StatusMixin:
    config: AgentConfig
    logger: logging.Logger
    buffer: EventBuffer
    transport: HTTPTransport
    _start_time: float
    _running: bool
    events_sent: int
    metrics_sent: int
    events_failed: int
    metrics_failed: int
    rules_fetched: int
    _cached_rules: DynamicRules | None
    _rules_last_update: float
    _last_successful_check: float | None
    _last_successful_check_wall: float | None
    _flush_consecutive_failures: int
    _status_consecutive_failures: int
    _rules_consecutive_failures: int
    _last_status_push_ok: bool | None
    _events_failure_streak: int
    _metrics_failure_streak: int

    def _flush_failure_streak(self) -> int:
        return max(self._events_failure_streak, self._metrics_failure_streak)

    async def get_status(self) -> AgentStatus:
        current_time = get_current_timestamp()
        uptime = time.time() - self._start_time
        buffer_size = await self.buffer.get_buffer_size()

        transport_stats = self.transport.get_stats()
        buffer_stats = self.buffer.get_stats()
        last_flush_time = buffer_stats.get("last_flush_time")

        status: Literal["healthy", "degraded", "failed"] = "healthy"
        errors: list[str] = []

        if transport_stats["circuit_breaker_state"] == "OPEN":
            status = "degraded"
            errors.append("Transport circuit breaker is open")

        if buffer_size >= self.config.buffer_size * 0.9:
            status = "degraded"
            errors.append("Buffer nearly full")

        streak = self._flush_failure_streak()
        if streak > 0:
            status = "degraded"
            errors.append(f"Delivery is failing: {streak} consecutive flush failure(s)")

        if self.events_failed + self.metrics_failed > 0:
            failure_rate = (self.events_failed + self.metrics_failed) / max(
                1,
                self.events_sent
                + self.metrics_sent
                + self.events_failed
                + self.metrics_failed,
            )
            if failure_rate > 0.1:
                status = "degraded"
                errors.append(f"High failure rate: {failure_rate:.1%}")

        return AgentStatus(
            timestamp=current_time,
            status=status,
            uptime=uptime,
            events_sent=self.events_sent,
            events_failed=self.events_failed,
            buffer_size=buffer_size,
            last_flush=_number_or_none(last_flush_time),
            errors=errors,
            rules_stale=rules_are_stale(
                self._cached_rules, self._last_successful_check, time.monotonic()
            ),
            rules_last_checked=(
                None
                if self._last_successful_check_wall is None
                else datetime.fromtimestamp(
                    self._last_successful_check_wall, tz=timezone.utc
                )
            ),
        )

    def get_stats(self) -> dict[str, JsonValue]:
        return {
            "running": self._running,
            "uptime": time.time() - self._start_time,
            "events_sent": self.events_sent,
            "metrics_sent": self.metrics_sent,
            "events_failed": self.events_failed,
            "metrics_failed": self.metrics_failed,
            "rules_fetched": self.rules_fetched,
            "buffer_stats": self.buffer.get_stats(),
            "transport_stats": self.transport.get_stats(),
            "cached_rules": self._cached_rules is not None,
            "rules_last_update": self._rules_last_update,
            "loop_failures": {
                "flush": self._flush_consecutive_failures,
                "status": self._status_consecutive_failures,
                "rules": self._rules_consecutive_failures,
            },
            "last_status_push_ok": self._last_status_push_ok,
        }

    async def health_check(self) -> bool:
        if not self._running:
            return False

        try:
            transport_stats = self.transport.get_stats()
            if transport_stats.get("circuit_breaker_state") == "OPEN":
                return False

            buffer_size = await self.buffer.get_buffer_size()
            if buffer_size >= self.config.buffer_size * 0.95:
                return False

            if (
                self._flush_failure_streak()
                >= _UNHEALTHY_AFTER_CONSECUTIVE_FLUSH_FAILURES
            ):
                return False

            total_sent = self.events_sent + self.metrics_sent
            total_failed = self.events_failed + self.metrics_failed
            total_attempts = total_sent + total_failed

            if total_attempts > 0:
                failure_rate = total_failed / total_attempts
                if failure_rate > 0.5:
                    return False

            return True
        except Exception as e:
            self.logger.error(f"Error during health check: {str(e)}")
            return False
