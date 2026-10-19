import asyncio
import itertools
import logging
import time
from collections import deque
from typing import Any

from guard_agent._client_flush import FlushMixin
from guard_agent._client_status import StatusMixin
from guard_agent._rules_staleness import RULES_STALE_WARNING_INTERVAL, rules_are_stale
from guard_agent.models import AgentConfig, DynamicRules
from guard_agent.protocols import TransportProtocol

# The loop yields to an outside caller (the engine) for this many agent
# intervals, or engine gaps when those are longer.
_OUTSIDE_CALL_WINDOW_FACTOR = 1.5


class RulesMixin:
    config: AgentConfig
    logger: logging.Logger
    transport: TransportProtocol
    rules_fetched: int
    _cached_rules: DynamicRules | None
    _rules_last_update: float
    _last_successful_check: float | None
    _last_successful_check_wall: float | None
    _outside_call_stamps: deque[float]
    _next_rules_call_is_the_loops: bool
    _rules_stale_warned_at: float | None
    _rules_inflight: "asyncio.Future[DynamicRules | None] | None"

    async def get_dynamic_rules(self) -> DynamicRules | None:
        """Check the SaaS (conditional GET) and return the rules to apply.

        Every call from outside the agent's own loop is stamped, even one that
        joins an in-flight request, so the loop knows an engine is polling.
        Concurrent calls share one in-flight request and its result.
        """
        if self._next_rules_call_is_the_loops:
            self._next_rules_call_is_the_loops = False
        else:
            self._outside_call_stamps.append(time.monotonic())
        return await self._shared_rules_check()

    async def _shared_rules_check(self) -> DynamicRules | None:
        inflight = self._rules_inflight
        if inflight is None:
            inflight = asyncio.ensure_future(self._check_dynamic_rules())
            self._rules_inflight = inflight
            inflight.add_done_callback(self._clear_rules_inflight)
        return await asyncio.shield(inflight)

    async def _rules_check_from_the_loop(self) -> None:
        # The flag is read and cleared at the first line of get_dynamic_rules,
        # which runs before any await, so an outside call cannot take it.
        self._next_rules_call_is_the_loops = True
        try:
            await self.get_dynamic_rules()
        finally:
            self._next_rules_call_is_the_loops = False

    def _clear_rules_inflight(self, _finished: "asyncio.Future[Any]") -> None:
        # A new request starts only once this one cleared, so nothing newer is lost.
        self._rules_inflight = None

    def _rules_check_is_due(self) -> bool:
        """The loop polls only when no outside call started within the window.

        The window is 1.5 x the larger of the agent interval and the larger of
        the last two gaps between outside calls (the agent interval alone before
        the second outside call), so a force_update or an error retry in the
        engine does not make the loop take over.
        """
        stamps = self._outside_call_stamps
        if not stamps:
            return True
        longest = float(self.config.dynamic_rule_interval)
        if len(stamps) >= 2:
            gaps = [later - earlier for earlier, later in itertools.pairwise(stamps)]
            longest = max(longest, *gaps)
        return time.monotonic() - stamps[-1] >= _OUTSIDE_CALL_WINDOW_FACTOR * longest

    async def _check_dynamic_rules(self) -> DynamicRules | None:
        try:
            rules = await self.transport.fetch_dynamic_rules()
        except Exception as e:
            self.logger.error(f"Failed to fetch dynamic rules: {str(e)}")
            rules = None
        now = time.monotonic()
        if not rules:
            self._warn_if_rules_are_stale(now)
            return self._cached_rules
        self._record_successful_rules_check(now)
        if rules is self._cached_rules:
            self.logger.debug("Dynamic rules unchanged")
            return rules
        self._cached_rules = rules
        self.rules_fetched += 1
        self.logger.debug("Dynamic rules updated")
        return rules

    # `now` is monotonic; the wall stamp only feeds the status report.
    def _record_successful_rules_check(self, now: float) -> None:
        self._rules_last_update = time.time()
        self._last_successful_check = now
        self._last_successful_check_wall = time.time()
        if self._rules_stale_warned_at is None:
            return
        self._rules_stale_warned_at = None
        held = self._cached_rules
        self.logger.info(
            "Dynamic rules check recovered after the rules went stale"
            + (f" (held rule {held.rule_id} v{held.version})" if held else "")
        )

    def _warn_if_rules_are_stale(self, now: float) -> None:
        held = self._cached_rules
        last_check = self._last_successful_check
        if held is None or last_check is None:
            return
        if not rules_are_stale(held, last_check, now):
            return
        warned_at = self._rules_stale_warned_at
        if warned_at is not None and now - warned_at < RULES_STALE_WARNING_INTERVAL:
            return
        self._rules_stale_warned_at = now
        self.logger.warning(
            f"Dynamic rules are stale: the SaaS has not answered a rules check "
            f"for {now - last_check:.0f}s (rule {held.rule_id} "
            f"v{held.version}, ttl {held.ttl}s); still serving the cached rules"
        )


class LoopsMixin(FlushMixin, StatusMixin, RulesMixin):
    config: AgentConfig
    logger: logging.Logger
    transport: Any
    _running: bool
    _loop_error_log_threshold: int
    _flush_consecutive_failures: int
    _status_consecutive_failures: int
    _rules_consecutive_failures: int
    _last_status_push_ok: bool | None

    def _log_loop_failure(self, loop_name: str, count: int, exc: Exception) -> None:
        message = (
            f"{loop_name} failed {count} consecutive time(s); "
            f"cause: {type(exc).__name__}: {exc}"
        )
        if count >= self._loop_error_log_threshold:
            self.logger.error(message)
        else:
            self.logger.warning(message)

    async def _flush_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.config.flush_interval)
                if self._running:
                    await self.flush_buffer()
                self._flush_consecutive_failures = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._flush_consecutive_failures += 1
                self._log_loop_failure(
                    "flush loop", self._flush_consecutive_failures, e
                )

    async def _status_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.config.status_interval)
                if self._running:
                    status = await self.get_status()
                    ok = await self.transport.send_status(status)
                    self._last_status_push_ok = bool(ok)
                    if ok:
                        self._status_consecutive_failures = 0
                    else:
                        self._status_consecutive_failures += 1
                        self._log_loop_failure(
                            "status loop",
                            self._status_consecutive_failures,
                            RuntimeError("send_status returned False"),
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._last_status_push_ok = False
                self._status_consecutive_failures += 1
                self._log_loop_failure(
                    "status loop", self._status_consecutive_failures, e
                )

    async def _rules_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.config.dynamic_rule_interval)
                if self._running and self._rules_check_is_due():
                    await self._rules_check_from_the_loop()
                self._rules_consecutive_failures = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._rules_consecutive_failures += 1
                self._log_loop_failure(
                    "rules loop", self._rules_consecutive_failures, e
                )
