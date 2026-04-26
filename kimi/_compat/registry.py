"""Compatibility shim for the platform-adapter registry hook.

This shim's job is narrow: detect at module-import time whether
``hermes-agent`` includes the ``register_platform_factory`` hook. If it
doesn't, raise ``ImportError`` with an actionable install hint instead
of letting the original "no module named ..." error bubble up — that
error gets stripped by the Hermes plugin loader's exception handler.

Two failure modes are distinguished:

1. **Registry hook missing**: ``gateway.platforms.registry`` doesn't
   exist. The user's hermes-agent install is too old (or vanilla
   upstream pre-merge of the registry hook PR). Tell them which branch
   to install.

2. **gateway.config import failed**: a real broken install (partial
   pip install, missing transitive deps, etc.). Re-raise with the
   original cause attached so the diagnostic isn't misleading.

A second precondition — ``Platform.KIMI`` membership in the closed
enum — is checked by the plugin's ``register()`` function instead of
here, so the actionable WARNING log surfaces in the gateway log even
if the loader's exception handler strips multi-line messages.
"""

from __future__ import annotations

_FORK_INSTALL_HINT = (
    "  pip install 'hermes-agent @ "
    "git+https://github.com/linxule/hermes-agent.git@feat/kimi-plugin-variant'"
)

# First: distinguish "registry hook missing" from "gateway.config broken".
# We probe gateway.config independently because gateway.platforms.registry
# imports it internally — failure there would otherwise be misattributed.
try:
    import gateway.config  # noqa: F401  — import for side-effect probe
except ImportError as exc:
    raise ImportError(
        "hermes-kimi-plugin failed to import gateway.config from hermes-agent. "
        "This usually means your hermes-agent install is broken or incomplete "
        "(missing transitive dependencies, partial pip install, etc.). "
        "Original cause is attached above."
    ) from exc

try:
    from gateway.platforms.registry import register_platform_factory  # noqa: F401
except ImportError as exc:
    raise ImportError(
        "hermes-kimi-plugin requires the platform-adapter registry hook in "
        "hermes-agent (currently in upstream review at "
        "https://github.com/NousResearch/hermes-agent — PR pending). Until "
        "merged, install hermes-agent from a branch that carries both the "
        "hook AND the Platform.KIMI enum value:\n\n"
        f"{_FORK_INSTALL_HINT}\n"
    ) from exc

__all__ = ["register_platform_factory"]
