"""
Deprecation shim - the package has been renamed to ``guard-agent``.

The Python import path has always been ``guard_agent``, so code that
does ``from guard_agent import ...`` continues to work without change.
This module exists only so that older code that imported from the
``fastapi_guard_agent`` namespace directly keeps working while emitting
a deprecation warning.

Every name of ``guard_agent.__all__`` is re-exported explicitly (no star import);
the agent repo's tests/test_shim_range.py fails when a name is missing here.
"""

from __future__ import annotations

import warnings as _warnings

from guard_agent import (
    AgentConfig,
    AgentHandlerProtocol,
    AgentStatus,
    BufferFullError,
    BufferProtocol,
    CircuitBreaker,
    DynamicRules,
    EventBatch,
    EventBuffer,
    GuardAgentError,
    GuardAgentHandler,
    HTTPTransport,
    JsonValue,
    RateLimiter,
    RedisHandlerProtocol,
    SecurityEvent,
    SecurityMetric,
    SyncGuardAgentHandler,
    SyncRedisHandlerProtocol,
    TransportProtocol,
    __version__,
    generate_batch_id,
    get_current_timestamp,
    guard_agent,
    hash_ip,
    sanitize_headers,
    setup_agent_logging,
    truncate_payload,
    validate_config,
)

__all__ = [
    "guard_agent",
    "GuardAgentHandler",
    "SyncGuardAgentHandler",
    "AgentConfig",
    "SecurityEvent",
    "SecurityMetric",
    "DynamicRules",
    "AgentStatus",
    "EventBatch",
    "EventBuffer",
    "BufferFullError",
    "GuardAgentError",
    "HTTPTransport",
    "AgentHandlerProtocol",
    "TransportProtocol",
    "JsonValue",
    "BufferProtocol",
    "RedisHandlerProtocol",
    "SyncRedisHandlerProtocol",
    "generate_batch_id",
    "get_current_timestamp",
    "hash_ip",
    "sanitize_headers",
    "truncate_payload",
    "validate_config",
    "setup_agent_logging",
    "RateLimiter",
    "CircuitBreaker",
    "__version__",
]

_warnings.warn(
    "`fastapi_guard_agent` has been renamed. Import from `guard_agent` instead "
    "(e.g. `from guard_agent import GuardAgentHandler, AgentConfig`). "
    "This alias is provided by the `fastapi-guard-agent` meta-package and "
    "will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)
