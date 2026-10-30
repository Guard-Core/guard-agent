from __future__ import annotations

import io
import json
import logging
import re
import threading
from collections.abc import Generator
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from guard_agent.install_id import resolve_install_id
from guard_agent.logging_utils import JsonFormatter, setup_agent_logging
from guard_agent.models import AgentConfig, SecurityEvent
from guard_agent.transport import HTTPTransport

UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


def _warnings(caplog: pytest.LogCaptureFixture, marker: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and marker in r.getMessage()
    ]


def test_unusable_state_directory_falls_back_to_one_in_memory_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is needed")

    with caplog.at_level(logging.WARNING, logger="guard_agent.install_id"):
        result = resolve_install_id(state_path=blocker / "sub" / "install-id")

    assert UUID4.fullmatch(result)
    assert len(_warnings(caplog, "install_id.in_memory")) == 1


def test_unreadable_state_directory_does_not_raise(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(self: Path) -> bool:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "exists", denied)

    with caplog.at_level(logging.WARNING, logger="guard_agent.install_id"):
        result = resolve_install_id(state_path=tmp_path / "install-id")

    assert UUID4.fullmatch(result)


def test_unresolvable_home_falls_back_to_one_in_memory_id(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_home() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", no_home)

    with caplog.at_level(logging.WARNING, logger="guard_agent.install_id"):
        result = resolve_install_id()

    assert UUID4.fullmatch(result)
    assert len(_warnings(caplog, "install_id.in_memory")) == 1


@pytest.mark.parametrize(
    "stored",
    [
        "not-a-uuid\nsecond line",
        "",
        "   ",
        "123e4567-e89b-12d3-a456-426614174000",
        "3F2504E0-4F89-41D3-9A0C-0305E82C3301",
        "x" * 500,
    ],
)
def test_an_invalid_stored_id_is_replaced_and_rewritten_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stored: str
) -> None:
    state = tmp_path / "install-id"
    state.write_text(stored, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="guard_agent.install_id"):
        result = resolve_install_id(state_path=state)

    assert UUID4.fullmatch(result)
    assert state.read_text(encoding="utf-8") == result
    assert len(_warnings(caplog, "install_id.invalid")) == (1 if stored.strip() else 0)


def test_a_valid_stored_id_is_kept_without_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state = tmp_path / "install-id"
    written = resolve_install_id(state_path=state)

    with caplog.at_level(logging.WARNING, logger="guard_agent.install_id"):
        again = resolve_install_id(state_path=state)

    assert again == written
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.fixture
def stub_ingest() -> Generator[tuple[int, list[str | None]], None, None]:
    seen: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append(self.headers.get("X-Agent-Install-Id"))
            self.send_response(201)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, seen
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.mark.asyncio
async def test_a_corrupt_install_id_file_still_lets_the_real_transport_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_ingest: tuple[int, list[str | None]],
) -> None:
    port, seen = stub_ingest
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    state = tmp_path / ".guard-agent" / "install-id"
    state.parent.mkdir()
    state.write_text("not-a-uuid\nsecond line", encoding="utf-8")
    config = AgentConfig(
        api_key="k1234567890abc",
        endpoint=f"http://127.0.0.1:{port}",
        retry_attempts=0,
        timeout=5,
    )
    transport = HTTPTransport(config)
    await transport.initialize()
    event = SecurityEvent(
        timestamp=datetime.now(timezone.utc),
        event_type="penetration_attempt",
        ip_address="203.0.113.7",
        action_taken="logged",
    )

    delivered = await transport.send_events([event])
    await transport.close()

    assert delivered is True
    assert seen == [state.read_text(encoding="utf-8")]
    assert UUID4.fullmatch(seen[0] or "")


@pytest.fixture(autouse=True)
def clean_agent_logger() -> Generator[None, None, None]:
    agent_logger = logging.getLogger("guard_agent")
    agent_logger.handlers = []
    agent_logger.setLevel(logging.NOTSET)
    yield
    for handler in agent_logger.handlers[:]:
        handler.close()
        agent_logger.removeHandler(handler)
    agent_logger.setLevel(logging.NOTSET)


def test_json_formatter_keeps_the_traceback_and_the_stack() -> None:
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("guard_agent.json_exc")
    log.propagate = False
    log.addHandler(handler)
    try:
        try:
            raise ZeroDivisionError("boom")
        except ZeroDivisionError:
            log.exception("boom", stack_info=True)
    finally:
        log.removeHandler(handler)

    entry = json.loads(sink.getvalue())
    assert "ZeroDivisionError" in entry["exception"]
    assert entry["exception"].startswith("Traceback")
    assert "Stack (most recent call last)" in entry["stack"]


def test_reconfigure_removes_only_the_handlers_the_agent_attached() -> None:
    agent_logger = logging.getLogger("guard_agent")
    setup_agent_logging()
    sink = io.StringIO()
    host = logging.StreamHandler(sink)
    agent_logger.addHandler(host)

    setup_agent_logging()

    assert host in agent_logger.handlers
    assert len(agent_logger.handlers) == 1
    assert not sink.closed


def test_log_file_is_honoured_next_to_a_host_handler(tmp_path: Path) -> None:
    agent_logger = logging.getLogger("guard_agent")
    host = logging.StreamHandler(io.StringIO())
    agent_logger.addHandler(host)
    log_file = tmp_path / "agent.log"

    setup_agent_logging(log_file=str(log_file), reconfigure=False)
    setup_agent_logging(log_file=str(log_file), reconfigure=False)

    files = [h for h in agent_logger.handlers if isinstance(h, logging.FileHandler)]
    assert len(files) == 1
    assert files[0].baseFilename == str(log_file)
    assert host in agent_logger.handlers


def test_reconfigure_with_log_file_and_a_host_handler_adds_the_file_handler(
    tmp_path: Path,
) -> None:
    agent_logger = logging.getLogger("guard_agent")
    host = logging.StreamHandler(io.StringIO())
    agent_logger.addHandler(host)
    log_file = tmp_path / "agent.log"

    setup_agent_logging(log_file=str(log_file))
    setup_agent_logging(log_file=str(log_file))

    files = [h for h in agent_logger.handlers if isinstance(h, logging.FileHandler)]
    assert len(files) == 1
    assert host in agent_logger.handlers
    agent_logger.info("reaches the file")
    for handler in files:
        handler.flush()
    assert "reaches the file" in log_file.read_text(encoding="utf-8")
