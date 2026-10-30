import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping

from guard_agent._json_types import JsonValue
from guard_agent._transport_dispatch import (
    ETaggedResponse,
    NotModifiedResponse,
    TransportDispatchMixin,
)
from guard_agent._version import __version__ as _AGENT_VERSION
from guard_agent.exceptions import PayloadTooLargeError, PermanentClientError
from guard_agent.models import (
    AgentConfig,
    AgentStatus,
    DynamicRules,
    EventBatch,
    SecurityEvent,
    SecurityMetric,
)
from guard_agent.utils import (
    RateLimitedError,
    RateLimiter,
    calculate_backoff_delay,
    generate_batch_id,
    get_current_timestamp,
)

_MAX_RETRY_AFTER_SECONDS = 300.0


def _is_a_rule_set(body: Mapping[str, JsonValue]) -> bool:
    """A rules body names its rule and version; an error shape names neither."""
    return isinstance(body.get("rule_id"), str) and isinstance(body.get("version"), int)


class TransportSendMixin(TransportDispatchMixin):
    config: AgentConfig
    logger: logging.Logger
    _held_rules: DynamicRules | None
    _held_rules_etag: str | None
    items_dropped_permanently: int = 0

    async def send_events(self, events: list[SecurityEvent]) -> bool:
        """Send security events to the SaaS platform.

        Returns True when the batch was durably accepted OR intentionally
        dropped because it is permanently un-sendable (non-retryable 4xx);
        the caller deletes Redis keys and does not requeue in both cases.
        Returns False only on transient failure, so the caller requeues and
        retains the Redis keys for retry.
        """
        if not events:
            return True

        try:
            batch = EventBatch(
                project_id=self.config.project_id or "default",
                events=events,
                batch_id=generate_batch_id(),
                created_at=get_current_timestamp(),
                agent_version=_AGENT_VERSION,
                guard_version=self.config.guard_version,
                guard_core_version=self.config.guard_core_version,
            )

            return await self._send_with_retry(
                "/api/v1/events", batch.model_dump(), "events"
            )

        except PayloadTooLargeError as e:
            return await self._split_or_drop_on_payload_too_large(
                events, e, "events", self.send_events
            )
        except PermanentClientError as e:
            self._drop_permanent_rejection(events, e, "events")
            return True
        except Exception as e:
            self.logger.error(f"Failed to send events: {str(e)}")
            self.requests_failed += 1
            return False

    async def send_metrics(self, metrics: list[SecurityMetric]) -> bool:
        """Send metrics to the SaaS platform."""
        if not metrics:
            return True

        try:
            batch = EventBatch(
                project_id=self.config.project_id or "default",
                metrics=metrics,
                batch_id=generate_batch_id(),
                created_at=get_current_timestamp(),
                agent_version=_AGENT_VERSION,
                guard_version=self.config.guard_version,
                guard_core_version=self.config.guard_core_version,
            )

            return await self._send_with_retry(
                "/api/v1/metrics", batch.model_dump(), "metrics"
            )

        except PayloadTooLargeError as e:
            return await self._split_or_drop_on_payload_too_large(
                metrics, e, "metrics", self.send_metrics
            )
        except PermanentClientError as e:
            self._drop_permanent_rejection(metrics, e, "metrics")
            return True
        except Exception as e:
            self.logger.error(f"Failed to send metrics: {str(e)}")
            self.requests_failed += 1
            return False

    async def _split_or_drop_on_payload_too_large(
        self,
        items: list,
        error: PayloadTooLargeError,
        data_type: str,
        send_half: Callable[[list], Awaitable[bool]],
    ) -> bool:
        if len(items) <= 1:
            self.logger.warning(
                f"Dropping {data_type} batch of {len(items)} item; "
                f"payload exceeds size cap even as a single item: {error.detail}"
            )
            self.requests_failed += 1
            self.items_dropped_permanently += len(items)
            self._fire_error_hook(
                "transport_send",
                error,
                {"data_type": data_type, "item_count": len(items)},
            )
            return True
        midpoint = len(items) // 2
        left = await send_half(items[:midpoint])
        right = await send_half(items[midpoint:])
        return left and right

    def _drop_permanent_rejection(
        self, items: list, error: PermanentClientError, data_type: str
    ) -> None:
        self.logger.warning(
            f"Dropping {data_type} batch of {len(items)} item(s); "
            f"permanently rejected ({error.status_code}): {error.detail}"
        )
        self.requests_failed += 1
        self.items_dropped_permanently += len(items)
        self._fire_error_hook(
            "transport_send",
            error,
            {"data_type": data_type, "item_count": len(items)},
        )

    async def fetch_dynamic_rules(self) -> DynamicRules | None:
        """Fetch dynamic rules from the SaaS platform."""
        try:
            response_data = await self._get_with_retry(
                "/api/v1/rules", **self._conditional_rules_request()
            )

            if isinstance(response_data, NotModifiedResponse):
                return self._held_rules_after_not_modified()

            if response_data:
                if not _is_a_rule_set(response_data):
                    self.logger.warning(
                        "Ignoring a rules response that is not a rule set "
                        "(no rule_id and version)"
                    )
                    return None
                rules = DynamicRules(**response_data)
                self._held_rules = rules
                self._held_rules_etag = (
                    response_data.etag
                    if isinstance(response_data, ETaggedResponse)
                    else None
                )
                return rules

            return None

        except Exception as e:
            self.logger.error(f"Failed to fetch dynamic rules: {str(e)}")
            return None

    def _conditional_rules_request(self) -> dict[str, dict[str, str]]:
        """Headers and params naming the rule set held, empty before the first 200.

        The rule id and version travel together because synthetic rule sets
        reuse versions; the ETag is sent only when the last 200 carried one.
        """
        held = self._held_rules
        if held is None:
            return {}
        request: dict[str, dict[str, str]] = {
            "params": {
                "last_rule_id": held.rule_id,
                "last_rule_version": str(held.version),
            }
        }
        if self._held_rules_etag:
            request["headers"] = {"If-None-Match": self._held_rules_etag}
        return request

    def _held_rules_after_not_modified(self) -> DynamicRules | None:
        if self._held_rules is None:
            self.logger.warning("Rules GET answered 304 but no rules are held")
        return self._held_rules

    async def send_status(self, status: AgentStatus) -> bool:
        """Send agent status/health information."""
        try:
            return await self._send_with_retry(
                "/api/v1/status",
                status.model_dump(),
                "status",
                self.status_rate_limiter,
            )

        except Exception as e:
            self.logger.error(f"Failed to send status: {str(e)}")
            return False

    def _evaluate_send_result(
        self, result: dict[str, JsonValue] | bool, data_type: str
    ) -> bool | None:
        """Return True (accepted), False (partial failure), or None (retry)."""
        if isinstance(result, dict) and (
            result.get("success") is False or result.get("errors")
        ):
            self.logger.warning(
                f"Server acknowledged {data_type} batch with partial failure: "
                f"success={result.get('success')!r} errors={result.get('errors')!r}"
            )
            self.requests_failed += 1
            return False
        if result or isinstance(result, dict):
            self.requests_sent += 1
            self.logger.debug(f"Successfully sent {data_type} batch")
            return True
        self.requests_failed += 1
        return None

    async def _sleep_or_record_giveup(self, attempt: int, delay: float) -> bool:
        """Sleep to retry (return True) or record a final failure (return False)."""
        if attempt < self.config.retry_attempts:
            await asyncio.sleep(delay)
            return True
        self.requests_failed += 1
        return False

    async def _send_with_retry(
        self,
        endpoint: str,
        data: Mapping[str, object],
        data_type: str,
        limiter: RateLimiter | None = None,
    ) -> bool:
        """Send data with retry logic and circuit breaker."""
        limiter = limiter or self.rate_limiter
        for attempt in range(self.config.retry_attempts + 1):
            try:
                if not await limiter.acquire():
                    retry_after = limiter.get_retry_after()
                    self.logger.warning(
                        f"Rate limit exceeded, waiting {retry_after:.1f}s"
                    )
                    await asyncio.sleep(retry_after)
                    continue

                result = await self.circuit_breaker.call(
                    self._make_request, "POST", endpoint, data
                )

                outcome = self._evaluate_send_result(result, data_type)
                if outcome is not None:
                    return outcome

            except RateLimitedError as e:
                delay = min(e.retry_after_seconds, _MAX_RETRY_AFTER_SECONDS)
                self.logger.warning(
                    f"Server rate-limited {data_type}; sleeping {delay:.1f}s "
                    f"per Retry-After"
                )
                await self._sleep_or_record_giveup(attempt, delay)
            except PermanentClientError:
                raise
            except Exception as e:
                self.logger.warning(
                    f"Attempt {attempt + 1} failed for {data_type}: {str(e)}"
                )

                delay = calculate_backoff_delay(attempt, self.config.backoff_factor)
                if not await self._sleep_or_record_giveup(attempt, delay):
                    self.logger.error(f"All retry attempts failed for {data_type}")
                    self._fire_error_hook(
                        "transport_send",
                        e,
                        {"endpoint": endpoint, "data_type": data_type},
                    )

        return False

    async def _back_off_after_rate_limited_get(
        self, error: RateLimitedError, endpoint: str, attempt: int
    ) -> None:
        delay = min(error.retry_after_seconds, _MAX_RETRY_AFTER_SECONDS)
        self.logger.warning(
            f"Server rate-limited GET {endpoint}; sleeping {delay:.1f}s per Retry-After"
        )
        if attempt < self.config.retry_attempts:
            await asyncio.sleep(delay)
        else:
            self.requests_failed += 1

    async def _back_off_after_failed_get(
        self, error: Exception, endpoint: str, attempt: int
    ) -> None:
        self.logger.warning(
            f"GET attempt {attempt + 1} failed for {endpoint}: {str(error)}"
        )

        if attempt < self.config.retry_attempts:
            delay = calculate_backoff_delay(attempt, self.config.backoff_factor)
            await asyncio.sleep(delay)
        else:
            self.requests_failed += 1

    async def _get_with_retry(
        self,
        endpoint: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, JsonValue] | None:
        """GET request with retry logic and circuit breaker."""
        for attempt in range(self.config.retry_attempts + 1):
            try:
                while not await self.rules_rate_limiter.acquire():
                    await asyncio.sleep(self.rules_rate_limiter.get_retry_after())

                response_data = await self.circuit_breaker.call(
                    self._make_request,
                    "GET",
                    endpoint,
                    None,
                    headers or None,
                    params or None,
                )

                if isinstance(response_data, dict):
                    self.requests_sent += 1
                    return response_data
                else:
                    self.requests_failed += 1
                    if attempt < self.config.retry_attempts:
                        await asyncio.sleep(
                            calculate_backoff_delay(attempt, self.config.backoff_factor)
                        )

            except RateLimitedError as e:
                await self._back_off_after_rate_limited_get(e, endpoint, attempt)
            except PermanentClientError as e:
                self.logger.warning(
                    f"GET {endpoint} permanently rejected ({e.status_code}); "
                    f"not retrying"
                )
                self.requests_failed += 1
                return None
            except Exception as e:
                await self._back_off_after_failed_get(e, endpoint, attempt)

        return None
