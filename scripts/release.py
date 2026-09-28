"""Cut a release, or print a version's release notes.

    uv run python scripts/release.py cut 0.2.0      # bump pyproject, move Unreleased
    uv run python scripts/release.py notes 0.2.0    # print that version's changelog section

`cut` writes `version = "0.2.0"` into pyproject.toml and turns the changelog's
`## [Unreleased]` section into `## [0.2.0] - <today>` with a fresh, empty Unreleased above
it. It does not run git: the Makefile's `release` target relocks, commits and tags after
this script, so a mistake here is a diff to read, not a tag to delete. `notes` is what the
release workflow puts in the GitHub release body; it needs only the standard library.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
UNRELEASED = "## [Unreleased]"


def bump_pyproject(text: str, version: str) -> str:
    new, n = re.subn(r'^version = "[^"]+"', f'version = "{version}"', text, count=1, flags=re.M)
    if n != 1:
        raise ValueError("no version line in pyproject.toml")
    return new


def cut_changelog(text: str, version: str, today: str) -> str:
    if UNRELEASED not in text:
        raise ValueError("no Unreleased section in the changelog")
    if f"## [{version}]" in text:
        raise ValueError(f"{version} is already in the changelog")
    head, _, rest = text.partition(UNRELEASED)
    body = rest.lstrip("\n")
    if body.startswith("## [") or not body.strip():
        raise ValueError("the Unreleased section is empty; nothing to release")
    return f"{head}{UNRELEASED}\n\n## [{version}] - {today}\n\n{body}"


def section(text: str, version: str) -> str:
    """The body of `## [version] - date`, up to the next `## ` heading."""
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        raise ValueError(f"no section for {version} in the changelog")
    return m.group(1).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["cut", "notes"])
    ap.add_argument("version")
    ap.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    ap.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    args = ap.parse_args(argv)
    if not SEMVER.match(args.version):
        print(f"version must be x.y.z, got {args.version!r}", file=sys.stderr)
        return 2
    changelog = args.changelog.read_text(encoding="utf-8")
    try:
        if args.command == "notes":
            sys.stdout.write(section(changelog, args.version))
            return 0
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        pyproject = bump_pyproject(args.pyproject.read_text(encoding="utf-8"), args.version)
        cut = cut_changelog(changelog, args.version, today)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    args.pyproject.write_text(pyproject, encoding="utf-8")
    args.changelog.write_text(cut, encoding="utf-8")
    print(
        f"{args.pyproject}: version = {args.version}; {args.changelog}: [{args.version}] - {today}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
