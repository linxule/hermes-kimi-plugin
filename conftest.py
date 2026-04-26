"""Repo-level pytest config.

The plugin code lives under ``kimi/`` (not at the repo root) so that the
``__init__.py`` the Hermes plugin loader needs is hidden from pytest's
collection walk. Without that nesting, pytest's parent-package detection
picks up the root ``__init__.py`` and tries to import it standalone,
which fails on the relative imports inside ``register()`` because there's
no parent-package context.

To let test files import ``kimi_adapter`` as a top-level module
(matching the layout the Hermes plugin loader expects when ``kimi/`` is
symlinked into ``$HERMES_HOME/plugins/kimi/``), this conftest.py inserts
``<repo>/kimi/`` into sys.path.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent / "kimi"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
