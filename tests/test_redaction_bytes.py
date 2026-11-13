"""Redaction reaches bytes values, which SecurityEvent.metadata can carry.

metadata is typed dict[str, object], so a caller can put a request body in it as
bytes. The redactor used to return bytes untouched, and the JSON encoder then wrote
their repr ("b'...'") with every secret in it. Bytes are decoded as UTF-8 (invalid
bytes replaced, as for bytes keys) and redacted like the same text. SecurityMetric
tags are dict[str, str], so bytes cannot reach them past validation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from guard_agent.client import GuardAgentHandler
from guard_agent.models import AgentConfig, SecurityEvent, SecurityMetric
from guard_agent.utils import sanitize_headers

SENSITIVE = ["authorization", "password"]


def test_json_bytes_value_is_redacted_like_the_same_text() -> None:
    as_text = sanitize_headers({"body": '{"password":"hunter2"}'}, SENSITIVE)
    as_bytes = sanitize_headers({"body": b'{"password":"hunter2"}'}, SENSITIVE)
    assert as_bytes == as_text == {"body": '{"password": "[REDACTED]"}'}


@pytest.mark.parametrize("wrap", [bytearray, memoryview])
def test_other_bytes_like_values_are_redacted_too(wrap: type) -> None:
    out = sanitize_headers({"body": wrap(b'{"password":"hunter2"}')}, SENSITIVE)
    assert out == {"body": '{"password": "[REDACTED]"}'}


def test_bytes_nested_in_a_list_are_redacted() -> None:
    out = sanitize_headers({"items": [b'{"authorization":"Bearer x"}']}, SENSITIVE)
    assert "Bearer" not in json.dumps(out)


def test_plain_bytes_decode_to_text_and_invalid_bytes_are_replaced() -> None:
    assert sanitize_headers({"a": b"hello"}, SENSITIVE) == {"a": "hello"}
    assert sanitize_headers({"a": b"\xff"}, SENSITIVE) == {"a": "�"}


def test_bytes_under_a_sensitive_key_stay_redacted() -> None:
    out = sanitize_headers({"Authorization": b"Bearer abc"}, SENSITIVE)
    assert out == {"Authorization": "[REDACTED]"}


async def test_event_metadata_bytes_are_redacted_at_send_event() -> None:
    GuardAgentHandler._instance = None
    handler = GuardAgentHandler(
        AgentConfig(
            api_key="test-api-key",
            endpoint="http://localhost:8000",
            project_id="p",
            flush_interval=3600,
            sensitive_headers=SENSITIVE,
        )
    )
    await handler.send_event(
        SecurityEvent(
            timestamp=datetime.now(timezone.utc),
            event_type="request",
            ip_address="203.0.113.7",
            action_taken="log",
            metadata={"body": b'{"password":"hunter2"}'},
        )
    )
    stored = handler.buffer.event_buffer[0].metadata
    assert "hunter2" not in json.dumps(stored, default=str)
    GuardAgentHandler._instance = None


def test_metric_tags_cannot_hold_bytes_past_validation() -> None:
    metric = SecurityMetric.model_validate(
        {
            "timestamp": datetime.now(timezone.utc),
            "metric_type": "request_count",
            "value": 1.0,
            "tags": {"k": b"v"},
        }
    )
    assert metric.tags == {"k": "v"}
