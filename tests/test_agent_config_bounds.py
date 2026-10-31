from __future__ import annotations

import pytest
from pydantic import ValidationError

from guard_agent.models import AgentConfig

# The bounds the TypeScript agent enforces in validateAgentConfig
# (guard-agent-ts v3.2.4 src/config.ts:141-181).
REFUSED: list[tuple[str, object]] = [
    ("buffer_size", 0),
    ("buffer_size", -1),
    ("flush_interval", 0),
    ("flush_interval", -5),
    ("timeout", 0),
    ("timeout", -1),
    ("retry_attempts", -1),
    ("backoff_factor", 0),
    ("backoff_factor", -0.5),
    ("high_watermark_ratio", 0),
    ("high_watermark_ratio", -0.1),
    ("high_watermark_ratio", 1.01),
    ("high_watermark_ratio", 5),
    ("max_concurrent_flushes", 0),
    ("max_concurrent_flushes", -2),
    ("compression_threshold", -1),
    ("stop_timeout", 0),
    ("stop_timeout", -1),
]

ACCEPTED: list[tuple[str, object]] = [
    ("stop_timeout", 0.5),
    ("buffer_size", 1),
    ("flush_interval", 1),
    ("timeout", 1),
    ("retry_attempts", 0),
    ("backoff_factor", 0.001),
    ("high_watermark_ratio", 1),
    ("high_watermark_ratio", 0.001),
    ("max_concurrent_flushes", 1),
    ("compression_threshold", 0),
]


@pytest.mark.parametrize(("field", "value"), REFUSED)
def test_out_of_range_numeric_values_are_refused(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=field):
        AgentConfig.model_validate({"api_key": "k1234567890abc", field: value})


@pytest.mark.parametrize(("field", "value"), ACCEPTED)
def test_the_edge_of_each_range_is_accepted(field: str, value: object) -> None:
    config = AgentConfig.model_validate({"api_key": "k1234567890abc", field: value})

    assert getattr(config, field) == value


def test_every_documented_default_passes_its_own_bound() -> None:
    config = AgentConfig(api_key="k1234567890abc")

    assert (
        config.buffer_size,
        config.flush_interval,
        config.timeout,
        config.retry_attempts,
        config.backoff_factor,
        config.high_watermark_ratio,
        config.max_concurrent_flushes,
        config.compression_threshold,
    ) == (100, 30, 30, 3, 1.0, 0.8, 1, 1024)
    for field_name in (
        "buffer_size",
        "flush_interval",
        "timeout",
        "retry_attempts",
        "backoff_factor",
        "high_watermark_ratio",
        "max_concurrent_flushes",
        "compression_threshold",
    ):
        default = AgentConfig.model_fields[field_name].default
        AgentConfig.model_validate({"api_key": "k1234567890abc", field_name: default})
