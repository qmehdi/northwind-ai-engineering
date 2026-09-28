"""Northwind Cloud support-ticket intelligence."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

FALLBACK_VERSION = "0.0.0+unknown"


def _read_version() -> str:
    """The version of the installed package: `pyproject.toml` is the single place it is
    written, `uv sync` installs it, `importlib.metadata` reads it back. The fallback shows
    up when the package is imported from a checkout that was never installed, so a
    `x-nw-version: 0.0.0+unknown` header is a deployment mistake, not a release."""
    try:
        return _installed_version("nw")
    except PackageNotFoundError:
        return FALLBACK_VERSION


__version__ = _read_version()
