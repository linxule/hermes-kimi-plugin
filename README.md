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
| **Output modes** | `output_mode: tool_only \| passthrough` | `tool_only` suppresses agent text in favour of `SendMessage` tool calls (matches the hakimi pattern). Default is `passthrough`. See [Picking an output_mode](#picking-an-output_mode) below. |
| **Bounded room state** | `_BoundedLRU(maxsize=N)` on `_rooms`, `_last_message_id_per_room`, `_probe_msg_id_room_counts` | Per-room dicts cap at `room_cache_max_entries` entries (default 500). Eviction does NOT affect message-dispatch correctness (replay-dedup is owned by `_processed_set`, separately bounded). It does have observable side effects under cardinality pressure — see [Bounded room state](#bounded-room-state) below. |
| **Burst-drop instrumentation** | Gap-candidate INFO logs + reconnect counter + paginated `list_group_messages` | Gathers evidence for whether suspicious per-room timing gaps cluster around stream reconnects. Does NOT recover messages — that's a future Phase 1+. See [Burst-drop instrumentation](#burst-drop-instrumentation) below. |
| **Onboarding skill** | Embedded `optional-skills/communication/kimi-platform/` | Once enabled in skill settings, agents get a brief on Kimi-specific behaviours (group vs DM, slash semantics, etc.). |

### Picking an `output_mode`

Hermes routes outbound messages to Kimi via two distinct paths, and `output_mode` only gates the first:

| # | Path | Triggered by | Gated by `output_mode`? |
|---|---|---|---|
| 1 | `adapter.send()` | The gateway run-loop, for each chunk of agent prose during a streaming turn | **Yes** |
| 2 | `send_kimi_message()` (module-level helper) | The `send_message_tool` (agent's explicit tool call) and the cron scheduler's Kimi delivery path | **No** |

Modes:

- **`passthrough`** *(default)* — both paths emit. Agent prose streams to Kimi as it's generated, plus tool-driven and cron sends. Matches every other Hermes platform adapter.
- **`tool_only`** — path 1 is suppressed. Agent prose stays in Hermes logs but never reaches Kimi. Path 2 is unaffected: explicit `send_message_tool` calls and cron-delivered messages still appear normally. The user only sees output that the agent decided to emit via the tool.

When `tool_only` is the right choice:
- **Group rooms** where streaming prose would be noisy and the agent should emit a single curated reply.
- **Multi-step agents** where intermediate "thinking out loud" is undesirable platform-side but useful in logs.
- **Cron-only or tool-only deployments** with no interactive turns.

When to stay on `passthrough`:
- **1:1 DMs** where the user expects streaming-response UX — silence under `tool_only` looks indistinguishable from a hung bot.
- **Setups where the agent isn't reliably guided** (system prompt or skill nudge) to call `send_message_tool`. Without that guidance, `tool_only` makes the bot appear mute on every turn.

This flag exists because the bridge's previous workaround (`HIDE_TOOL_CALLS=1`) hung Hermes over stdio. The adapter's in-process coupling lets the suppression happen at the right layer without that deadlock.

### Bounded room state

Three of the adapter's per-room dicts (`_rooms`, `_last_message_id_per_room`, `_probe_msg_id_room_counts`) used to grow without ceiling on long-running deployments. They're now backed by `_BoundedLRU` with a configurable cap (`room_cache_max_entries`, default 500). The cap is **per-dict**, but all three share the same `room_id` key space so cardinality is symmetric.

**What eviction never breaks:** message-dispatch correctness. Replay dedup is owned by `_processed_set`, which is bounded by `_DEDUP_MAXLEN=2000` independently. Whether a room's metadata is in `_rooms` or not has zero bearing on whether the agent sees a duplicate or dropped message.

**What eviction *does* affect** under cardinality pressure (≥ cap rooms ever encountered):

1. **`first-seen` DEBUG log on resumed rooms.** `_last_message_id_per_room` is hoisted out of the DEBUG gate so toggling DEBUG on later doesn't produce a misleading `first-seen`. Eviction reintroduces a narrower version of that artifact: a quiet room evicted then resumed will log `first-seen` instead of a delta on its first message back. Misleading observability, not misleading state.
2. **Probe sample-phase reset.** `_probe_msg_id_room_counts` keys its DEBUG sampling phase off the count modulo `probe_msg_id_sample_rate`. After eviction the count restarts at 1, so the first `sample_rate - 1` resumed messages are skipped from the DEBUG sample.
3. **Cold-resume RPC failure.** `_rooms` re-fetches a missing entry via `GetRoom` + `ListMembers`; on transient `KimiAdapterError` the fallback returns `{"name": room_id, "type": "group"}` with no members. Without eviction, that fallback only runs for *never-cached* rooms; with eviction, a failed re-fetch on a previously-cached room can briefly degrade display name + members until the next successful refresh.

For Bloom's typical Pi deployment (~10 unique rooms over weeks) none of these ever fire. For deployments with hundreds-to-thousands of rooms, raise the cap:

```yaml
platforms:
  kimi:
    extra:
      room_cache_max_entries: 5000
```

### Burst-drop instrumentation

Three INFO-level signals gather evidence about message-loss patterns *without* recovering anything. They exist so a future Phase 1+ recovery design can be picked against real production data instead of assumed traffic patterns.

**1. Gap-candidate INFO log.** Tracks **wall-clock arrival time** per room (`time.time()` at the moment each `chatMessage` is processed by the adapter). When the delta between consecutive arrivals in a room is `≥ burst_drop_gap_log_threshold_s` (default 30s, set to 0 to disable):

```
INFO Kimi groups: gap candidate room=<chat_id> id=<this_id> prev=<prev_id> delta_s=N.N (>=30.0s threshold) since_reconnect_s=K.K connect#=M
```

Note: this is **wall-clock arrival delta**, not Kimi-`messageId`-embedded timestamp delta. Kimi's production message ids are UUID v8 with a non-standard epoch (their first 48 bits decode to magnitudes ~16× wall-clock seconds), so id-based timestamps are unreliable for time-domain analysis. Wall-clock at receive time is the operator-meaningful signal.

The `since_reconnect_s` and `connect#` fields make correlation against reconnects implicit in a single line — operators don't need to grep two log streams.

This is anomaly-spotting only — most gaps are legitimate (idle room, no traffic). The signal becomes useful when **clusters** of these correlate against reconnects (low `since_reconnect_s` values in burst). At Bloom's typical traffic patterns the default 30s threshold may produce one-off false positives from human conversational pauses; raise to 60-120s if INFO noise becomes distracting.

**2. Reconnect counter + snapshot log.** On the first `chatMessage` post-(re)connect:

```
INFO Kimi groups: subscribe stream live (connect#N, rooms_tracked=M, prev_backoff=X.Xs)
```

`connect#N` is monotonic over the adapter lifetime (cold start = #1). The same value also appears in every gap-candidate log line for in-line correlation.

**3. Paginated `list_group_messages`.** The wrapper now follows `nextPageToken` up to a configurable `max_pages` (default 1, backward-compatible with every existing caller). Required for any future recovery design that fetches more than `limit` messages from a gap.

Tunable via `config.extra`:

```yaml
platforms:
  kimi:
    extra:
      burst_drop_gap_log_threshold_s: 30.0   # 0 disables the gap-candidate INFO
      # (no config knob for the reconnect counter — always on)
      # (max_pages is per-call; default 1)
```

This is **evidence-gathering**, not recovery. Recovery designs (Phase 1+) require a release cycle of this data first to choose between reconnect catch-up vs periodic poll vs gap-triggered.

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
