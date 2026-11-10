import socket

import pytest

from tests import conftest


def test_a_connect_outside_loopback_is_refused_and_recorded() -> None:
    with socket.socket() as sock:
        with pytest.raises(OSError, match="outside loopback"):
            sock.connect(("203.0.113.7", 443))
    recorded = conftest._NETWORK_VIOLATIONS.copy()
    conftest._NETWORK_VIOLATIONS.clear()
    assert len(recorded) == 1
    assert "203.0.113.7" in recorded[0]


def test_connect_ex_outside_loopback_is_refused_and_recorded() -> None:
    with socket.socket(socket.AF_INET6) as sock:
        with pytest.raises(OSError, match="outside loopback"):
            sock.connect_ex(("2001:db8::1", 443, 0, 0))
    recorded = conftest._NETWORK_VIOLATIONS.copy()
    conftest._NETWORK_VIOLATIONS.clear()
    assert len(recorded) == 1


def test_a_connect_to_loopback_is_allowed() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.socket() as client:
            client.connect(server.getsockname())
    assert not conftest._NETWORK_VIOLATIONS
