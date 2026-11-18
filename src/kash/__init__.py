"""Kash - self-hosted personal finance and budgeting application."""

from importlib.metadata import version

# The one place the version is set is pyproject.toml (the release PR bumps it, and merging
# that PR tags the release with the same number).
__version__ = version("kash")
