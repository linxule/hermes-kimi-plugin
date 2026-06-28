# hermes-kimi-plugin

Kimi (kimi.com / Moonshot AI) chat platform adapter for [Hermes Agent](https://github.com/NousResearch/hermes-agent), distributed as a standalone plugin.

Bridges Hermes Agent gateways to a single Kimi bot identity, handling **direct messages** (Zed ACP over WebSocket) and **group rooms** (Connect RPC over WebSocket) through one adapter — same model `KimiAdapter` Bloom has been running on production since 2026-04-23 (in-tree) and as a plugin since 2026-04-26.

## Status

Current version: **2.2.2**. Targets vanilla upstream `NousResearch/hermes-agent` ≥ 0.14.0 (uses `ctx.register_platform()` and `apply_yaml_config_fn`). See [CHANGELOG.md](CHANGELOG.md) for the full release history.

Production reference: the plugin has been running continuously on a long-lived Raspberry Pi deployment since 2026-04-27, first against a fork branch and now against vanilla upstream `main`. Gateway log lines like `Plugin 'kimi' registered platform: kimi` and `hermes_plugins.kimi.kimi_adapter: Kimi: connected as <bot-name>` confirm the plugin path is active end-to-end.

## Install

You'll need:
1. A Hermes Agent install that includes the platform-adapter registry hook AND the canonical Kimi wiring (see "Install Hermes Agent" below)
2. A Kimi bot token from your kimi.com account (Settings → Connected Apps → Generate `km_b_prod_…` token)

### Install Hermes Agent

Vanilla upstream `hermes-agent` ≥ 0.14.0 is sufficient. Both prerequisites (`ctx.register_platform()` + `apply_yaml_config_fn`) are upstream.

v0.14.0 (the Foundation Release, tag `v2026.5.16`) is the floor. As of 2026-05-18 the v0.14.0 wheel has not yet been published to PyPI — until that lands, install from the git tag:

```bash
pip install 'hermes-agent @ git+https://github.com/NousResearch/hermes-agent.git@v2026.5.16'
```

Once v0.14.0 reaches PyPI:

```bash
pip install 'hermes-agent>=0.14.0'
```

Or always install from upstream main directly:

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

### Behind a corporate / institutional proxy (v2.1.6+)

If you run the gateway behind an HTTP proxy (corporate network, institutional firewall, anything that requires `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` env vars), the plugin honours those env vars automatically from v2.1.6 onwards via `aiohttp`'s `trust_env=True`. On v2.1.5 and earlier the plugin silently bypassed the proxy and outbound requests to `kimi.com` failed with bare connection errors. If you see "cannot connect to kimi.com" symptoms on a network that requires a proxy, upgrade.

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
KIMI_HOME_CHANNEL=room:<uuid>
```

The UUID is owned by kimi.com — get it from your gateway's `inbound message` log line for the target chat (`chat=room:<uuid>`), or rely on `/sethome` to pick it up for you.

### ⚠️ Bot identity rotation invalidates the home channel

The room UUIDs kimi.com assigns to a 1:1 conversation are **derived from the bot's identity** (same UUIDv7 timestamp + identity bits as the bot's user UUID). When the bot's identity rotates — typically because you regenerated the bot token via kimi.com's "Connected Apps → Generate token" flow, or because the underlying claw runtime cycled the bot's user account — kimi.com starts a **new** 1:1 room for the new bot, with a new UUID.

`KIMI_HOME_CHANNEL` in `.env` is **not** automatically updated when this happens. The env var keeps pointing at the old room, which the new bot identity has no membership in. Symptoms:

- Restart / shutdown notifications go to a room nobody is in (silent void; no error visible in logs because the send succeeds at the API level)
- Cron deliveries with `deliver: kimi` look like they succeeded (HTTP 200 from kimi.com) but the recipient sees nothing
- The first hint is usually a user noticing they stopped seeing gateway-status pings

**Diagnose** by comparing the bot's `id=...` in the `Kimi: connected as ...` log line against the UUID stem in `KIMI_HOME_CHANNEL`. If the first segment differs (e.g. bot reports `id=AAAAAAAA-...` but the home channel says `room:BBBBBBBB-...`), the home channel is stale.

**Recover** by either:

1. Running `/sethome` from inside the live 1:1 DM with the new bot identity (recommended — the new chat_id gets written to `.env` automatically), OR
2. Unsetting `KIMI_HOME_CHANNEL` entirely if you don't need a Kimi-side home (`sed -i '/^KIMI_HOME_CHANNEL=/d' $HERMES_HOME/.env`), then restarting the gateway.

Either way, restart the gateway to pick up the new `.env` value (or the absence of one).

### Group participation: mention gate + exempt list

Kimi's wire model is one of the awkward facts about this platform: **1:1 conversations and group rooms both use `room:<uuid>`** with no field distinguishing them. From the adapter's perspective every inbound message looks like "a message from some room." This breaks the usual "DM = respond freely, group = require @-mention" split that platforms like Telegram, Discord, and Matrix can encode at the wire level.

The plugin handles it with three cooperating layers:

1. **`group_require_mention`** (boolean, default `false`) — adapter-wide gate. When `true`, any message that doesn't @-mention the bot is dropped before reaching Hermes.
2. **`kimi_free_response_chats`** (list of `room:<uuid>`, default empty) — per-chat exempt list. Rooms in this list bypass the mention gate even when `group_require_mention=true`. Use for **explicit operator policy** — "this small trusted group room should be free-response despite the global gate."
3. **`kimi_dm_autodetect`** (boolean, default `false`, added in **v2.2.0**) — auto-bypass the gate for rooms structurally identifiable as DMs (exactly 2 members: bot + user). When `true`, the adapter calls `list_group_members` on rooms about to be dropped by the gate; 2-member rooms get auto-bypassed without operator config. Off by default for v2.2.0 — opt-in until stabilization; a future release may flip the default.

The two config knobs serve different purposes and can be combined: `kimi_dm_autodetect=true` removes the need to list DM UUIDs in `kimi_free_response_chats` manually; the exempt list is still needed for explicit free-response groups (use case 2).

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
        - room:<dm-uuid>   # 1:1 DM with the user
        # Add additional room UUIDs here if you want certain groups to be
        # free-response regardless of the global mention gate.
```

The room UUIDs come from the same place as `KIMI_HOME_CHANNEL` — your gateway's `inbound message` log line (`chat=room:<uuid>`). The plugin tolerates `group_require_mention_exempt_rooms` as an alias if you find that name more explicit.

**Or with `kimi_dm_autodetect=true`** (v2.2.0+) — DMs auto-bypass without listing UUIDs, exempt list only needed for explicit free-response group policy:

```yaml
platforms:
  kimi:
    enabled: true
    extra:
      group_require_mention: true
      kimi_dm_autodetect: true   # 2-member rooms auto-bypass the gate
      # kimi_free_response_chats only needed for explicit free-response groups:
      # kimi_free_response_chats:
      #   - room:<3plus-member-group-you-want-freely-responsive>
```

Performance note: `kimi_dm_autodetect` only fires on messages about to be dropped by the gate (i.e. when `group_require_mention=true` AND no @-mention AND not in the explicit exempt list). The detector then calls `list_group_members` once per new room (~50-200 ms RPC over WAN), cached for 5 minutes. Messages headed to dispatch never incur this cost — the fast path is unchanged.

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

#### `tool_only` and `send_message_tool` — known limitation

Against current upstream Hermes (≤ 0.14.0), `send_message_tool._send_via_adapter` prefers the live `adapter.send()` over `standalone_sender_fn` when both are available (this is upstream's documented dispatch contract — live in-process adapter first, standalone fallback for out-of-process callers). In `tool_only` mode that means explicit tool sends from an in-process gateway are silently suppressed alongside streaming prose, because this adapter currently doesn't differentiate "prose chunk" from "explicit tool send" at the gate.

It's a plugin-side limitation, not an upstream gap. Until the adapter grows differentiation:

- For **interactive 1:1 DMs**, stay on `passthrough` (the default). `tool_only` makes the bot indistinguishable from a hung one.
- For **cron-only or out-of-process deployments**, `tool_only` works because cron dispatch falls through to `standalone_sender_fn` (Path 2), which is not gated by `output_mode`.
- For **mixed in-process + cron**, the safer choice is `passthrough`.

### Send-timeout policy (v2.1.4+)

Client-side `aiohttp.ClientTimeout` firing on a `SendMessage` POST is treated as **non-retryable** by the live-adapter `send()` and by the standalone `send_kimi_message()` helper. Production instrumentation in v2.1.4 captured a case where kimi.com server-side accepted the POST internally but held the HTTP response on that specific TCP connection past the 30 s timeout — the gateway's retry layer would then re-POST on a fresh connection, and the user would see the message twice.

The trade-off: if a future timeout corresponds to a real non-delivery (server-side never accepted), the user sees nothing. The captured production case showed server-side acceptance, but the diagnostic is one data point and the trade-off favours "no duplicates" over "rare drop". See [`CHANGELOG.md`](CHANGELOG.md) for the v2.1.4 / v2.1.5 entries with the full reasoning + the WARNING-level log line you can grep for if you suspect this fired in your deployment.

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
# Install hermes-agent from upstream main (v0.14.0+ is the floor — see pyproject.toml):
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
- `kimi_adapter.py` — full `KimiAdapter(BasePlatformAdapter)` implementation

`_compat/registry.py` is a vendored compatibility shim: it tries to import the upstream registry, and re-raises `ImportError` with an actionable message if the hook isn't present. This makes the plugin's requirements explicit at import time.

`tests/test_kimi.py` contains the adapter unit tests. They cover envelope codec, chat-id routing, dedup, MessageEvent synthesis, slash-command detection, mention-gate exemption, cross-loop session handling, and the send-arm exception policy. Plugin-integration tests (registry registration, dispatch, in-tree fallback) live in the in-fork suite at `tests/hermes_cli/test_kimi_plugin_integration.py` and are not duplicated here because they depend on the full Hermes plugin loader runtime.

## Plugin-author notes (for other external platform authors)

This section is for someone writing their own external Hermes platform plugin (`ctx.register_platform()` based, not in-tree). The patterns below were learned the hard way while running this adapter in production; documenting them so the next plugin author doesn't have to rediscover them.

### Cross-loop `aiohttp.ClientSession` binding

**Problem.** A `ClientSession` is bound to whichever asyncio event loop ran its constructor. If your adapter caches a session created at `connect()` time (gateway main loop), and Hermes's `send_message_tool` later invokes `adapter.send()` from a worker-thread loop via `_run_async` → `worker_loop.run_until_complete`, the cached session is reached from the *wrong* loop. aiohttp's `TimerContext.__enter__` raises:

```
RuntimeError: Timeout context manager should be used inside a task
```

…because `asyncio.current_task(loop=session._loop)` looks at the gateway loop while you're on the worker loop.

**Plugin-side workaround.** Gate every `ClientSession` use through an async-context-manager helper that returns the cached session when loops match, or an ephemeral same-loop session otherwise. Sketch:

```python
@asynccontextmanager
async def _session_for_current_loop(self):
    current_loop = asyncio.get_running_loop()
    cached = self._http_session
    if cached is None or getattr(cached, "closed", False):
        # First use or stale — create on the current loop and cache.
        self._http_session = aiohttp.ClientSession(trust_env=True)
        self._http_session_loop = current_loop
        yield self._http_session
        return
    if self._http_session_loop is None or self._http_session_loop is current_loop:
        yield cached
        return
    # Cross-loop: ephemeral session scoped to this call only.
    async with aiohttp.ClientSession(trust_env=True) as ephemeral:
        yield ephemeral
```

Then everywhere in the adapter that needs HTTP:

```python
async with self._session_for_current_loop() as session, session.post(...) as r:
    ...
```

See `kimi/kimi_adapter.py` (`_session_for_current_loop`, ~line 1616) for the production implementation including a closed-session guard and the loop-tracking attribute set on `connect()`.

**Trade-off.** Cross-loop calls pay a single-connection cost (ephemeral session, no pool reuse for that one request). Same-loop calls (the common case) reuse the cached pool. The alternative is the bug.

**Upstream counterpart.** A future upstream improvement to `send_message_tool` would marshal `adapter.send()` back to the gateway loop via `asyncio.run_coroutine_threadsafe`, removing the cross-loop call entirely and making this helper obsolete. Until then, it's the plugin-side workaround.

### `aiohttp.ClientSession(trust_env=True)` for proxy support (v2.1.6+ rationale)

Always construct sessions with `trust_env=True` so they honour `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` env vars. Without it, aiohttp ignores proxy env vars and your plugin will silently bypass corporate / institutional proxies — symptoms are bare connection errors with no clue pointing at the proxy as the cause. This applies to every aiohttp session site in the adapter, not just the primary one.

### Send-timeout retry semantics (v2.1.4+ rationale)

If your platform's send endpoint can accept a request server-side but hold the response past the client-side timeout, mark `aiohttp.ClientTimeout` exceptions raised from sends as **non-retryable** in the result/error returned to Hermes. Hermes's retry layer will otherwise re-POST on a fresh connection and the user sees a duplicate. The trade-off favours "no duplicates" over the rare case of a genuine non-delivery (where the user sees nothing). For Hermes ≥ 0.14.0 this is signalled via `SendResult.retryable = False` (default) or `_set_fatal_error(code, message, *, retryable=False)` on auth-style errors that should not be retried.

### Config-time `${VAR}` resolution (until upstream PR-C lands)

Vanilla upstream Hermes does NOT apply `${VAR}` substitution to values inside `platforms.<plugin>.extra` (it does for built-in platforms via `_apply_env_overrides()` in `gateway/config.py`, but not for plugin-registered platforms). Until [this upstream PR](https://github.com/linxule/hermes-agent/tree/feat/platform-registry-env-template) lands, plugin authors should either:

1. **Resolve defensively in the adapter** — accept the literal `"${MY_TOKEN}"` and call a small helper that returns `os.environ.get(name, "")` for whole-field matches. Idempotent and forward-compatible: when upstream resolves it first, the adapter sees the already-resolved value and the helper passes it through unchanged.
2. **Use `apply_yaml_config_fn`** — the canonical config-translation hook shipped in v0.13.0 (`3633c8690`). Register a function in `register(ctx)` that walks the top-level YAML block and resolves whatever templates your plugin cares about. Right answer for non-trivial schemas; overkill for the common `${VAR}` case.

This plugin uses approach (1) for forward-compat safety. See `kimi/kimi_adapter.py` if you want the helper as prior art.

### Standalone `media_files` arrives as `(path, is_voice)` tuples (v2.2.3)

If you register a `standalone_sender_fn` (the out-of-process / cron send path), the media argument is **not** a list of path strings. Upstream's `send_message_tool` runs the attachments through `BasePlatformAdapter.extract_media` (newer upstream: `filter_media_delivery_paths`) first, so your function receives a `List[Tuple[str, bool]]` of `(path, is_voice)` tuples. Forwarding that straight to `Path(...)` / `mimetypes.guess_type(...)` raises `TypeError`, which the host swallows as a generic `"Plugin standalone send failed"` — a **silent attachment drop**, not a crash. Normalize first:

```python
media_paths = [media_path for media_path, _is_voice in (media_files or [])]
```

Drop the `is_voice` flag unless your platform has a distinct voice-note upload (mirrors the in-tree discord adapter's `for media_path, _is_voice in media_files`). This bit this plugin in v2.2.2 and was fixed in v2.2.3.

## License

MIT — see [LICENSE](LICENSE).
