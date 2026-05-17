# hermes-kimi-plugin

Kimi (kimi.com / Moonshot AI) chat platform adapter for [Hermes Agent](https://github.com/NousResearch/hermes-agent), distributed as a standalone plugin.

Bridges Hermes Agent gateways to a single Kimi bot identity, handling **direct messages** (Zed ACP over WebSocket) and **group rooms** (Connect RPC over WebSocket) through one adapter — same model `KimiAdapter` Bloom has been running on production since 2026-04-23 (in-tree) and as a plugin since 2026-04-26.

## Status

> **v2.1.2 (2026-05-17) — `kimi_free_response_chats` exempt list for the mention gate.** Setting `group_require_mention=true` globally previously swallowed DM traffic because Kimi delivers 1:1 conversations as `room:<uuid>` indistinguishable from group rooms at the wire level. This release adds a Matrix/DingTalk-style per-chat allowlist (`kimi_free_response_chats`) so the user can require @-mentions in groups while keeping the DM free-response. Also accepts `group_require_mention_exempt_rooms` as an explicit alias. 7 new tests in `MentionGateExemptionTests`. See "Group participation" section below for the recommended config patterns including how this interacts with Kimi's platform-level "restricted vs. open visibility mode" routing. **Also**: hardened the `_install_fake_session` test fixture to pin `session.closed = False`, plugging a latent v2.1.1 issue where every backoff-test session was being silently replaced with a real `aiohttp.ClientSession()` because `MagicMock().closed` is truthy by default. The cross-loop helper's `getattr(cached, "closed", False)` check is correct for production (real aiohttp `.closed` is a proper bool); the fixture just needed to match that contract. Suite now 227/227 in <1s, up from 220 tests with intermittent timeouts.
>
> **v2.1.1 (2026-05-17) — cross-loop aiohttp session workaround.** v2.1.0 wired the `send_message` tool path for Kimi targets for the first time, which surfaced a latent cross-event-loop bug: `KimiAdapter.connect()` creates `self._http_session` on the gateway's main event loop, but Hermes's `send_message_tool` dispatches `adapter.send()` from a worker-thread event loop via `_run_async` → `worker_loop.run_until_complete`. aiohttp binds `ClientSession` to whichever loop is running at `__init__` time; using the session from a different loop later raises `RuntimeError("Timeout context manager should be used inside a task")` because `asyncio.current_task(loop=session._loop)` returns `None` from the worker loop. This release adds a `_session_for_current_loop()` async-context-manager helper that yields the cached session when the current loop matches and an ephemeral session bound to the current loop otherwise (connection-pool reuse preserved for normal traffic; single-connection cost on cross-loop calls). All five session call sites refactored. 4 new regression tests for `CrossLoopSessionTests`. An upstream issue tracks the broader fix in `tools/send_message_tool.py` — once landed, this plugin-side workaround becomes redundant but stays safe (idempotent when loops match).
>
> **v2.1.0 (2026-05-17) — out-of-process cron delivery + top-level YAML config bridge.** Three changes: (1) wired `standalone_sender_fn=_standalone_send` into `register()` so cron jobs and `send_message_tool` can deliver to Kimi rooms without a live in-process adapter (e.g. when cron runs in a separate process from the gateway); (2) fixed `env_enablement_fn` to seed `home_channel` as a dict (`{"chat_id": ..., "name": ...}`) matching upstream's `HomeChannel` contract — the previous string form silently failed the `isinstance(home, dict)` check in `gateway/config.py:1855-1871`, so cron home-channel delivery never worked despite `KIMI_HOME_CHANNEL` being set; (3) corrected `apply_yaml_config_fn` docstring + README to reflect that the bridge reads a **top-level `kimi:` block** (per `yaml_cfg.get(entry.name)` in `gateway/config.py:871`), not `platforms.kimi.*`. 5 new tests for the wrapper contract + end-to-end YAML→env→`HomeChannel` integration tests. All 216 tests pass.
>
> **v2.0.1 (2026-05-16) — `${VAR}` env-template resolution now also covers the standalone send path.** v2.0.0 wrapped the live-adapter `__init__` chain in a defensive `_resolve_env_template` helper so a `token: ${KIMI_BOT_TOKEN}` config.yaml line would resolve correctly even though Hermes does not invoke env-substitution for external-plugin `PlatformConfig.token`. Code review surfaced that the standalone `send_kimi_message()` helper (used by cron delivery and `send_message_tool` when no live adapter is available) still read `config.token` directly — meaning cron-driven kimi deliveries would 401 silently against a `${VAR}` config while the live bot path worked fine. This release wraps that path identically, adds 2 regression tests for the standalone surface, and hardens the existing adapter-init test against shell-env leakage by asserting on a UUID-derived sentinel.
>
> **v2.0.0 — runs against vanilla upstream Hermes Agent.** Teknium shipped the canonical platform-plugin API in commit [`2e20f6ae2`](https://github.com/NousResearch/hermes-agent/commit/2e20f6ae2) ("feat: complete plugin platform parity — all 12 integration points", v0.11.0, 2026-04-11) and the YAML-config bridge in [`3633c8690`](https://github.com/NousResearch/hermes-agent/commit/3633c8690) (`apply_yaml_config_fn` registry hook, v0.13.0, 2026-05-13). This release retires the fork-branch dependency and adopts those upstream extension points.
>
> The two PRs this plugin's earlier releases were carrying forward (`hook/platform-adapter-registry` + `feat/platform-kimi-enum`) have been retired — Teknium's `register_platform()` is the upstream equivalent and is strictly richer than what we were proposing. Historical fork branches are preserved as `archive/*` tags on [`linxule/hermes-agent`](https://github.com/linxule/hermes-agent) for reference.
>
> Production reference: Bloom (Xule's Pi) has been running this plugin continuously since 2026-04-27 — first against the fork branch, now against vanilla upstream main. Logger module name `hermes_plugins.kimi.kimi_adapter` in gateway logs confirms the plugin path is active end-to-end.

## Install

You'll need:
1. A Hermes Agent install that includes the platform-adapter registry hook AND the canonical Kimi wiring (see "Install Hermes Agent" below)
2. A Kimi bot token from your kimi.com account (Settings → Connected Apps → Generate `km_b_prod_…` token)

### Install Hermes Agent

Vanilla upstream `hermes-agent` ≥ 0.13.0 is sufficient. Both prerequisites (`ctx.register_platform()` + `apply_yaml_config_fn`) are upstream:

```bash
pip install 'hermes-agent>=0.13.0'
```

Or install from upstream main directly:

```bash
pip install 'hermes-agent @ git+https://github.com/NousResearch/hermes-agent.git@main'
```

Earlier fork branches (`hook/platform-adapter-registry`, `feat/platform-kimi-enum`, `feat/kimi-plugin-variant`) are retired — see `archive/*` tags on `linxule/hermes-agent` if you need historical inspection.

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
    # Optional raw PlatformConfig.extra values. Hermes passes these through
    # without ${VAR} expansion; the adapter defensively resolves full
    # ${KIMI_BOT_TOKEN} literals in token fields at runtime.
    extra:
      bot_token: ${KIMI_BOT_TOKEN}
      # See kimi_adapter.py for the full shape.
    # Defaults are tuned for Bloom's Pi deployment:
    #   home_channel: nag-once-per-session
    #   subscribe_backoff: 2s → 60s exponential
    #   reconnect_strategy: exponential with jitter
```

For env-first setups, add to `$HERMES_HOME/.env`:

```
KIMI_BOT_TOKEN=km_b_prod_<your_token>
```

For profile configs that should avoid a per-profile `.env`, use the plugin's
top-level YAML bridge instead:

```yaml
kimi:
  bot_token: km_b_prod_<your_token>
  home_channel: room:<uuid>
  allowed_users:
    - km_u_<uuid>
```

Hermes calls the bridge with `config.yaml`'s top-level `kimi:` block. It does
not call the bridge for `platforms.kimi.extra`; those values arrive as raw
`PlatformConfig.extra` data.

Restart the Hermes gateway. On boot you should see a log line like:

```
INFO hermes_cli.plugins: Plugin 'kimi' registered platform: kimi
INFO hermes_plugins.kimi.kimi_adapter: Kimi: connected as <bot-name>
```

If you don't see the `registered platform: kimi` line, the loader didn't pick up the plugin — check that the symlink at `$HERMES_HOME/plugins/kimi` resolves to this repo's `kimi/` subdirectory and that `plugins.enabled: [- kimi]` is set in `config.yaml`.

### Home channel (optional)

Hermes models a **home channel per platform**. Each platform you connect has its own, independent of the others — Telegram's home doesn't affect Kimi's, Discord's doesn't affect either. Setting `/sethome` in one platform does not propagate to another.

For Kimi specifically, the home channel is **optional**. The platform works fully without one — direct conversation (inbound messages + agent replies) doesn't consult the home channel at all. What you gain by setting one:

| Trigger | Effect with `KIMI_HOME_CHANNEL` set | Effect without it |
|---|---|---|
| User-driven `/restart` slash command | Posts `♻️ Gateway online — Hermes is back and ready.` to the home channel after the gateway reboots | No Kimi-side notification (Telegram / Discord still notify if they have their own homes) |
| Gateway shutdown | Posts shutdown notice to the home channel | No Kimi-side notification |
| Cron job with `deliver: kimi` and no explicit `chat_id` | Routes to the home channel | Cron job's Kimi delivery fails (you need either a home or an explicit target) |
| Anything calling `get_home_channel(Platform("kimi"))` | Returns the configured `HomeChannel` dataclass | Returns `None` |

#### Two ways to set it

```bash
# Option 1: env var (set once, persists across restarts)
echo 'KIMI_BOT_TOKEN=km_b_prod_...' >> $HERMES_HOME/.env
echo 'KIMI_HOME_CHANNEL=room:<uuid>' >> $HERMES_HOME/.env
```

```
# Option 2: /sethome slash command, run from inside the target Kimi chat
/sethome
```

Both end up writing the same `KIMI_HOME_CHANNEL=...` line to `$HERMES_HOME/.env`. The slash command path is preferred because it pulls the current `chat_id` from the message context — no manual UUID copying.

#### Format of the chat_id

Kimi's wire model wraps **every** conversation behind a `room:<uuid>` prefix, including your 1:1 chat with the bot. There is no separate "DM" identifier shape for user↔bot conversations on Kimi — the 1:1 is structurally a 2-person room from kimi.com's perspective.

So a valid `KIMI_HOME_CHANNEL` looks like:

```
KIMI_HOME_CHANNEL=room:19e31a29-4722-8804-8000-094a7731741b
```

The UUID is owned by kimi.com — get it from your gateway's `inbound message` log line for the target chat (`chat=room:<uuid>`), or rely on `/sethome` to pick it up for you.

### ⚠️ Bot identity rotation invalidates the home channel

The room UUIDs kimi.com assigns to a 1:1 conversation are **derived from the bot's identity** (same UUIDv7 timestamp + identity bits as the bot's user UUID). When the bot's identity rotates — typically because you regenerated the bot token via kimi.com's "Connected Apps → Generate token" flow, or because the underlying claw runtime cycled the bot's user account — kimi.com starts a **new** 1:1 room for the new bot, with a new UUID.

`KIMI_HOME_CHANNEL` in `.env` is **not** automatically updated when this happens. The env var keeps pointing at the old room, which the new bot identity has no membership in. Symptoms:

- Restart / shutdown notifications go to a room nobody is in (silent void; no error visible in logs because the send succeeds at the API level)
- Cron deliveries with `deliver: kimi` look like they succeeded (HTTP 200 from kimi.com) but the recipient sees nothing
- The first hint is usually a user noticing they stopped seeing gateway-status pings

**Diagnose** by comparing the bot's `id=...` in the `Kimi: connected as ...` log line against the UUID stem in `KIMI_HOME_CHANNEL`. If the first segment differs (e.g. bot reports `id=19e31a29-...` but the home channel says `room:19dbb6a7-...`), the home channel is stale.

**Recover** by either:

1. Running `/sethome` from inside the live 1:1 DM with the new bot identity (recommended — the new chat_id gets written to `.env` automatically), OR
2. Unsetting `KIMI_HOME_CHANNEL` entirely if you don't need a Kimi-side home (`sed -i '/^KIMI_HOME_CHANNEL=/d' $HERMES_HOME/.env`), then restarting the gateway.

Either way, restart the gateway to pick up the new `.env` value (or the absence of one).

### Group participation: mention gate + exempt list

Kimi's wire model is one of the awkward facts about this platform: **1:1 conversations and group rooms both use `room:<uuid>`** with no field distinguishing them. From the adapter's perspective every inbound message looks like "a message from some room." This breaks the usual "DM = respond freely, group = require @-mention" split that platforms like Telegram, Discord, and Matrix can encode at the wire level.

The plugin handles it with two cooperating layers:

1. **`group_require_mention`** (boolean, default `false`) — adapter-wide gate. When `true`, any message that doesn't @-mention the bot is dropped before reaching Hermes.
2. **`kimi_free_response_chats`** (list of `room:<uuid>`, default empty) — per-chat exempt list. Rooms in this list bypass the mention gate even when `group_require_mention=true`. This is where you put the DM room.

#### Interaction with Kimi's platform-level visibility mode

Kimi.com has a **platform-level routing setting** that operates *above* the adapter. As Kimi's group conductor describes it:

> in restricted mode, the platform literally only delivers a given message to the members who are @mentioned in it (plus the conductor). in open mode, the platform delivers every main chat message to every member in the group.
>
> so for an external agent with its own gateway — like your Pi in London — the gateway receives whatever the platform sends it. the agent's local settings determine what it actually does with those messages (auto-respond, only respond when addressed, ignore unless tagged, etc.), but the platform setting controls what the gateway receives in the first place.

This means `group_require_mention=true` is a **defense-in-depth** filter, not a stand-alone gate:

- **Restricted-mode groups**: Kimi pre-filters at the platform. The gateway only receives @-mentions of the bot. `group_require_mention=true` matches every incoming message and is effectively a no-op; setting it costs nothing but isn't required.
- **Open-mode groups**: Kimi delivers everything. The bot decides via `group_require_mention`. This is the case where the local gate earns its keep.
- **1:1 DMs**: not affected by visibility mode (it's a 2-person room). Always delivered.

#### Recommended configurations

| Use case | Kimi room mode | `group_require_mention` | `kimi_free_response_chats` |
|---|---|---|---|
| 1:1 DM only, no groups | n/a | doesn't matter | unset |
| 1:1 DM + open-mode groups, stay quiet unless tagged | open | `true` | `[room:<dm-uuid>]` |
| 1:1 DM + restricted-mode groups | restricted | doesn't matter | unset (DM and group both pre-filtered correctly) |
| 1:1 DM + free-response open-mode groups | open | `false` | unset |

The most defensive pattern when you don't know your peers' room-mode preferences is **option 2**: `group_require_mention=true` + DM exempted. It works correctly regardless of whether the groups are restricted or open mode, because either Kimi pre-filters (and the local gate doesn't fire) or Kimi delivers everything (and the local gate handles it).

#### Example config

```yaml
platforms:
  kimi:
    enabled: true
    extra:
      group_require_mention: true
      kimi_free_response_chats:
        - room:19e31a29-4722-8804-8000-094a7731741b   # 1:1 DM with the user
        # Add additional room UUIDs here if you want certain groups to be
        # free-response regardless of the global mention gate.
```

The room UUIDs come from the same place as `KIMI_HOME_CHANNEL` — your gateway's `inbound message` log line (`chat=room:<uuid>`). The plugin tolerates `group_require_mention_exempt_rooms` as an alias if you find that name more explicit.

## What the plugin does

| Surface | Mechanism | Notes |
|---|---|---|
| **DM** (1:1) | WebSocket to `wss://www.kimi.com/api-claw/bots/agent-ws` | Persistent connection, auto-reconnect with exponential backoff. Kimi closes idle WS after ~60s; the adapter handles this transparently. |
| **Group rooms** | Connect RPC over WebSocket to the same bot endpoint | Requires the `X-Kimi-OpenClaw-Version` header to be ≥ `2026.3.13` for group participation (the plugin sets this automatically). |
| **Slash commands** | Pass-through to the Hermes runtime | `/new`, `/compact`, `/status`, etc. handled at the runtime layer. |
| **Tool calls** | Native streaming via session/update | Tool-call frames are forwarded to the Kimi UI without being filtered. |
| **Output modes** | `output_mode: tool_only \| passthrough` | `tool_only` suppresses agent text in favour of `SendMessage` tool calls (matches the hakimi pattern). Default is `passthrough`. See [Picking an output_mode](#picking-an-output_mode) below. |
| **Bounded room state** | `_BoundedLRU(maxsize=N)` on four per-room dicts | `_rooms`, `_last_message_id_per_room`, `_probe_msg_id_room_counts` share `room_cache_max_entries` (default 500); `_last_arrival_time_per_room` uses its own larger `arrival_time_cache_max_entries` (default 10000) because eviction would silently blind gap detection. Eviction never affects message-dispatch correctness — replay dedup is owned by `_processed_set`, separately bounded. See [Bounded room state](#bounded-room-state) below. |
| **Onboarding skill** | Embedded `optional-skills/communication/kimi-platform/` | Once enabled in skill settings, agents get a brief on Kimi-specific behaviours (group vs DM, slash semantics, etc.). |

### Picking an `output_mode`

Hermes routes outbound messages to Kimi via two distinct paths:

| # | Path | Triggered by | Gated by `output_mode`? |
|---|---|---|---|
| 1 | `adapter.send()` | The gateway run-loop streaming agent prose; AND `send_message_tool` when a live adapter exists (upstream dispatch order — see [⚠️ Known limitation](#-known-limitation-tool_only--send_message_tool) below) | **Yes** |
| 2 | `send_kimi_message()` (module-level helper) | `standalone_sender_fn` route — cron scheduler delivery when no live adapter is in-process; and `send_message_tool` as a fallback if the live adapter is unavailable | **No** |

Modes:

- **`passthrough`** *(default)* — both paths emit. Agent prose streams to Kimi as it's generated, plus tool-driven and cron sends. Matches every other Hermes platform adapter.
- **`tool_only`** — path 1 is suppressed. Agent prose stays in Hermes logs but never reaches Kimi. Useful for cron-only or batch deployments where the agent's streaming text isn't wanted at the platform layer.

#### ⚠️ Known limitation: `tool_only` + `send_message_tool`

Against current upstream Hermes (`NousResearch/hermes-agent` ≤ 0.14.0), `send_message_tool._send_via_adapter` prefers the live adapter (`adapter.send()`) when one exists. That means explicit `send_message_tool` calls go through **Path 1** in practice, not Path 2 — so in `tool_only` mode they're silently suppressed alongside the gateway prose.

In practice this affects:

- **In-process gateways with `tool_only`**: agent's explicit tool sends to Kimi disappear (the adapter's `send()` returns `SendResult(success=True)` without dispatching). No error, no log warning.
- **In-process gateways with `passthrough`** *(the default)*: unaffected — tool sends work normally because Path 1 isn't suppressed.
- **Out-of-process cron with `tool_only`**: cron delivery still works because no live adapter is present, so dispatch falls through to `standalone_sender_fn` (Path 2).

The right architectural fix is upstream: `send_message_tool` should prefer the registered `standalone_sender_fn` over the live adapter when one exists, since `standalone_sender_fn` is the plugin's documented "explicit-send" interface. An issue is staged for this at [`linxule/clawd-world:.research/upstream-issue-text/cross-loop-send-message-tool.md`](https://github.com/linxule/clawd-world) (not yet filed against upstream).

Until that lands, two workarounds for `tool_only` deployments that need explicit tool sends:

1. **Run cron out-of-process from the gateway** (separate `hermes cron` runner). Cron deliveries route via `standalone_sender_fn` and bypass the suppressed live-adapter `send()` cleanly.
2. **Stay on `passthrough`** unless you've verified that your specific deployment shape doesn't depend on `send_message_tool` to Kimi.

When `tool_only` is still the right choice (with the caveat above understood):

- **Group rooms** where streaming prose would be noisy and the agent should emit a single curated reply via `send_message_tool` AND that delivery happens via out-of-process cron OR a future upstream Hermes that prefers `standalone_sender_fn`.
- **Cron-only deployments** with no interactive turns and no in-process `send_message_tool` usage.

When to stay on `passthrough` *(default)*:

- **1:1 DMs** where the user expects streaming-response UX — silence under `tool_only` looks indistinguishable from a hung bot.
- **Setups where the agent isn't reliably guided** (system prompt or skill nudge) to call `send_message_tool`. Without that guidance, `tool_only` makes the bot appear mute on every turn.
- **Any in-process gateway** with mixed `send_message_tool` + cron usage until upstream prefers `standalone_sender_fn`.

This flag exists because the bridge's previous workaround (`HIDE_TOOL_CALLS=1`) hung Hermes over stdio. The adapter's in-process coupling lets the suppression happen at the right layer without that deadlock.

### Bounded room state

Four of the adapter's per-room dicts are backed by `_BoundedLRU` so cardinality can't grow without ceiling on long-running deployments:

- **Shared cap** (`room_cache_max_entries`, default 500) — `_rooms`, `_last_message_id_per_room`, `_probe_msg_id_room_counts`. All three share the same `room_id` key space and have non-load-bearing eviction semantics.
- **Larger separate cap** (`arrival_time_cache_max_entries`, default 10000) — `_last_arrival_time_per_room`. Eviction would silently blind the Phase 0 gap-candidate log (next message produces `prev_arrival=None` and the INFO log won't fire regardless of actual delay). Codex review #58 flagged this as a contradiction with the "no load-bearing state" promise; the separate higher cap resolves it without making memory unbounded — 10000 entries × ~16 bytes ≈ 160KB, effectively unbounded for any realistic deployment.

**What eviction never breaks:** message-dispatch correctness. Replay dedup is owned by `_processed_set`, which is bounded by `_DEDUP_MAXLEN=2000` independently. Whether a room's metadata is in `_rooms` or not has zero bearing on whether the agent sees a duplicate or dropped message.

**What eviction *does* affect** under cardinality pressure (≥ shared cap rooms ever encountered):

1. **`first-seen` DEBUG log on resumed rooms.** `_last_message_id_per_room` is hoisted out of the DEBUG gate so toggling DEBUG on later doesn't produce a misleading `first-seen`. Eviction reintroduces a narrower version of that artifact: a quiet room evicted then resumed will log `first-seen` instead of a delta on its first message back. Misleading observability, not misleading state.
2. **Probe sample-phase reset.** `_probe_msg_id_room_counts` keys its DEBUG sampling phase off the count modulo `probe_msg_id_sample_rate`. After eviction the count restarts at 1, so the first `sample_rate - 1` resumed messages are skipped from the DEBUG sample.
3. **Cold-resume RPC failure.** `_rooms` re-fetches a missing entry via `GetRoom` + `ListMembers`; on transient `KimiAdapterError` the fallback returns `{"name": room_id, "type": "group"}` with no members. Without eviction, that fallback only runs for *never-cached* rooms; with eviction, a failed re-fetch on a previously-cached room can briefly degrade display name + members until the next successful refresh.

For Bloom's typical Pi deployment (~10 unique rooms over weeks) none of these ever fire. For deployments with hundreds-to-thousands of rooms, raise the caps:

```yaml
platforms:
  kimi:
    extra:
      room_cache_max_entries: 5000
      arrival_time_cache_max_entries: 50000   # only if >10000 distinct rooms
```

## Production reference

Bloom (Xule's home Raspberry Pi) has been running this adapter against real Kimi traffic:
- 2026-04-23 → 2026-04-26: in-tree at `gateway/platforms/kimi.py` on `linxule/hermes-agent:feat/kimi-platform-adapter`
- 2026-04-26 → 2026-04-27: as this plugin in-tree on `linxule/hermes-agent:feat/kimi-plugin-variant`
- 2026-04-27 → 2026-05-16: as this **standalone plugin** symlinked into `~/.hermes/plugins/kimi`, against `linxule/hermes-agent:feat/platform-kimi-enum` (the fork branch carrying our proposed `register_platform_adapter` hook + `Platform.KIMI` enum + canonical wiring)
- 2026-05-16 → present: as this **standalone plugin** against **vanilla upstream Hermes Agent main**, using Teknium's `register_platform()` API. The fork dependency is retired.

Validated end-to-end via gateway logs showing `hermes_plugins.kimi.kimi_adapter` as the connecting module (i.e., the plugin path from this repo, not any in-tree fallback shim).

## Diagnostics

These are **observability** signals, not capabilities — operators read them to diagnose, but the plugin never recovers messages on the basis of them. Recovery design (Phase 1+) is gated on a release cycle of these signals from real production data.

### Burst-drop instrumentation (Phase 0)

Three INFO-level signals gather evidence about message-loss patterns *without* recovering anything.

**1. Gap-candidate INFO log.** Tracks **monotonic arrival time** per room (`time.monotonic()` at the moment each `chatMessage` is processed by the adapter). When the delta between consecutive arrivals in a room is `≥ burst_drop_gap_log_threshold_s` (default 30s, set to 0 to disable):

```
INFO Kimi groups: gap candidate room=<chat_id> id=<this_id> prev=<prev_id> delta_s=N.N (>=30.0s threshold) since_reconnect_s=K.K connect#=M
```

`time.monotonic()` is the right primitive for "how long since last delivery" — it never goes backward, never jumps on NTP sync or VM suspend/resume, and is immune to leap-second adjustments. Wall-clock correlation comes from the log line's own leading ISO timestamp; the embedded `delta_s` is honest process-time. (Earlier wall-clock implementation switched to monotonic in #58 cumulative review after independent flags from Codex and Kimi reviewers.)

This is anomaly-spotting only — most gaps are legitimate (idle room, no traffic). The signal becomes useful when **clusters** of these correlate against reconnects (low `since_reconnect_s` values in burst). At Bloom's typical traffic patterns the default 30s threshold may produce one-off false positives from human conversational pauses; raise to 60-120s if INFO noise becomes distracting.

Also note: Kimi's production message ids are UUID v8 with a non-standard epoch (their first 48 bits decode to magnitudes ~16× wall-clock seconds), so id-based timestamps are unreliable for time-domain analysis. That's why the gap delta is wall-time-based, not id-derived.

**2. Reconnect counter + snapshot log.** On the first `chatMessage` post-(re)connect, after the counter is bumped (the bump is now pre-dispatch in #58 to keep the gap log's `connect#` consistent with the snapshot log):

```
INFO Kimi groups: subscribe stream live (connect#N, rooms_tracked=M, prev_backoff=X.Xs)
```

`connect#N` is monotonic over the adapter lifetime (cold start = #1). The same value also appears in every gap-candidate log line for in-line correlation.

**3. Paginated `list_group_messages`.** The wrapper follows `nextPageToken` up to a configurable `max_pages` (default 1, backward-compatible with every existing caller). Anchor IDs (`startMessageId` / `endMessageId`) are sent only on the first request — subsequent requests carry the opaque cursor only, avoiding the duplicate-results / undefined-ordering risk Kimi review #58 flagged. Required for any future recovery design that fetches more than `limit` messages from a gap.

Tunable via `config.extra`:

```yaml
platforms:
  kimi:
    extra:
      burst_drop_gap_log_threshold_s: 30.0   # 0 disables the gap-candidate INFO
      arrival_time_cache_max_entries: 10000   # see Bounded room state above
      # (no config knob for the reconnect counter — always on)
      # (max_pages is per-call; default 1)
```

Recovery designs (Phase 1+) require a release cycle of this data first to choose between reconnect catch-up vs periodic poll vs gap-triggered.

## Tests

```bash
# Install hermes-agent from upstream main (v0.13.0+ is the floor — see pyproject.toml):
pip install 'hermes-agent @ git+https://github.com/NousResearch/hermes-agent.git@main'

# Install plugin dev deps (pytest etc.) — the plugin itself is NOT pip-installed,
# the wheel is intentionally metadata-only. The actual plugin discovery happens
# via the $HERMES_HOME/plugins/kimi/ symlink described in the install section.
pip install -e '.[dev]'

# Run the unit suite:
pytest tests/
```

The unit tests are pure-function correctness checks (envelope codec, chat-id routing, dedup, MessageEvent synthesis, slash-command detection). Live-network tests against real Kimi are gated behind `KIMI_INTEGRATION_TOKEN` and skipped by default.

CI runs the suite against **vanilla upstream `NousResearch/hermes-agent:main`** — see `.github/workflows/ci.yml`. The daily scheduled run at 06:00 UTC doubles as a kill-switch for upstream drift: if a future commit on upstream main removes or renames a symbol this plugin depends on, the CI fails and we get the signal before production does.

## Architecture notes

The plugin code lives in two files at the repo root:

- `__init__.py` — `register(ctx)` function, the only surface the Hermes plugin loader sees
- `kimi_adapter.py` — full `KimiAdapter(BasePlatformAdapter)` implementation (3,142 LoC)

`_compat/registry.py` is a vendored compatibility shim: it tries to import the upstream registry, and re-raises `ImportError` with an actionable message if the hook isn't present. This makes the plugin's requirements explicit at import time.

`tests/test_kimi.py` contains 154 unit tests covering the adapter; the matching plugin-integration tests (registry registration, dispatch, in-tree fallback) live in the in-fork suite at `tests/hermes_cli/test_kimi_plugin_integration.py` and are not duplicated here because they depend on the full Hermes plugin loader runtime.

## License

MIT — see [LICENSE](LICENSE).
