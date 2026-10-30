from __future__ import annotations

import contextlib
import logging
import os
import re
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

__all__ = ["resolve_install_id"]

_INSTALL_ID_FORMAT = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


def _read_install_id(path: Path) -> str | None:
    try:
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        logger.exception("install_id.read_failed path=%s", path)
        return None


def _create_exclusively(temp: Path, path: Path) -> bool:
    """Put the written temp file at `path` only if nothing is there yet.

    A hard link fails when the target exists, so of several processes racing
    to create the file exactly one wins. Where hard links are unavailable the
    file is created with O_EXCL and filled in.
    """
    try:
        os.link(temp, path)
    except FileExistsError:
        return False
    except OSError:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(temp.read_text(encoding="utf-8"))
    return True


def _write_install_id(path: Path, install_id: str, *, exclusive: bool) -> bool | None:
    """True when `install_id` is now the stored id, False when another process
    stored one first (exclusive mode only), None when nothing could be stored."""
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(install_id, encoding="utf-8")
        if exclusive:
            return _create_exclusively(temp, path)
        os.replace(temp, path)
        return True
    except OSError:
        logger.exception("install_id.write_failed path=%s", path)
        return None
    finally:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)


def _default_state_path() -> Path | None:
    try:
        return Path.home() / ".guard-agent" / "install-id"
    except RuntimeError:
        logger.exception("install_id.home_unresolved")
        return None


def _is_install_id(value: str | None) -> bool:
    return value is not None and _INSTALL_ID_FORMAT.fullmatch(value) is not None


def _stored_or_new_id(path: Path) -> str:
    existing = _read_install_id(path)
    if _is_install_id(existing):
        return str(existing)
    if existing:
        logger.warning(
            "install_id.invalid path=%s: the stored value is not an install id "
            "this agent wrote, so a new one replaces it",
            path,
        )
    new_id = str(uuid4())
    stored = _write_install_id(path, new_id, exclusive=existing is None)
    if stored is None:
        logger.warning(
            "install_id.in_memory path=%s: the install id could not be stored "
            "and lasts for this process only",
            path,
        )
        return new_id
    if stored is False:
        winner = _read_install_id(path)
        if _is_install_id(winner):
            return str(winner)
        _write_install_id(path, new_id, exclusive=False)
    return new_id


def resolve_install_id(
    *, state_path: Path | None = None, override: str | None = None
) -> str:
    if override:
        if _is_install_id(override):
            return override
        logger.warning(
            "install_id.override_invalid: the install_id override is not a "
            "lowercase UUID 4, so it is ignored"
        )
    if state_path is None:
        state_path = _default_state_path()
    if state_path is None:
        logger.warning(
            "install_id.in_memory: no home directory, the install id lasts "
            "for this process only"
        )
        return str(uuid4())
    return _stored_or_new_id(state_path)
