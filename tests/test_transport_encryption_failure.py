import base64

import pytest

from guard_agent.encryption import EncryptionConfigError
from guard_agent.models import AgentConfig
from guard_agent.transport import HTTPTransport


def test_encryption_init_failure_raises_not_falls_back() -> None:
    config = AgentConfig(
        api_key="x-api-key-1",
        endpoint="https://example.com",
        project_encryption_key="not-a-valid-key",
    )
    with pytest.raises(EncryptionConfigError):
        HTTPTransport(config)


def test_encryption_init_succeeds_with_valid_key() -> None:
    valid_key = base64.urlsafe_b64encode(b"a" * 32).decode()
    config = AgentConfig(
        api_key="x-api-key-1",
        endpoint="https://example.com",
        project_encryption_key=valid_key,
    )
    transport = HTTPTransport(config)
    assert transport is not None
    assert transport._encryption_enabled is True
    assert transport._encryptor is not None
