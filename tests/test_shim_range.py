"""The fastapi-guard-agent alias shim must resolve to this agent's major.

The shim kept `guard-agent>=3.0.0,<4.0.0` while the agent was 4.0.0, so installs
through the old name resolved to an old agent. These tests read the shim's own
pyproject.toml and the bump script, so the range cannot drift again.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from guard_agent._version import __version__

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHIM_PYPROJECT = PROJECT_ROOT / "shim" / "pyproject.toml"


def _shim_requirement() -> Requirement:
    match = re.search(r'"(guard-agent[^"]*)"', SHIM_PYPROJECT.read_text())
    assert match is not None, "shim/pyproject.toml has no guard-agent dependency"
    return Requirement(match.group(1))


def _next_major(version: str) -> Version:
    return Version(f"{Version(version).major + 1}.0.0")


def _load_bump_script() -> ModuleType:
    path = PROJECT_ROOT / ".github" / "scripts" / "bump_version.py"
    spec = importlib.util.spec_from_file_location("bump_version_for_shim_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_shim_range_admits_the_agent_version() -> None:
    assert _shim_requirement().specifier.contains(__version__)


def test_shim_range_excludes_the_next_major() -> None:
    assert not _shim_requirement().specifier.contains(_next_major(__version__))


def test_bump_script_moves_the_shim_range_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "pyproject.toml").write_text(
        'version = "1.3.0"\ndependencies = [\n    "guard-agent>=3.0.0,<4.0.0",\n]\n'
    )
    (shim / "README.md").write_text(
        "fastapi-guard-agent==1.3.0 installs guard-agent>=3.0.0,<4.0.0\n"
    )
    bump = _load_bump_script()
    monkeypatch.setattr(bump, "PROJECT_ROOT", tmp_path)

    assert bump.update_shim("4.0.0") is True

    pyproject = (shim / "pyproject.toml").read_text()
    assert 'version = "1.4.0"' in pyproject
    assert '"guard-agent>=4.0.0,<5.0.0"' in pyproject
    assert (
        "fastapi-guard-agent==1.4.0 installs guard-agent>=4.0.0,<5.0.0"
        in (shim / "README.md").read_text()
    )

    assert bump.update_shim("4.1.2") is True
    assert 'version = "1.4.0"' in (shim / "pyproject.toml").read_text()


def test_bump_script_reports_a_shim_without_a_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "pyproject.toml").write_text('name = "x"\n')
    (shim / "README.md").write_text("")
    bump = _load_bump_script()
    monkeypatch.setattr(bump, "PROJECT_ROOT", tmp_path)

    assert bump.update_shim("4.0.0") is False


def test_shim_module_reexports_every_agent_name_and_warns() -> None:
    import guard_agent

    path = PROJECT_ROOT / "shim" / "fastapi_guard_agent" / "__init__.py"
    spec = importlib.util.spec_from_file_location("fastapi_guard_agent_shim", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with pytest.warns(DeprecationWarning, match="has been renamed"):
        spec.loader.exec_module(module)

    assert sorted(module.__all__) == sorted(guard_agent.__all__)
    for name in guard_agent.__all__:
        assert getattr(module, name) is getattr(guard_agent, name)
