"""TALISMAN package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("talisman")
except PackageNotFoundError:
    __version__ = "0.1.0"
