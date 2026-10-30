from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from guard_agent.models import AgentConfig
from guard_agent.transport import HTTPTransport


def _make_mock_client() -> AsyncMock:
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value={"status": "ok"})
    mock_response.text = "OK"
    mock_response.headers = {}
    mock_response.url = "http://localhost:8000/test"

    client = AsyncMock()
    client.post = AsyncMock(return_value=mock_response)
    client.get = AsyncMock(return_value=mock_response)
    client.is_closed = False
    client.aclose = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_post_encrypted_adds_signature_header_when_secret_set() -> None:
    valid_key = base64.urlsafe_b64encode(b"0" * 32).decode()
    config = AgentConfig(
        api_key="test_key-api-key-1",
        endpoint="http://test.com",
        project_id="test_project",
        project_encryption_key=valid_key,
        payload_signing_secret="my-hmac-secret",
    )
    transport = HTTPTransport(config)
    mock_client = _make_mock_client()
    transport._client = mock_client

    data = {
        "events": [{"event_type": "test"}],
        "metrics": [],
        "batch_id": "batch-1",
    }

    await transport._post_encrypted(data)

    headers = mock_client.post.call_args.kwargs["headers"]
    assert "X-Payload-Signature" in headers
    assert headers["X-Payload-Signature"].startswith("v1=")


@pytest.mark.asyncio
async def test_post_unencrypted_adds_signature_header_when_secret_set() -> None:
    config = AgentConfig(
        api_key="test_key-api-key-1",
        endpoint="http://test.com",
        project_id="test_project",
        payload_signing_secret="my-hmac-secret",
    )
    transport = HTTPTransport(config)
    mock_client = _make_mock_client()
    transport._client = mock_client

    await transport._post_unencrypted(
        "http://test.com/api/v1/status",
        {"status": "ok"},
    )

    headers = mock_client.post.call_args.kwargs["headers"]
    assert "X-Payload-Signature" in headers
    assert headers["X-Payload-Signature"].startswith("v1=")


@pytest.mark.asyncio
async def test_post_encrypted_omits_signature_header_when_secret_absent() -> None:
    valid_key = base64.urlsafe_b64encode(b"0" * 32).decode()
    config = AgentConfig(
        api_key="test_key-api-key-1",
        endpoint="http://test.com",
        project_id="test_project",
        project_encryption_key=valid_key,
    )
    transport = HTTPTransport(config)
    mock_client = _make_mock_client()
    transport._client = mock_client

    data = {
        "events": [{"event_type": "test"}],
        "metrics": [],
        "batch_id": "batch-1",
    }

    await transport._post_encrypted(data)

    headers = mock_client.post.call_args.kwargs["headers"]
    assert "X-Payload-Signature" not in headers


@pytest.mark.asyncio
async def test_post_unencrypted_omits_signature_header_when_secret_absent() -> None:
    config = AgentConfig(
        api_key="test_key-api-key-1",
        endpoint="http://test.com",
        project_id="test_project",
    )
    transport = HTTPTransport(config)
    mock_client = _make_mock_client()
    transport._client = mock_client

    await transport._post_unencrypted(
        "http://test.com/api/v1/status",
        {"status": "ok"},
    )

    headers = mock_client.post.call_args.kwargs["headers"]
    assert "X-Payload-Signature" not in headers
