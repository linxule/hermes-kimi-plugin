# hermes-kimi-plugin

Kimi (kimi.com / Moonshot AI) chat platform adapter for [Hermes Agent](https://github.com/NousResearch/hermes-agent), distributed as a standalone plugin.

Bridges Hermes Agent gateways to a single Kimi bot identity, handling **direct messages** (Zed ACP over WebSocket) and **group rooms** (Connect RPC over WebSocket) through one adapter — same model `KimiAdapter` Bloom has been running on production since 2026-04-23 (in-tree) and as a plugin since 2026-04-26.

## Status

> **Pending upstream merge.** This plugin requires three things from `hermes-agent` that aren't in vanilla upstream yet, packaged as **two upstream PRs**:
>
> 1. The `register_platform_adapter` hook (generic — adds the registry surface)
> 2. The `Platform.KIMI` enum value + canonical platform-config wiring for Kimi (env-var loading in `_apply_env_overrides`, redaction-list entry, prompt hint, cron delivery, auth env-maps)
>
> Until both land, install Hermes Agent from a fork branch that carries them — see the install section below.
>
> Production reference: Bloom (Xule's Pi) has been running this plugin against `feat/platform-kimi-enum` (the clean upstream-equivalent base for both prospective PRs) since 2026-04-27. Logger module name `hermes_plugins.kimi.kimi_adapter` in gateway logs confirms the plugin path is active end-to-end.

## Install

You'll need:
1. A Hermes Agent install that includes the platform-adapter registry hook AND the canonical Kimi wiring (see "Install Hermes Agent" below)
2. A Kimi bot token from your kimi.com account (Settings → Connected Apps → Generate `km_b_prod_…` token)

### Install Hermes Agent

The recommended install target until both upstream PRs land is `feat/platform-kimi-enum` on the linxule fork. It carries the registry hook PLUS the Kimi enum value PLUS the canonical platform wiring needed for the gateway to actually route traffic to your plugin:

```bash
pip install 'hermes-agent @ git+https://github.com/linxule/hermes-agent.git@feat/platform-kimi-enum'
```

Two other branches exist for narrower inspection — both are **insufficient on their own** for running the plugin:

- `hook/platform-adapter-registry`: the registry hook PR in isolation. This plugin's `register()` raises `RuntimeError` because `Platform.KIMI` is missing. Use only for reviewing the hook PR diff.
- `feat/kimi-plugin-variant`: the legacy fork branch that ships the plugin in-tree. Don't install this if you also intend to use the standalone plugin — you'd get two `plugins/kimi/` discovery entries.

After both upstream PRs merge (registry hook + canonical Kimi wiring), install from PyPI:

```bash
pip install 'hermes-agent>=<version-with-hook-and-kimi-wiring>'
```

### Install the plugin

The Hermes plugin loader discovers plugins from `$HERMES_HOME/plugins/<name>/`. The plugin code lives under `kimi/` in this repo (the nesting keeps the loader's `__init__.py` out of pytest's collection walk). Symlink that subdirectory:

```bash
git clone https://github.com/linxule/hermes-kimi-plugin.git ~/code/hermes-kimi-plugin
ln -s ~/code/hermes-kimi-plugin/kimi "$HERMES_HOME/plugins/kimi"
```

Or if you'd rather copy:

```bash
git clone https://github.com/linxule/hermes-kimi-plugin.git /tmp/hermes-kimi-plugin
cp -r /tmp/hermes-kimi-plugin/kimi "$HERMES_HOME/plugins/kimi"
```

Note: pip-installable distribution is intentionally NOT supported — the `pyproject.toml` ships a metadata-only wheel. The plugin code doesn't belong in `site-packages` (the loader doesn't discover from there, and a flat-wheel layout would clash with other packages). Pip path is deferred until upstream documents a site-packages plugin-discovery convention.

### Configure

Add to `$HERMES_HOME/config.yaml`:

```yaml
plugins:
  enabled:
    - kimi

platforms:
  kimi:
    enabled: true
    # Optional Kimi-specific settings — see kimi_adapter.py for the full shape.
    # Defaults are tuned for Bloom's Pi deployment:
    #   home_channel: nag-once-per-session
    #   subscribe_backoff: 2s → 60s exponential
    #   reconnect_strategy: exponential with jitter
```

Add to `$HERMES_HOME/.env`:

```
KIMI_BOT_TOKEN=km_b_prod_<your_token>
```

Restart the Hermes gateway. On boot you should see a log line like:

```
INFO hermes_cli.plugins: Plugin 'kimi' registered platform adapter: kimi
INFO hermes_plugins.kimi.kimi_adapter: Kimi: connected as <bot-name>
```

If you don't see the `register_platform_adapter` line, the hook isn't present in your Hermes Agent install — re-check the "Install Hermes Agent" section above.

## What the plugin does

| Surface | Mechanism | Notes |
|---|---|---|
| **DM** (1:1) | WebSocket to `wss://www.kimi.com/api-claw/bots/agent-ws` | Persistent connection, auto-reconnect with exponential backoff. Kimi closes idle WS after ~60s; the adapter handles this transparently. |
| **Group rooms** | Connect RPC over WebSocket to the same bot endpoint | Requires the `X-Kimi-OpenClaw-Version` header to be ≥ `2026.3.13` for group participation (the plugin sets this automatically). |
| **Slash commands** | Pass-through to the Hermes runtime | `/new`, `/compact`, `/status`, etc. handled at the runtime layer. |
| **Tool calls** | Native streaming via session/update | Tool-call frames are forwarded to the Kimi UI without being filtered. |
| **Output modes** | `output_mode: tool_only \| passthrough` | `tool_only` suppresses agent text in favour of `SendMessage` tool calls (matches the hakimi pattern). Default is `passthrough`. |
| **Onboarding skill** | Embedded `optional-skills/communication/kimi-platform/` | Once enabled in skill settings, agents get a brief on Kimi-specific behaviours (group vs DM, slash semantics, etc.). |

## Production reference

Bloom (Xule's home Raspberry Pi) has been running this adapter against real Kimi traffic:
- 2026-04-23 → 2026-04-26: in-tree at `gateway/platforms/kimi.py` on `linxule/hermes-agent:feat/kimi-platform-adapter`
- 2026-04-26 → 2026-04-27: as this plugin in-tree on `linxule/hermes-agent:feat/kimi-plugin-variant`
- 2026-04-27 → present: as this **standalone plugin** symlinked into `~/.hermes/plugins/kimi`, against `linxule/hermes-agent:feat/platform-kimi-enum` (the upstream-equivalent base for both prospective PRs)

Validated end-to-end via gateway logs showing `hermes_plugins.kimi.kimi_adapter` as the connecting module (i.e., the plugin path from this repo, not any in-tree fallback shim).

## Tests

```bash
# Install hermes-agent from the fork branch that has the registry hook + canonical Kimi wiring:
pip install 'hermes-agent @ git+https://github.com/linxule/hermes-agent.git@feat/platform-kimi-enum'

# Install plugin dev deps (pytest etc.) — the plugin itself is NOT pip-installed,
# the wheel is intentionally metadata-only. The actual plugin discovery happens
# via the $HERMES_HOME/plugins/kimi/ symlink described in the install section.
pip install -e '.[dev]'

# Run the unit suite:
pytest tests/
```

The unit tests are pure-function correctness checks (envelope codec, chat-id routing, dedup, MessageEvent synthesis, slash-command detection). Live-network tests against real Kimi are gated behind `KIMI_INTEGRATION_TOKEN` and skipped by default.

CI runs the suite against `hermes-agent` from the fork's `feat/platform-kimi-enum` branch (registry hook + `Platform.KIMI` enum, no plugin code in-tree — clean dependency target). See `.github/workflows/ci.yml`. The "kill-switch for upstream divergence" against vanilla `NousResearch/hermes-agent:main` is deferred until both upstream PRs merge — until then, vanilla upstream is *expected* to fail at plugin discovery with an actionable `ImportError`.

## Architecture notes

The plugin code lives in two files at the repo root:

- `__init__.py` — `register(ctx)` function, the only surface the Hermes plugin loader sees
- `kimi_adapter.py` — full `KimiAdapter(BasePlatformAdapter)` implementation (3,142 LoC)

`_compat/registry.py` is a vendored compatibility shim: it tries to import the upstream registry, and re-raises `ImportError` with an actionable message if the hook isn't present. This makes the plugin's requirements explicit at import time.

`tests/test_kimi.py` contains 154 unit tests covering the adapter; the matching plugin-integration tests (registry registration, dispatch, in-tree fallback) live in the in-fork suite at `tests/hermes_cli/test_kimi_plugin_integration.py` and are not duplicated here because they depend on the full Hermes plugin loader runtime.

## License

MIT — see [LICENSE](LICENSE).
