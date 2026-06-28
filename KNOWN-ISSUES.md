# Known Issues

Triaged defects that are confirmed but intentionally deferred. Each entry records the decision, rationale, scope, and a ready-to-apply fix so a future session can ship it without re-triaging. Active/in-flight work lives in `CLAUDE.md` and `.review/NEXT-SESSION.md`; this file is for known-but-deferred problems.

---

## Standalone media-contract tuple bug — `kimi/kimi_adapter.py` `_standalone_send`

**Status:** ✅ **RESOLVED in v2.2.3** (2026-06-29) — fix shipped to the standalone plugin (see CHANGELOG). Original triage retained below for provenance.

**Decision & rationale:** The only trigger is a **cron / out-of-process `send_message_tool` send carrying text + an attachment to a Kimi group room** — not a current workflow for the Bloom deployment ("doesn't make sense yet that cron jobs would be sent to a group room"). The bug is latent in practice today. Revisit and ship as v2.2.3 the moment cron-with-attachments becomes a real scenario.

**The defect:** `_standalone_send` (`kimi/kimi_adapter.py:4068`) declares `media_files: Optional[List[str]]` (`:4074`) and forwards items unchanged as `media_paths=media_files` (`:4084`). But the host hermes-agent's `send_message_tool` builds media as `List[Tuple[str, bool]]` = `(path, is_voice)` (via `BasePlatformAdapter.extract_media`, `gateway/platforms/base.py:2138`) and passes it straight to the plugin's registered `standalone_sender_fn` (`:4279`) with **no normalization**. So each tuple reaches `Path(path).expanduser()` at `kimi/kimi_adapter.py:678` → `TypeError` (second tuple-unsafe sink: `mimetypes.guess_type` at `:629`). The host swallows it as `{"error": "Plugin standalone send failed: ..."}` → silent attachment drop.

**Scope (what's affected / not):**
- ❌ Cron / out-of-process **text + media** sends to Kimi group rooms → attachment silently lost.
- ✅ Live **in-process bot** replies with images — fine (different path: `send`/`send_image_file`).
- ✅ **Text-only** cron sends — fine (no media).
- ✅ **Media-only** cron sends — gated out by the host (`send_message_tool.py:689`) before reaching the plugin.

**Host-contract note:** `extract_media` returns `(path, is_voice)` tuples in BOTH installed `hermes_agent` 0.13.0 and 0.14.0 (the plugin's `>=0.14.0` floor); `send_message_tool.py` is byte-identical across both. (`filter_media_delivery_paths` — the function name in current upstream/main — does NOT exist in 0.14.0; `extract_media` yields the tuples directly. The plugin fix is host-version-agnostic regardless.)

**Test gap:** `tests/test_kimi.py` `StandaloneSendRegistryWrapperTests` (`:312/:322`, `:352/:363`) double-masks the bug — feeds bare-string `media_files` AND mocks `send_kimi_message`, so neither the tuple shape nor the real `Path()` sink is exercised.

**Fix when ready (v2.2.3):** literal Discord unpack in `_standalone_send`, dropping `is_voice` (Kimi has no voice-note concept) — identical to the bundled R14 fix:
```python
media_paths = [media_path for media_path, _is_voice in (media_files or [])]
```
Pair with: rewrite the two `test_kimi.py` cases to the real `[("/tmp/a.png", False)]` shape + add one un-mocked regression test driving a tuple through to `_upload_kimi_file`; bump `pyproject.toml` version + CHANGELOG; and add the tuple-contract gotcha to the README **Plugin-author notes** (other external-plugin authors hit the same `standalone_sender_fn` media shape).

**Triage provenance (2026-06-16):** confirmed independently by Codex (`codex-rescue`) and a Claude subagent — both YES, agreeing on every file:line, against the installed `.venv` packages (authoritative). Sibling fix landed in the bundled copy as upstream PR #28704 R14 (`2b1d5aa60`).
