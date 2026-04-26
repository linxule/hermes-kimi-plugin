"""Compatibility shim package.

Importing this package triggers the upstream-feature-check at module load
time so a missing platform-adapter registry hook fails plugin discovery
cleanly with a clear error, rather than blowing up later inside register()
with a confusing AttributeError.
"""

from .registry import register_platform_factory  # noqa: F401

__all__ = ["register_platform_factory"]
