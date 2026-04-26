"""hermes-kimi-plugin — Kimi (kimi.com / Moonshot AI) platform adapter for Hermes Agent.

The Hermes plugin loader imports this module under ``hermes_plugins.kimi``
(see ``hermes_cli/plugins.py``'s ``PluginManager._load_directory_module``).
The loader sets ``module.__package__`` and inserts the module into
``sys.modules`` before executing the body, so the relative imports below
resolve correctly.

For pytest at the repo root, ``conftest.py`` + ``--import-mode=importlib``
arrange for test files to import ``kimi_adapter`` directly as a top-level
module without needing this ``__init__.py`` to load at all.

The adapter body lives in :mod:`kimi_adapter`. The compatibility shim
lives in :mod:`_compat.registry`.
"""

from __future__ import annotations

import logging
from typing import Any

# _compat is imported first so the registry-hook check fires before
# anything else can fail. If the installed hermes-agent doesn't have the
# hook, the import below raises ImportError with an actionable message.
from . import _compat  # noqa: F401  — load-time registry-hook check

# Eager imports beyond this point — matches the in-fork plugins/kimi/
# pattern. Keeps the diagnostics consistent: any other module-load error
# (missing transitive dep, gateway/config drift, etc.) shows up at plugin
# discovery rather than register-call time.
from gateway.config import Platform

from .kimi_adapter import KimiAdapter, check_kimi_requirements

logger = logging.getLogger(__name__)

__all__ = ["KimiAdapter", "check_kimi_requirements", "register"]


def register(ctx: Any) -> None:
    """Register the Kimi platform adapter factory with Hermes.

    Requires Platform.KIMI in the closed gateway.config.Platform enum
    (a separate one-line in-tree commit beyond the registry hook PR;
    see ``_compat/registry.py`` for the install hint).
    """
    # Second precondition: closed Platform enum must include KIMI. Done
    # here at register-time (rather than at module load) so the actionable
    # install hint surfaces in the gateway log even when the plugin
    # loader's exception handler strips multi-line messages — by logging
    # at ERROR level *before* re-raising.
    if not hasattr(Platform, "KIMI"):
        msg = (
            "hermes-kimi-plugin requires Platform.KIMI in "
            "gateway.config.Platform. The registry hook PR is for factories, "
            "not enum members; adding Platform.KIMI is a separate one-line "
            "in-tree commit. Until both upstream PRs land, install "
            "hermes-agent from a branch that includes both:\n\n"
            "  pip install 'hermes-agent @ "
            "git+https://github.com/linxule/hermes-agent.git@feat/kimi-plugin-variant'"
        )
        logger.error("kimi plugin will not register: %s", msg)
        raise RuntimeError(msg)

    ctx.register_platform_adapter(
        Platform.KIMI,
        lambda config: KimiAdapter(config),
        requirements_check=check_kimi_requirements,
    )
