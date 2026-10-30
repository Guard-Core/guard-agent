from __future__ import annotations

import logging
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


def _write_install_id(path: Path, install_id: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(install_id, encoding="utf-8")
    except OSError:
        logger.exception("install_id.write_failed path=%s", path)
        return False
    return True


def _default_state_path() -> Path | None:
    try:
        return Path.home() / ".guard-agent" / "install-id"
    except RuntimeError:
        logger.exception("install_id.home_unresolved")
        return None


def resolve_install_id(
    *, state_path: Path | None = None, override: str | None = None
) -> str:
    if override:
        return override
    if state_path is None:
        state_path = _default_state_path()
    if state_path is None:
        logger.warning(
            "install_id.in_memory: no home directory, the install id lasts "
            "for this process only"
        )
        return str(uuid4())
    existing = _read_install_id(state_path)
    if existing and _INSTALL_ID_FORMAT.fullmatch(existing):
        return existing
    if existing:
        logger.warning(
            "install_id.invalid path=%s: the stored value is not an install id "
            "this agent wrote, so a new one replaces it",
            state_path,
        )
    new_id = str(uuid4())
    if not _write_install_id(state_path, new_id):
        logger.warning(
            "install_id.in_memory path=%s: the install id could not be stored "
            "and lasts for this process only",
            state_path,
        )
    return new_id
