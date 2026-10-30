from __future__ import annotations

import errno
import logging
import os
import re
import threading
from pathlib import Path

import pytest

from guard_agent.install_id import resolve_install_id

UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


@pytest.mark.parametrize("rounds", range(5))
def test_concurrent_first_resolves_all_return_the_one_stored_id(
    tmp_path: Path, rounds: int
) -> None:
    state = tmp_path / "fresh" / "install-id"
    barrier = threading.Barrier(8)
    returned: list[str] = []
    lock = threading.Lock()

    def resolve() -> None:
        barrier.wait()
        value = resolve_install_id(state_path=state)
        with lock:
            returned.append(value)

    threads = [threading.Thread(target=resolve) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(returned) == 8
    assert len(set(returned)) == 1
    assert state.read_text(encoding="utf-8") == returned[0]
    assert [p.name for p in state.parent.iterdir()] == ["install-id"]


def test_a_failed_write_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: Path, data: str, encoding: str | None = None) -> int:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "write_text", refuse)

    assert UUID4.fullmatch(resolve_install_id(state_path=tmp_path / "install-id"))
    assert list(tmp_path.iterdir()) == []


def test_where_hard_links_are_unavailable_the_file_is_still_created_exclusively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_links(src: object, dst: object) -> None:
        raise OSError(errno.EPERM, "links not supported")

    monkeypatch.setattr(os, "link", no_links)
    state = tmp_path / "install-id"

    first = resolve_install_id(state_path=state)

    assert UUID4.fullmatch(first)
    assert state.read_text(encoding="utf-8") == first
    assert resolve_install_id(state_path=state) == first
    assert [p.name for p in tmp_path.iterdir()] == ["install-id"]


def test_losing_the_race_to_a_corrupt_file_replaces_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "install-id"

    def another_process_wrote_junk(src: object, dst: object) -> None:
        Path(str(dst)).write_text("junk\nvalue", encoding="utf-8")
        raise FileExistsError(errno.EEXIST, "exists")

    monkeypatch.setattr(os, "link", another_process_wrote_junk)

    result = resolve_install_id(state_path=state)

    assert UUID4.fullmatch(result)
    assert state.read_text(encoding="utf-8") == result


def test_an_override_that_is_not_an_install_id_is_ignored_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state = tmp_path / "install-id"

    with caplog.at_level(logging.WARNING, logger="guard_agent.install_id"):
        result = resolve_install_id(state_path=state, override="bad\nvalue")

    assert UUID4.fullmatch(result)
    assert state.read_text(encoding="utf-8") == result
    assert [r.getMessage() for r in caplog.records].count(
        "install_id.override_invalid: the install_id override is not a "
        "lowercase UUID 4, so it is ignored"
    ) == 1


def test_a_valid_override_wins_and_is_not_stored(tmp_path: Path) -> None:
    state = tmp_path / "install-id"
    chosen = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"

    assert resolve_install_id(state_path=state, override=chosen) == chosen
    assert not state.exists()
