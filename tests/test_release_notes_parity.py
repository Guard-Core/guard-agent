"""CHANGELOG.md and docs/release-notes.md are twins: entries, versions and dates.

Ported from guard-core's release-notes parity test and widened to every version
section, because the drift found here was in released sections (a date that
differed by a day, a missing v3.0.1 section, a version heading with no title,
a section placed out of order), not only in the Unreleased one.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"
_RELEASE_NOTES = _REPO_ROOT / "docs" / "release-notes.md"
_SECTION_SEPARATOR = "\n___\n"
_VERSION_HEADING = re.compile(r"^v(\d+)\.(\d+)\.(\d+) \((\d{4}-\d{2}-\d{2})\)$", re.M)
_DASH_RULE = re.compile(r"^-{3,}$")


def _sections(text: str) -> list[str]:
    return text.split(_SECTION_SEPARATOR)


def unreleased_entries(text: str) -> set[str]:
    section = next(s for s in _sections(text) if s.lstrip().startswith("Unreleased"))
    return {line for line in section.splitlines() if line.startswith("- ")}


def version_sections(text: str) -> dict[str, tuple[str, set[str]]]:
    found: dict[str, tuple[str, set[str]]] = {}
    for section in _sections(text):
        heading = _VERSION_HEADING.search(section)
        if heading is None:
            continue
        version = ".".join(heading.group(1, 2, 3))
        entries = {line for line in section.splitlines() if line.startswith("- ")}
        found[version] = (heading.group(4), entries)
    return found


def _version_order(text: str) -> list[tuple[int, ...]]:
    return [
        (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        for m in _VERSION_HEADING.finditer(text)
    ]


def _both() -> tuple[str, str]:
    return (
        _CHANGELOG.read_text(encoding="utf-8"),
        _RELEASE_NOTES.read_text(encoding="utf-8"),
    )


def test_unreleased_sections_hold_the_same_entries() -> None:
    changelog, release_notes = (unreleased_entries(t) for t in _both())
    assert changelog, "CHANGELOG.md Unreleased section has no entries"
    assert changelog - release_notes == set(), "only in CHANGELOG.md"
    assert release_notes - changelog == set(), "only in docs/release-notes.md"


def test_both_files_list_the_same_versions() -> None:
    changelog, release_notes = (version_sections(t) for t in _both())
    assert set(changelog) ^ set(release_notes) == set(), "version sections differ"


def test_both_files_give_every_version_the_same_date() -> None:
    changelog, release_notes = (version_sections(t) for t in _both())
    dates = {
        v: (changelog[v][0], release_notes[v][0])
        for v in changelog.keys() & release_notes.keys()
        if changelog[v][0] != release_notes[v][0]
    }
    assert dates == {}, "release date differs between the files"


def test_every_version_section_holds_the_same_entries_in_both_files() -> None:
    changelog, release_notes = (version_sections(t) for t in _both())
    differing = [
        v
        for v in changelog
        if v in release_notes and changelog[v][1] != release_notes[v][1]
    ]
    assert differing == [], "entries differ in these versions"


def test_versions_run_newest_first_in_both_files() -> None:
    for text in _both():
        order = _version_order(text)
        assert order == sorted(order, reverse=True)
        assert len(order) == len(set(order)), "a version appears twice"


def test_no_heading_underline_is_left_without_a_title() -> None:
    for text in _both():
        lines = text.splitlines()
        orphans = [
            n + 1
            for n, line in enumerate(lines)
            if _DASH_RULE.match(line)
            and (n == 0 or lines[n - 1].strip() in ("", "___"))
        ]
        assert orphans == [], f"underline with no title at lines {orphans}"


def test_parity_check_fails_when_one_entry_is_removed_from_one_file() -> None:
    changelog, release_notes = _both()
    removed = next(
        line
        for line in changelog.split(_SECTION_SEPARATOR, 2)[1].splitlines()
        if line.startswith("- ")
    )
    trimmed = changelog.replace(removed + "\n", "", 1)
    assert unreleased_entries(trimmed) != unreleased_entries(release_notes)
