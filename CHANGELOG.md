# Changelog

All notable changes to `hermes-kimi-plugin`. Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses semver where API contract changes bump major, behavior changes bump minor, and bug fixes bump patch.

## [2.1.6] — 2026-05-18

### Fixed
- All five `aiohttp.ClientSession` construction sites in the adapter (the persistent session in `connect()`, three lazy/replacement/ephemeral sites inside `_session_for_current_loop()`, and the standalone `send_kimi_message()` helper) now set `trust_env=True`. aiohttp's default is `False`, which silently ignores `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` environment variables — users behind a corporate or institutional proxy could not reach kimi.com via this plugin. Symmetric with the in-tree Yuanbao / WeCom / Weixin / Matrix adapters and with upstream commit [`c1ae18ee8`](https://github.com/NousResearch/hermes-agent/commit/c1ae18ee8) (the SMS / Slack / Teams / Google-Chat sweep, 2026-05-16). An inline comment at the `connect()` site explains the rationale and warns future maintainers that the flag must be re-applied at every construction site — aiohttp does not pick it up from a module-level default.

### Context
- Discovered during an upstream-sync survey of `NousResearch/hermes-agent` since v0.13.0 (commit `3633c8690`, ~487 commits in the window). The trust_env sweep was the one mechanical patch from that survey; the gateway-side hardening (per-platform circuit breaker `518f39557`, `safe_schedule_threadsafe` helper `4e89c5308`, cron parallel-job-result fix `7a7e78a36`) is automatic benefit with no plugin-side change required. The `tools/send_message_tool.py` dispatch contract is unchanged in the window, so the README's `tool_only + send_message_tool` known-limitation section remains accurate. Full survey + a Kimi-vs-Yuanbao gap analysis live in `.review/` (gitignored).

### Tests
- 5 new tests in `ProxyTrustEnvTests` covering all five construction sites: persistent (`connect()`), lazy/replacement/cross-loop (`_session_for_current_loop`), and standalone (`send_kimi_message`). Suite: 239/239.

## [2.1.5] — 2026-05-17

### Fixed
- Extended the v2.1.4 duplicate-send fix to the standalone send path. `send_kimi_message()` (used by cron delivery and by `send_message_tool` as a fallback when no live adapter is in-process) previously caught only `aiohttp.ClientError`, so a bare `asyncio.TimeoutError` would propagate uncaught and could duplicate-deliver via the cron retry layers. Adds an `except asyncio.TimeoutError` clause with matching `retryable=False` semantics and a WARNING log identifying the chat and the configured timeout.

### Changed
- Replaced a stale private-repo reference in the `tool_only` section of the README with a generic placeholder; the upstream issue tracking the underlying architectural fix will be linked once filed.

### Tests
- 2 new regression tests in `SendKimiMessageStandalonePolicyTests`. Suite: 234/234.

## [2.1.4] — 2026-05-17

### Fixed
- Duplicate-send: the gateway's retry wrapper used to re-POST a `SendMessage` on a fresh TCP connection after the initial 30 s `aiohttp.ClientTimeout`, but kimi.com's server-side delivery had already succeeded, so the user saw the message twice. Production instrumentation at 18:46:03–18:46:37 BST captured the mechanism: one `SendMessage` entered `_rpc_unary` at T+0 and never received `RESPONSE_HEADERS`, while a concurrent `SendMessage` on the same adapter / same session completed in 697 ms during the same hang window — proving the hang is per-connection, not per-pool. The fix splits the live `send()` except chain so `asyncio.TimeoutError` returns `retryable=False` (with a WARNING log identifying the chat and the configured timeout); `KimiTransientError` keeps its `retryable=True` contract for genuine network failures.

### Trade-off
- Client-side timeouts now drop a reply rather than retrying. If the timeout corresponds to a real non-delivery (server-side never accepted the POST) the user sees nothing. The captured production case showed server-side acceptance, but the diagnostic is one data point — the trade-off favours "no duplicates" over "rare drop" pending more evidence.

### Tests
- 4 new tests in `SendArmExceptionPolicyTests`. Suite: 232/232.

## [2.1.3] — 2026-05-17

### Fixed
- Mention-gate exempt list silently never fired in production. v2.1.2 deployed with `kimi_free_response_chats: [room:<uuid>]` (the README-documented prefixed form) — but `chat_id` arrives at `_on_group_event` from the subscribe-stream envelope as a **raw UUID** with no `room:` prefix, so the set-membership check never matched. The exempt list was loaded correctly but had no behavioural effect.

### Changed
- Entries are normalised at load time: an optional leading `room:` prefix is stripped so the in-memory set comparison reflects what `_on_group_event` actually sees. Both prefixed and raw-UUID forms now work; the documented prefixed form remains the recommended public-facing convention because it matches `KIMI_HOME_CHANNEL`.

### Tests
- 1 new regression test (`test_exempt_entries_strip_room_prefix`), 2 existing tests updated to reflect normalisation behaviour.

## [2.1.2] — 2026-05-17

### Added
- `kimi_free_response_chats` — per-chat exempt list for the mention gate. Setting `group_require_mention=true` globally previously swallowed DM traffic because Kimi delivers 1:1 conversations as `room:<uuid>` indistinguishable from group rooms at the wire level. This config follows the Matrix / DingTalk pattern (`_free_rooms` / `free_response_chats`): rooms in this list bypass the mention gate even when the global gate is on. Also accepts `group_require_mention_exempt_rooms` as an explicit alias.

### Fixed
- `_install_fake_session` test fixture: pinned `session.closed = False` to plug a latent issue where every backoff-test session was being silently replaced with a real `aiohttp.ClientSession()` because `MagicMock().closed` is truthy by default. The cross-loop helper's `getattr(cached, "closed", False)` check is correct for production (real aiohttp `.closed` is a proper bool); the fixture just needed to match that contract.

### Tests
- 7 new tests in `MentionGateExemptionTests`. Suite: 227/227 in <1s, up from 220 with intermittent timeouts.

## [2.1.1] — 2026-05-17

### Fixed
- Cross-event-loop aiohttp session binding. v2.1.0 wired the `send_message_tool` path for Kimi targets for the first time, which surfaced a latent cross-loop bug: `KimiAdapter.connect()` creates `self._http_session` on the gateway's main event loop, but Hermes's `send_message_tool` dispatches `adapter.send()` from a worker-thread event loop via `_run_async` → `worker_loop.run_until_complete`. aiohttp binds `ClientSession` to whichever loop is running at `__init__` time; using the session from a different loop later raises `RuntimeError("Timeout context manager should be used inside a task")` because `asyncio.current_task(loop=session._loop)` returns `None` from the worker loop. This release adds a `_session_for_current_loop()` async-context-manager helper that yields the cached session when the current loop matches and an ephemeral session bound to the current loop otherwise (connection-pool reuse preserved for normal traffic; single-connection cost on cross-loop calls).

### Tests
- 4 regression tests in `CrossLoopSessionTests`.

### Note
- An upstream issue tracks the broader fix in `tools/send_message_tool.py` (gateway-loop marshalling of the live `adapter.send()` call). Once landed, this plugin-side workaround becomes redundant but stays safe (idempotent when loops match).

## [2.1.0] — 2026-05-17

### Added
- `standalone_sender_fn=_standalone_send` registration so cron jobs and `send_message_tool` can deliver to Kimi rooms without a live in-process adapter (e.g. when cron runs in a separate process from the gateway).

### Fixed
- `env_enablement_fn` now seeds `home_channel` as a dict (`{"chat_id": ..., "name": ...}`) matching upstream's `HomeChannel` contract. The previous string form silently failed the `isinstance(home, dict)` check in `gateway/config.py:1855-1871`, so cron home-channel delivery never worked despite `KIMI_HOME_CHANNEL` being set.

### Changed
- Corrected `apply_yaml_config_fn` docstring + README to reflect that the bridge reads a **top-level `kimi:` block** (per `yaml_cfg.get(entry.name)` in `gateway/config.py:871`), not `platforms.kimi.*`.

### Tests
- 5 new tests for the wrapper contract + end-to-end YAML→env→`HomeChannel` integration tests. Suite: 216/216.

## [2.0.1] — 2026-05-16

### Fixed
- `${VAR}` env-template resolution now covers the standalone send path. v2.0.0 wrapped the live-adapter `__init__` chain in a defensive `_resolve_env_template` helper so a `token: ${KIMI_BOT_TOKEN}` config.yaml line would resolve correctly even though Hermes does not invoke env-substitution for external-plugin `PlatformConfig.token`. Code review surfaced that the standalone `send_kimi_message()` helper (used by cron delivery and `send_message_tool` when no live adapter is available) still read `config.token` directly — meaning cron-driven kimi deliveries would 401 silently against a `${VAR}` config while the live bot path worked fine. This release wraps that path identically.

### Tests
- 2 new regression tests for the standalone surface; the existing adapter-init test was hardened against shell-env leakage by asserting on a UUID-derived sentinel.

## [2.0.0] — 2026-05-16

### Changed
- Runs against vanilla upstream Hermes Agent. Teknium shipped the platform-plugin registry in commit [`8f144fe36`](https://github.com/NousResearch/hermes-agent/commit/8f144fe36) ("pluggable platform adapter registry + IRC reference implementation") and extended it to full platform parity in [`2e20f6ae2`](https://github.com/NousResearch/hermes-agent/commit/2e20f6ae2) ("complete plugin platform parity — all 12 integration points", v0.11.0, both 2026-04-11) plus the YAML-config bridge in [`3633c8690`](https://github.com/NousResearch/hermes-agent/commit/3633c8690) (`apply_yaml_config_fn` registry hook, v0.13.0, 2026-05-13). This release retires the fork-branch dependency and adopts those upstream extension points.

### Removed
- Fork-branch dependency. The two PRs this plugin's earlier releases were carrying forward (`hook/platform-adapter-registry` + `feat/platform-kimi-enum`) are retired — Teknium's `register_platform()` is the upstream equivalent and is strictly richer than what they proposed. Historical fork branches are preserved as `archive/*` tags on [`linxule/hermes-agent`](https://github.com/linxule/hermes-agent) for reference.

[2.1.6]: https://github.com/linxule/hermes-kimi-plugin/releases/tag/v2.1.6
[2.1.5]: https://github.com/linxule/hermes-kimi-plugin/releases/tag/v2.1.5
[2.1.4]: https://github.com/linxule/hermes-kimi-plugin/releases/tag/v2.1.4
[2.1.3]: https://github.com/linxule/hermes-kimi-plugin/releases/tag/v2.1.3
[2.1.2]: https://github.com/linxule/hermes-kimi-plugin/releases/tag/v2.1.2
[2.1.1]: https://github.com/linxule/hermes-kimi-plugin/releases/tag/v2.1.1
[2.1.0]: https://github.com/linxule/hermes-kimi-plugin/releases/tag/v2.1.0
[2.0.1]: https://github.com/linxule/hermes-kimi-plugin/releases/tag/v2.0.1
[2.0.0]: https://github.com/linxule/hermes-kimi-plugin/releases/tag/v2.0.0
