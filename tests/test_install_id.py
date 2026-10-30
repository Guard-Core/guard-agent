from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from guard_agent.install_id import resolve_install_id


def test_first_call_creates_file(tmp_path: Path) -> None:
    iid = resolve_install_id(state_path=tmp_path / "install-id")
    assert iid is not None
    assert (tmp_path / "install-id").exists()


def test_second_call_returns_same_id(tmp_path: Path) -> None:
    a = resolve_install_id(state_path=tmp_path / "install-id")
    b = resolve_install_id(state_path=tmp_path / "install-id")
    assert a == b


def test_explicit_override_takes_precedence(tmp_path: Path) -> None:
    iid = resolve_install_id(
        state_path=tmp_path / "install-id",
        override="3f2504e0-4f89-41d3-9a0c-0305e82c3301",
    )
    assert iid == "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def test_id_is_uuid4_format(tmp_path: Path) -> None:
    iid = resolve_install_id(state_path=tmp_path / "install-id")
    parts = iid.split("-")
    assert len(parts) == 5
    assert len(iid) == 36


def test_read_install_id_swallows_oserror_returns_fresh_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state_path = tmp_path / "install-id"
    state_path.write_text("existing-id", encoding="utf-8")

    with (
        caplog.at_level(logging.ERROR, logger="guard_agent.install_id"),
        patch.object(Path, "read_text", side_effect=PermissionError("denied")),
    ):
        result = resolve_install_id(state_path=state_path)

    assert result is not None
    assert result != "existing-id"
    assert len(result) == 36
    assert any("install_id.read_failed" in rec.message for rec in caplog.records)


def test_write_install_id_swallows_oserror_still_returns_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state_path = tmp_path / "install-id"

    with (
        caplog.at_level(logging.ERROR, logger="guard_agent.install_id"),
        patch.object(Path, "write_text", side_effect=PermissionError("denied")),
    ):
        result = resolve_install_id(state_path=state_path)

    assert result is not None
    assert len(result) == 36
    assert any("install_id.write_failed" in rec.message for rec in caplog.records)
