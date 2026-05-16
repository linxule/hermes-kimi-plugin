"""hermes-kimi-plugin — Kimi (kimi.com / Moonshot AI) platform adapter for Hermes Agent.

Registers a ``KimiAdapter`` via upstream's ``ctx.register_platform()`` API
(introduced by Teknium's ``2e20f6ae2`` "plugin platform parity" commit on
2026-04-11).  All canonical platform wiring — env-var loading, auth env-maps,
prompt hint, cron home-channel, display flags — lives as parameters on the
registration call inside ``kimi_adapter.register()``.

The Hermes plugin loader imports this module under ``hermes_plugins.kimi``
(see ``hermes_cli/plugins.py::PluginManager._load_directory_module``) and
calls the exported ``register(ctx)`` once at gateway startup.

For pytest at the repo root, ``conftest.py`` + ``--import-mode=importlib``
let test files import ``kimi_adapter`` directly as a top-level module
without needing this ``__init__.py`` to load at all.
"""

from __future__ import annotations

from .kimi_adapter import KimiAdapter, check_kimi_requirements, register

__all__ = ["KimiAdapter", "check_kimi_requirements", "register"]
