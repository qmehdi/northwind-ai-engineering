"""The release cut: pyproject bumped, Unreleased moved under the version, release notes
extracted; the changelog and the security docs are in shape."""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from release import bump_pyproject, cut_changelog, main, section  # noqa: E402

pytestmark = pytest.mark.session06

ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = """# Changelog

Intro.

## [Unreleased]

### Added

- a thing

## [0.1.0] - 2026-09-27

- history
"""


def test_cut_moves_unreleased_under_the_version():
    out = cut_changelog(CHANGELOG, "0.2.0", "2026-10-01")
    assert (
        "## [Unreleased]\n\n## [0.2.0] - 2026-10-01\n\n### Added\n\n- a thing\n\n## [0.1.0]" in out
    )
    assert section(out, "0.2.0") == "### Added\n\n- a thing\n"
    assert section(out, "0.1.0") == "- history\n"
    with pytest.raises(ValueError):
        cut_changelog(out, "0.3.0", "2026-10-02")  # Unreleased is empty now
    with pytest.raises(ValueError):
        cut_changelog(out, "0.2.0", "2026-10-02")  # already released
    with pytest.raises(ValueError):
        section(out, "9.9.9")


def test_bump_rewrites_the_version_line_only():
    text = 'name = "nw"\nversion = "0.1.0"\ndescription = "x"\n[tool]\nversion = "keep"\n'
    assert bump_pyproject(text, "0.2.0") == text.replace('"0.1.0"', '"0.2.0"')
    with pytest.raises(ValueError):
        bump_pyproject("nothing", "0.2.0")


def test_cli_cut_and_notes(tmp_path, capsys):
    py, cl = tmp_path / "pyproject.toml", tmp_path / "CHANGELOG.md"
    py.write_text('[project]\nversion = "0.1.0"\n')
    cl.write_text(CHANGELOG)
    args = ["--pyproject", str(py), "--changelog", str(cl)]
    assert main(["cut", "0.2.0", *args]) == 0
    assert 'version = "0.2.0"' in py.read_text()
    assert re.search(r"## \[0\.2\.0\] - \d{4}-\d{2}-\d{2}", cl.read_text())
    assert main(["notes", "0.2.0", *args]) == 0
    assert capsys.readouterr().out.endswith("- a thing\n")
    assert main(["cut", "0.2.0", *args]) == 1, "the second cut has nothing to release"
    assert main(["cut", "x.y", *args]) == 2


def test_repository_changelog_and_docs_are_in_shape():
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert "## [Unreleased]" in changelog and "## [0.1.0] - 2026-09-27" in changelog
    pyproject = (ROOT / "pyproject.toml").read_text()
    version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    assert f"## [{version}]" in changelog
    for doc in (
        ROOT / "docs" / "SECURITY.md",
        ROOT / "CHANGELOG.md",
        ROOT / "README.md",
        *sorted((ROOT / "docs" / "adr").glob("*.md")),
    ):
        text = doc.read_text()
        assert chr(0x2014) not in text and chr(0x2013) not in text, f"dash in {doc.name}"
    # Current decisions in docs/adr, superseded ones kept in docs/archive/adr; numbers never reused.
    adrs = sorted(p.name for p in (ROOT / "docs" / "adr").glob("*.md"))
    archived = sorted(p.name for p in (ROOT / "docs" / "archive" / "adr").glob("*.md"))
    assert adrs, "no current decision records"
    numbers = [n[:4] for n in adrs + archived]
    assert len(numbers) == len(set(numbers)), "an ADR number is used twice"
    for p in (ROOT / "docs" / "adr").glob("*.md"):
        text = p.read_text()
        assert "## Context" in text and "## Decision" in text and "## Consequences" in text
    security = (ROOT / "docs" / "SECURITY.md").read_text()
    for threat in (
        "Prompt injection",
        "exfiltration",
        "PII",
        "Cost denial",
        "Secret",
        "Supply chain",
        "output harms",
    ):
        assert threat in security
    assert "## Reporting" in security and "## Out of scope" in security
    readme = (ROOT / "README.md").read_text()
    assert "## Versioning" in readme and "## Security" in readme
