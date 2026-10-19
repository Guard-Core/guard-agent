import asyncio
import logging
import time
from typing import Any

from guard_agent._client_flush import FlushMixin
from guard_agent._client_status import StatusMixin
from guard_agent._rules_staleness import RULES_STALE_WARNING_INTERVAL, rules_are_stale
from guard_agent.models import AgentConfig, DynamicRules
from guard_agent.protocols import TransportProtocol

# asyncio.sleep can wake a hair before the interval has passed; without this
# slack the loop would skip a poll it owes and double the delay.
_LOOP_POLL_SLACK_SECONDS = 0.5


class RulesMixin:
    config: AgentConfig
    logger: logging.Logger
    transport: TransportProtocol
    rules_fetched: int
    _cached_rules: DynamicRules | None
    _rules_last_update: float
    _last_successful_check: float | None
    _rules_stale_warned_at: float | None
    _rules_inflight: "asyncio.Future[DynamicRules | None] | None"

    async def get_dynamic_rules(self) -> DynamicRules | None:
        """Check the SaaS (conditional GET) and return the rules to apply.

        Concurrent calls share one in-flight request and its result.
        """
        inflight = self._rules_inflight
        if inflight is None:
            inflight = asyncio.ensure_future(self._check_dynamic_rules())
            self._rules_inflight = inflight
            inflight.add_done_callback(self._clear_rules_inflight)
        return await asyncio.shield(inflight)

    def _clear_rules_inflight(self, _finished: "asyncio.Future[Any]") -> None:
        # A new request starts only once this one cleared, so nothing newer is lost.
        self._rules_inflight = None

    def _rules_check_is_due(self) -> bool:
        if self._last_successful_check is None:
            return True
        age = time.time() - self._last_successful_check
        return age >= self.config.dynamic_rule_interval - _LOOP_POLL_SLACK_SECONDS

    async def _check_dynamic_rules(self) -> DynamicRules | None:
        try:
            rules = await self.transport.fetch_dynamic_rules()
        except Exception as e:
            self.logger.error(f"Failed to fetch dynamic rules: {str(e)}")
            rules = None
        now = time.time()
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

    def _record_successful_rules_check(self, now: float) -> None:
        self._rules_last_update = now
        self._last_successful_check = now
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
                    await self.get_dynamic_rules()
                self._rules_consecutive_failures = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._rules_consecutive_failures += 1
                self._log_loop_failure(
                    "rules loop", self._rules_consecutive_failures, e
                )
