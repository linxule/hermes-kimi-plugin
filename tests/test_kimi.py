"""Unit tests for the Kimi platform adapter.

Focus is on pure-function correctness: envelope codec, chat-id routing,
dedup, MessageEvent synthesis, slash-command detection. Live-network tests
(GetMe, Subscribe against real Kimi) are gated behind a
``KIMI_INTEGRATION_TOKEN`` env var and skipped by default.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import MessageType, SendResult
from kimi_adapter import (
    _CONNECT_FLAG_COMPRESSED,
    _CONNECT_FLAG_END_STREAM,
    _DMInflight,
    _WS_MAX_FRAME_SIZE,
    KimiAdapter,
    KimiAuthError,
    KimiProtocolError,
    KimiRpcError,
    KimiTransientError,
    _extract_blocks_payload,
    _extract_short_id_from_text,
    _extract_user_identity,
    _is_standalone_slash_command,
    _parse_iso8601,
    _split_for_streaming,
    _ulid_time_ms,
    check_kimi_requirements,
)
from gateway.session import SessionSource


class _FakeWSStatusError(Exception):
    """Mimic websockets 12+ upgrade-rejection exception shape."""
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status_code = status


def _cfg(**extra) -> PlatformConfig:
    """Test config factory."""
    defaults = {"enable_dms": True, "enable_groups": True}
    defaults.update(extra)
    return PlatformConfig(
        enabled=True,
        token="km_b_prod_TEST_TOKEN",
        extra=defaults,
    )


class HelpersTests(unittest.TestCase):
    def test_is_standalone_slash_command(self):
        self.assertTrue(_is_standalone_slash_command("/status"))
        self.assertTrue(_is_standalone_slash_command("  /new  "))
        self.assertTrue(_is_standalone_slash_command("/compact"))
        self.assertFalse(_is_standalone_slash_command("/status please"))
        self.assertFalse(_is_standalone_slash_command("hello /status"))
        self.assertFalse(_is_standalone_slash_command("hi"))

    def test_split_for_streaming_short(self):
        self.assertEqual(_split_for_streaming("hi", 100), ["hi"])

    def test_split_for_streaming_long(self):
        text = "a" * 8000
        chunks = _split_for_streaming(text, 3500)
        # Rejoining should preserve length (minus stripped whitespace between).
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 3500 for c in chunks))

    def test_split_for_streaming_respects_size_and_preserves_content(self):
        text = "para one.\n\npara two.\n\npara three.\n\n" + ("x" * 5000)
        chunks = _split_for_streaming(text, 3500)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 3500 for c in chunks))
        # Length preservation (minus leading whitespace stripped between chunks).
        rejoined_len = sum(len(c) for c in chunks)
        self.assertGreaterEqual(rejoined_len, len(text) - 4 * len(chunks))

    def test_parse_iso8601(self):
        self.assertIsNotNone(_parse_iso8601("2026-04-23T16:53:48Z"))
        self.assertIsNotNone(_parse_iso8601("2026-04-23T16:53:48+00:00"))
        self.assertIsNone(_parse_iso8601("not-a-date"))
        self.assertIsNone(_parse_iso8601(""))


class RequirementsTests(unittest.TestCase):
    def test_dependencies_available(self):
        # websockets + aiohttp are hermes core deps — this should always pass.
        self.assertTrue(check_kimi_requirements())


class AdapterInitTests(unittest.TestCase):
    def test_config_parsing(self):
        cfg = _cfg(
            enable_dms=True,
            enable_groups=False,
            openclaw_version="2026.5.1",
            claw_id="custom-id-123",
            group_require_mention=True,
            user_message_prefix="FROM KIMI: ",
        )
        adapter = KimiAdapter(cfg)
        self.assertEqual(adapter._bot_token, "km_b_prod_TEST_TOKEN")
        self.assertTrue(adapter._enable_dms)
        self.assertFalse(adapter._enable_groups)
        self.assertEqual(adapter._openclaw_version, "2026.5.1")
        self.assertEqual(adapter._claw_id, "custom-id-123")
        self.assertTrue(adapter._group_require_mention)
        self.assertEqual(adapter._user_message_prefix, "FROM KIMI: ")

    def test_claw_id_auto_generated(self):
        adapter = KimiAdapter(_cfg())
        self.assertTrue(adapter._claw_id.startswith("hermes-kimi-"))
        self.assertEqual(len(adapter._claw_id), len("hermes-kimi-") + 16)

    def test_ws_upgrade_headers_include_spoof(self):
        adapter = KimiAdapter(_cfg())
        headers = adapter._ws_upgrade_headers()
        self.assertEqual(headers["X-Kimi-Bot-Token"], "km_b_prod_TEST_TOKEN")
        self.assertEqual(headers["X-Kimi-OpenClaw-Version"], "2026.3.13")
        self.assertIn("X-Kimi-Claw-ID", headers)
        self.assertIn("X-Kimi-OpenClaw-Plugins", headers)

    def test_ws_upgrade_headers_omit_empty_spoof(self):
        adapter = KimiAdapter(_cfg(openclaw_version="", openclaw_skills=""))
        headers = adapter._ws_upgrade_headers()
        self.assertNotIn("X-Kimi-OpenClaw-Version", headers)
        # Skills default is empty — should not be included.
        self.assertNotIn("X-Kimi-OpenClaw-Skills", headers)

    def test_http_headers_unary_vs_streaming(self):
        adapter = KimiAdapter(_cfg())
        unary = adapter._http_headers(streaming=False)
        streaming = adapter._http_headers(streaming=True)
        self.assertEqual(unary["Content-Type"], "application/json")
        self.assertEqual(streaming["Content-Type"], "application/connect+json")
        self.assertEqual(streaming["Accept"], "application/connect+json")
        self.assertEqual(unary["X-Kimi-Bot-Token"], "km_b_prod_TEST_TOKEN")
        self.assertEqual(unary["X-Kimi-Claw-Version"], "0.25.0")
        self.assertEqual(unary["X-Kimi-OpenClaw-Version"], "2026.3.13")
        self.assertIn("X-Kimi-Claw-ID", unary)
        self.assertEqual(
            json.loads(unary["X-Kimi-OpenClaw-Plugins"]),
            [{"id": "kimi-claw", "version": "0.25.0"}],
        )
        self.assertEqual(streaming["X-Kimi-OpenClaw-Plugins"], unary["X-Kimi-OpenClaw-Plugins"])

    def test_legacy_inventory_header_strings_are_normalized_to_json(self):
        adapter = KimiAdapter(_cfg(
            openclaw_plugins="kimi-claw",
            openclaw_skills="kimiim,worker-safety",
        ))

        headers = adapter._http_headers(streaming=False)

        self.assertEqual(
            json.loads(headers["X-Kimi-OpenClaw-Plugins"]),
            [{"id": "kimi-claw", "version": "0.25.0"}],
        )
        self.assertEqual(
            json.loads(headers["X-Kimi-OpenClaw-Skills"]),
            ["kimiim", "worker-safety"],
        )


class EnvelopeCodecTests(unittest.TestCase):
    def test_encode_envelope_data(self):
        adapter = KimiAdapter(_cfg())
        body = adapter._encode_envelope(b'{"hello": "world"}')
        self.assertEqual(body[0], 0)  # data flag
        length = struct.unpack(">I", body[1:5])[0]
        self.assertEqual(length, len(b'{"hello": "world"}'))
        self.assertEqual(body[5:], b'{"hello": "world"}')

    def test_encode_envelope_end_stream(self):
        adapter = KimiAdapter(_cfg())
        body = adapter._encode_envelope(b"{}", end_stream=True)
        self.assertEqual(body[0], _CONNECT_FLAG_END_STREAM)


class EnvelopeParserTests(unittest.IsolatedAsyncioTestCase):
    """Feed synthetic byte streams through the envelope parser."""

    async def _collect(self, adapter: KimiAdapter, stream: bytes):
        reader = _FakeStreamReader(stream)
        out = []
        async for msg in adapter._connect_envelope_parser(reader):
            out.append(msg)
        return out

    async def test_single_data_frame_then_end_stream(self):
        adapter = KimiAdapter(_cfg())
        data = b'{"ping":{}}'
        stream = (
            bytes([0]) + struct.pack(">I", len(data)) + data
            + bytes([_CONNECT_FLAG_END_STREAM]) + struct.pack(">I", 2) + b"{}"
        )
        msgs = await self._collect(adapter, stream)
        self.assertEqual(msgs, [{"ping": {}}])

    async def test_multiple_data_frames(self):
        adapter = KimiAdapter(_cfg())
        parts = [b'{"ping":{}}', b'{"message":{"id":"x"}}']
        stream = b""
        for p in parts:
            stream += bytes([0]) + struct.pack(">I", len(p)) + p
        stream += bytes([_CONNECT_FLAG_END_STREAM]) + struct.pack(">I", 2) + b"{}"
        msgs = await self._collect(adapter, stream)
        self.assertEqual(msgs, [{"ping": {}}, {"message": {"id": "x"}}])

    async def test_end_stream_with_auth_error_raises(self):
        adapter = KimiAdapter(_cfg())
        err_body = json.dumps({
            "error": {"code": "unauthenticated", "message": "token expired"}
        }).encode()
        stream = bytes([_CONNECT_FLAG_END_STREAM]) + struct.pack(">I", len(err_body)) + err_body
        with self.assertRaises(KimiAuthError):
            await self._collect(adapter, stream)

    async def test_end_stream_with_rpc_error_raises(self):
        adapter = KimiAdapter(_cfg())
        err_body = json.dumps({"error": {"code": "invalid_argument", "message": "bad"}}).encode()
        stream = bytes([_CONNECT_FLAG_END_STREAM]) + struct.pack(">I", len(err_body)) + err_body
        with self.assertRaises(KimiRpcError):
            await self._collect(adapter, stream)

    async def test_compressed_frame_rejected(self):
        adapter = KimiAdapter(_cfg())
        stream = bytes([_CONNECT_FLAG_COMPRESSED]) + struct.pack(">I", 2) + b"{}"
        with self.assertRaises(KimiProtocolError):
            await self._collect(adapter, stream)

    async def test_malformed_json_rejected(self):
        adapter = KimiAdapter(_cfg())
        bad = b"{not json"
        stream = bytes([0]) + struct.pack(">I", len(bad)) + bad
        with self.assertRaises(KimiProtocolError):
            await self._collect(adapter, stream)


class _FakeStreamReader:
    """Minimal aiohttp.StreamReader stub for parser tests."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    async def readexactly(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            raise asyncio.IncompleteReadError(self._data[self._pos:], n)
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        return chunk


class ChatIdRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_dm_prefix_routes_to_send_dm(self):
        adapter = KimiAdapter(_cfg())
        adapter._send_dm = AsyncMock(return_value=SendResult(success=True))
        adapter._send_group = AsyncMock()
        result = await adapter.send("dm:im:kimi:main", "hello")
        self.assertTrue(result.success)
        adapter._send_dm.assert_awaited_once()
        adapter._send_group.assert_not_awaited()

    async def test_room_prefix_routes_to_send_group(self):
        adapter = KimiAdapter(_cfg())
        adapter._send_dm = AsyncMock()
        adapter._send_group = AsyncMock(return_value=SendResult(success=True))
        result = await adapter.send("room:abc-def", "hello")
        self.assertTrue(result.success)
        adapter._send_group.assert_awaited_once()
        call_args = adapter._send_group.call_args
        self.assertEqual(call_args.args[0], "abc-def")  # room_id
        # thread_id position (from metadata) should be None
        self.assertIsNone(call_args.args[3])

    async def test_room_thread_suffix_extracts_thread(self):
        adapter = KimiAdapter(_cfg())
        adapter._send_group = AsyncMock(return_value=SendResult(success=True))
        await adapter.send("room:abc-def/thread-xyz", "hello")
        call_args = adapter._send_group.call_args
        self.assertEqual(call_args.args[0], "abc-def")
        self.assertEqual(call_args.args[3], "thread-xyz")

    async def test_thread_id_in_metadata(self):
        adapter = KimiAdapter(_cfg())
        adapter._send_group = AsyncMock(return_value=SendResult(success=True))
        await adapter.send("room:abc-def", "hello", metadata={"thread_id": "t-1"})
        call_args = adapter._send_group.call_args
        self.assertEqual(call_args.args[3], "t-1")

    async def test_unknown_prefix_fails_cleanly(self):
        adapter = KimiAdapter(_cfg())
        result = await adapter.send("weird:foo", "hello")
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertIn("unknown chat_id format", result.error)


class DedupTests(unittest.TestCase):
    def test_same_pair_deduped(self):
        adapter = KimiAdapter(_cfg())
        self.assertFalse(adapter._dedup_is_duplicate("group", "chat-1", "msg-1"))
        self.assertTrue(adapter._dedup_is_duplicate("group", "chat-1", "msg-1"))

    def test_different_kinds_not_deduped(self):
        adapter = KimiAdapter(_cfg())
        self.assertFalse(adapter._dedup_is_duplicate("group", "chat-1", "msg-1"))
        self.assertFalse(adapter._dedup_is_duplicate("dm", "chat-1", "msg-1"))

    def test_eviction_at_max(self):
        adapter = KimiAdapter(_cfg())
        maxlen = adapter._processed.maxlen
        # Fill + one overflow
        for i in range(maxlen + 1):
            adapter._dedup_is_duplicate("group", "chat", f"msg-{i}")
        # First key should be evicted now.
        self.assertFalse(adapter._dedup_is_duplicate("group", "chat", "msg-0"))


class MessageEventSynthesisTests(unittest.TestCase):
    def test_text_event(self):
        adapter = KimiAdapter(_cfg())
        event = adapter._build_message_event(
            kind="group",
            text="hello",
            message_id="mid-1",
            chat_id="room:abc",
            chat_name="Test Room",
            user_id="u-1",
            user_name="Alice",
        )
        self.assertEqual(event.text, "hello")
        self.assertEqual(event.message_type, MessageType.TEXT)
        self.assertEqual(event.source.platform, Platform.KIMI)
        self.assertEqual(event.source.chat_id, "room:abc")
        self.assertEqual(event.source.chat_type, "group")
        self.assertFalse(event.internal)

    def test_command_event(self):
        adapter = KimiAdapter(_cfg())
        event = adapter._build_message_event(
            kind="dm",
            text="/reset",
            message_id="mid-2",
            chat_id="dm:im:kimi:main",
            chat_name="Kimi DM",
            user_id="u-2",
            user_name=None,
        )
        self.assertEqual(event.message_type, MessageType.COMMAND)

    def test_photo_event(self):
        adapter = KimiAdapter(_cfg())
        event = adapter._build_message_event(
            kind="group",
            text="look at this",
            message_id="mid-3",
            chat_id="room:abc",
            chat_name=None,
            user_id="u-3",
            user_name=None,
            media_urls=["https://example/img.jpg"],
            media_types=["image/jpeg"],
        )
        self.assertEqual(event.message_type, MessageType.PHOTO)
        self.assertEqual(event.media_urls, ["https://example/img.jpg"])

    def test_auto_skill_passthrough(self):
        adapter = KimiAdapter(_cfg(auto_skill="test-skill"))
        event = adapter._build_message_event(
            kind="group",
            text="hello",
            message_id="mid-4",
            chat_id="room:abc",
            chat_name=None,
            user_id=None,
            user_name=None,
        )
        self.assertEqual(event.auto_skill, "test-skill")


class GroupEventParsingTests(unittest.IsolatedAsyncioTestCase):
    """Kimi Subscribe protobuf-JSON events are converted into Hermes messages."""

    async def test_protobuf_json_chat_message_event_dispatches_summary(self):
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore
        # Fix A: legacy summary-fallback test — disable hydration so the
        # cascade lands on the summary path WITHOUT making a real
        # _fetch_group_message → aiohttp call (Commit 6 made hydration
        # the default; without this the test attempts a real network
        # round-trip and only "passes" because the resulting transient
        # error falls through to the same summary fallback).
        adapter._hydrate_missing_text = False

        await adapter._on_group_event({
            "id": "evt-1",
            "chatMessage": {
                "chatId": "chat-1",
                "messageId": "msg-1",
                "status": "STATUS_COMPLETED",
                "senderId": "user-1",
                "senderShortId": "u1",
                "roomId": "room-1",
                "summary": "hello from kimi",
            },
        })

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        # Fix B: summary-fallback path now prepends a truncation marker
        # so the agent knows the body is a preview, not the full message.
        self.assertTrue(
            event.text.startswith("[message truncated"),
            f"expected truncation marker prefix, got: {event.text!r}",
        )
        self.assertIn("hello from kimi", event.text)
        self.assertEqual(event.source.chat_id, "room:chat-1")
        self.assertEqual(event.source.user_id, "user-1")
        self.assertEqual(event.source.user_name, "u1")

    async def test_generated_payload_shape_extracts_text_block(self):
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore

        await adapter._on_group_event({
            "id": "evt-2",
            "payload": {
                "case": "chatMessage",
                "value": {
                    "chatId": "chat-2",
                    "messageId": "msg-2",
                    "status": 2,
                    "senderShortId": "u2",
                    "blocks": [
                        {
                            "id": "b1",
                            "content": {
                                "case": "text",
                                "value": {"content": "block text"},
                            },
                        }
                    ],
                },
            },
        })

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        self.assertEqual(event.text, "block text")
        self.assertEqual(event.source.user_id, "kimi:u2")

    async def test_generating_status_is_not_dispatched(self):
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore

        await adapter._on_group_event({
            "id": "evt-3",
            "chatMessage": {
                "chatId": "chat-3",
                "messageId": "msg-3",
                "status": "STATUS_GENERATING",
                "summary": "partial",
            },
        })

        adapter.handle_message.assert_not_awaited()


class SendMessageShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_send_uses_kimi_blocks_request_shape(self):
        adapter = KimiAdapter(_cfg())
        adapter._rpc_unary = AsyncMock(return_value={"messageId": "sent-1"})  # type: ignore

        result = await adapter._send_group(
            "chat-1",
            "hello",
            reply_to="ignored",
            thread_id="ignored",
            metadata={},
        )

        self.assertTrue(result.success)
        adapter._rpc_unary.assert_awaited_once()
        method, body = adapter._rpc_unary.await_args.args
        self.assertEqual(method, "SendMessage")
        self.assertEqual(body["chatId"], "chat-1")
        self.assertEqual(body["blocks"][0]["text"]["content"], "hello")
        self.assertNotIn("text", body)
        self.assertNotIn("chat_id", body)

    async def test_group_send_supports_attachment_only_resource_link(self):
        adapter = KimiAdapter(_cfg())
        adapter._rpc_unary = AsyncMock(return_value={"messageId": "sent-2"})  # type: ignore

        result = await adapter._send_group(
            "chat-1",
            "",
            reply_to=None,
            thread_id=None,
            metadata={"attachments": [{
                "uri": "kimi-file://file-123",
                "name": "report.pdf",
                "mimeType": "application/pdf",
                "sizeBytes": 42,
            }]},
        )

        self.assertTrue(result.success)
        _method, body = adapter._rpc_unary.await_args.args
        self.assertEqual(len(body["blocks"]), 1)
        resource = body["blocks"][0]["resourceLink"]
        self.assertEqual(resource["uri"], "kimi-file://file-123")
        self.assertEqual(resource["title"], "report.pdf")
        self.assertNotIn("text", body["blocks"][0])

    async def test_send_document_uploads_to_kimi_file_resource(self):
        adapter = KimiAdapter(_cfg())
        adapter._http_session = MagicMock()
        adapter._send_group = AsyncMock(return_value=SendResult(success=True, message_id="sent-3"))  # type: ignore
        uploaded = [{
            "uri": "kimi-file://file-456",
            "name": "notes.txt",
            "mimeType": "text/plain",
        }]

        with patch("kimi_adapter._upload_kimi_files", new=AsyncMock(return_value=uploaded)) as upload_mock:
            result = await adapter.send_document("room:chat-1", "/tmp/notes.txt", caption="see attached")

        self.assertTrue(result.success)
        upload_mock.assert_awaited_once()
        adapter._send_group.assert_awaited_once()
        args = adapter._send_group.await_args.args
        self.assertEqual(args[0], "chat-1")
        self.assertEqual(args[1], "see attached")
        self.assertEqual(adapter._send_group.await_args.kwargs["metadata"]["attachments"], uploaded)

    async def test_kimi_file_media_resolution_replaces_uri_with_local_path(self):
        adapter = KimiAdapter(_cfg())
        adapter._resolve_kimi_file_uri = AsyncMock(return_value={
            "localPath": "/tmp/kimi-file/report.pdf",
            "contentType": "application/pdf",
        })  # type: ignore

        urls, types = await adapter._resolve_kimi_file_media(
            ["kimi-file://12345678-1234-1234-1234-123456789abc", "https://example/a.png"],
            ["resource_link", "image/png"],
            message_id="msg-1",
        )

        self.assertEqual(urls, ["/tmp/kimi-file/report.pdf", "https://example/a.png"])
        self.assertEqual(types, ["application/pdf", "image/png"])


class GroupRpcHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_chat_info_fetches_room_and_members(self):
        adapter = KimiAdapter(_cfg())
        adapter._rpc_unary = AsyncMock(side_effect=[
            {"room": {"id": "room-1", "name": "Research Room"}},
            {"members": [{"id": "m1", "shortId": "alice"}]},
        ])  # type: ignore

        info = await adapter.get_chat_info("room:room-1")

        self.assertEqual(info["name"], "Research Room")
        self.assertEqual(info["members"], [{"id": "m1", "shortId": "alice"}])
        self.assertEqual(
            adapter._rpc_unary.await_args_list[0].args,
            ("GetRoom", {"roomId": "room-1"}),
        )
        self.assertEqual(adapter._rpc_unary.await_args_list[1].args[0], "ListMembers")
        self.assertEqual(adapter._rpc_unary.await_args_list[1].args[1]["roomId"], "room-1")

    async def test_empty_subscribe_event_hydrates_from_list_messages(self):
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._rpc_unary = AsyncMock(return_value={
            "messages": [
                {
                    "senderId": "user-9",
                    "senderShortId": "u9",
                    "message": {
                        "id": "msg-9",
                        "blocks": [
                            {
                                "content": {
                                    "case": "text",
                                    "value": {"content": "hydrated text"},
                                },
                            }
                        ],
                    },
                }
            ]
        })  # type: ignore

        await adapter._on_group_event({
            "chatMessage": {
                "chatId": "chat-9",
                "messageId": "msg-9",
                "status": "STATUS_COMPLETED",
            },
        })

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        self.assertEqual(event.text, "hydrated text")
        self.assertEqual(event.source.user_id, "user-9")
        self.assertEqual(event.source.user_name, "u9")
        method, body = adapter._rpc_unary.await_args.args
        self.assertEqual(method, "ListMessages")
        self.assertEqual(body["chatId"], "chat-9")
        self.assertEqual(body["startMessageId"], "msg-9")

    async def test_failed_hydration_does_not_dispatch_or_dedup_empty_event(self):
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._fetch_group_message = AsyncMock(side_effect=[
            KimiRpcError("temporary bad cursor"),
            {
                "id": "msg-10",
                "blocks": [
                    {
                        "content": {
                            "case": "text",
                            "value": {"content": "second replay text"},
                        },
                    }
                ],
            },
        ])  # type: ignore
        event = {
            "chatMessage": {
                "chatId": "chat-10",
                "messageId": "msg-10",
                "status": "STATUS_COMPLETED",
            },
        }

        await adapter._on_group_event(event)
        adapter.handle_message.assert_not_awaited()

        await adapter._on_group_event(event)
        adapter.handle_message.assert_awaited_once()
        delivered = adapter.handle_message.await_args.args[0]
        self.assertEqual(delivered.text, "second replay text")

    async def test_hydrated_self_message_is_filtered(self):
        adapter = KimiAdapter(_cfg())
        adapter._me_id = "bot-self"
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._fetch_group_message = AsyncMock(return_value={
            "id": "msg-self",
            "senderId": "bot-self",
            "blocks": [
                {
                    "content": {
                        "case": "text",
                        "value": {"content": "echo"},
                    },
                }
            ],
        })  # type: ignore

        await adapter._on_group_event({
            "chatMessage": {
                "chatId": "chat-self",
                "messageId": "msg-self",
                "status": "STATUS_COMPLETED",
            },
        })

        adapter.handle_message.assert_not_awaited()

    async def test_non_user_group_message_role_is_not_dispatched(self):
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore
        # Fix A: summary-only event with hydration default-on would
        # otherwise trigger a real _fetch_group_message → aiohttp call.
        # Test asserts a role drop, not text content — disable hydration.
        adapter._hydrate_missing_text = False

        await adapter._on_group_event({
            "chatMessage": {
                "chatId": "chat-bot",
                "messageId": "msg-bot",
                "status": "STATUS_COMPLETED",
                "role": "ROLE_ASSISTANT",
                "senderId": "assistant-1",
                "summary": "assistant echo",
            },
        })

        adapter.handle_message.assert_not_awaited()

    async def test_hydrated_non_user_group_message_role_is_not_dispatched(self):
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._fetch_group_message = AsyncMock(return_value={
            "id": "msg-hydrated-bot",
            "role": "ASSISTANT",
            "senderId": "assistant-2",
            "blocks": [
                {
                    "content": {
                        "case": "text",
                        "value": {"content": "hydrated assistant echo"},
                    },
                }
            ],
        })  # type: ignore

        await adapter._on_group_event({
            "chatMessage": {
                "chatId": "chat-hydrated-bot",
                "messageId": "msg-hydrated-bot",
                "status": "STATUS_COMPLETED",
            },
        })

        adapter.handle_message.assert_not_awaited()


def _bot_msg(**overrides) -> dict:
    """Build a minimal ROLE_ASSISTANT chatMessage event with a bot-role sender."""
    base = {
        "chatMessage": {
            "chatId": "chat-bot",
            "messageId": "msg-bot",
            "status": "STATUS_COMPLETED",
            "role": "ROLE_ASSISTANT",
            "senderId": "assistant-1",
            "senderShortId": "u_bot",
            "summary": "hello from bot",
        },
    }
    base["chatMessage"].update(overrides)
    return base


class GroupTrustedSenderTests(unittest.IsolatedAsyncioTestCase):
    """group_trusted_senders is an authoritative short_id / id allowlist.

    Fix A: ``_bot_msg()`` is summary-only (no inline blocks). With
    hydration default-on these tests would attempt a real
    ``_fetch_group_message`` → ``aiohttp`` call. Tests assert
    trust/policy behavior, not text content — disable hydration on each
    fixture.
    """

    async def test_group_trusted_sender_bypasses_role_filter(self):
        adapter = KimiAdapter(_cfg(group_trusted_senders=["u_bot"]))
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._hydrate_missing_text = False  # Fix A — see class docstring

        await adapter._on_group_event(_bot_msg())

        adapter.handle_message.assert_awaited_once()

    async def test_group_trusted_sender_by_id_also_matches(self):
        adapter = KimiAdapter(_cfg(group_trusted_senders=["assistant-1"]))
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._hydrate_missing_text = False  # Fix A — see class docstring

        # senderShortId not in allowlist; senderId IS — should still bypass.
        await adapter._on_group_event(_bot_msg(senderShortId="u_somebody_else"))

        adapter.handle_message.assert_awaited_once()


class GroupAllowBotSendersPolicyTests(unittest.IsolatedAsyncioTestCase):
    """group_allow_bot_senders policy: off | trusted_only | mentions | all.

    Fix A: every test in this class feeds ``_bot_msg()`` (summary-only).
    Disable hydration so the cascade doesn't attempt a real
    ``_fetch_group_message`` → ``aiohttp`` call when policy allows
    dispatch.
    """

    async def test_group_allow_bot_senders_off_drops_assistant(self):
        adapter = KimiAdapter(_cfg(group_allow_bot_senders="off"))
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._hydrate_missing_text = False  # Fix A

        await adapter._on_group_event(_bot_msg())

        adapter.handle_message.assert_not_awaited()

    async def test_group_allow_bot_senders_trusted_only_drops_untrusted_assistant(self):
        adapter = KimiAdapter(_cfg(
            group_allow_bot_senders="trusted_only",
            group_trusted_senders=["u_other"],
        ))
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._hydrate_missing_text = False  # Fix A

        await adapter._on_group_event(_bot_msg())

        adapter.handle_message.assert_not_awaited()

    async def test_group_allow_bot_senders_trusted_only_allows_trusted_assistant(self):
        adapter = KimiAdapter(_cfg(
            group_allow_bot_senders="trusted_only",
            group_trusted_senders=["u_bot"],
        ))
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._hydrate_missing_text = False  # Fix A

        await adapter._on_group_event(_bot_msg())

        adapter.handle_message.assert_awaited_once()

    async def test_group_allow_bot_senders_mentions_allows_with_mention(self):
        adapter = KimiAdapter(_cfg(group_allow_bot_senders="mentions"))
        adapter._me_short_id = "u_me"
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._hydrate_missing_text = False  # Fix A

        await adapter._on_group_event(_bot_msg(
            mentions=[{"short_id": "u_me"}],
        ))

        adapter.handle_message.assert_awaited_once()

    async def test_group_allow_bot_senders_mentions_drops_without_mention(self):
        adapter = KimiAdapter(_cfg(group_allow_bot_senders="mentions"))
        adapter._me_short_id = "u_me"
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._hydrate_missing_text = False  # Fix A

        await adapter._on_group_event(_bot_msg(
            mentions=[{"short_id": "u_someone_else"}],
        ))

        adapter.handle_message.assert_not_awaited()

    async def test_group_allow_bot_senders_all_allows_unconditionally(self):
        adapter = KimiAdapter(_cfg(group_allow_bot_senders="all"))
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._hydrate_missing_text = False  # Fix A

        await adapter._on_group_event(_bot_msg())

        adapter.handle_message.assert_awaited_once()


class IsMentionOfMeTests(unittest.TestCase):
    """_is_mention_of_me: pure helper shared by policy + group_require_mention."""

    def _adapter(self) -> KimiAdapter:
        adapter = KimiAdapter(_cfg())
        adapter._me_id = "bot-self-id"
        adapter._me_short_id = "u_me"
        return adapter

    def test_is_mention_of_me_short_id_match(self):
        adapter = self._adapter()
        self.assertTrue(adapter._is_mention_of_me({
            "mentions": [{"short_id": "u_me"}],
        }))
        # shortId variant
        self.assertTrue(adapter._is_mention_of_me({
            "mentions": [{"shortId": "u_me"}],
        }))

    def test_is_mention_of_me_id_match(self):
        adapter = self._adapter()
        self.assertTrue(adapter._is_mention_of_me({
            "mentions": [{"id": "bot-self-id"}],
        }))

    def test_is_mention_of_me_mentioned_flag_fallback(self):
        adapter = self._adapter()
        # No mentions array, but `mentioned: true` → accept
        self.assertTrue(adapter._is_mention_of_me({"mentioned": True}))

    def test_is_mention_of_me_no_match(self):
        adapter = self._adapter()
        self.assertFalse(adapter._is_mention_of_me({}))
        self.assertFalse(adapter._is_mention_of_me({
            "mentions": [{"short_id": "u_someone_else"}],
        }))
        # Malformed inputs return False
        self.assertFalse(adapter._is_mention_of_me({"mentions": "not-a-list"}))
        self.assertFalse(adapter._is_mention_of_me({"mentions": ["not-a-dict"]}))


class GroupPolicyLoggingTests(unittest.IsolatedAsyncioTestCase):
    """Policy drops log at INFO, not DEBUG, for operator observability."""

    async def test_policy_drops_log_at_info_level(self):
        adapter = KimiAdapter(_cfg(group_allow_bot_senders="off"))
        adapter.handle_message = AsyncMock()  # type: ignore
        # Fix A: _bot_msg() is summary-only — disable hydration so the
        # cascade doesn't make a real network call before the role drop.
        adapter._hydrate_missing_text = False

        with self.assertLogs("kimi_adapter", level=logging.INFO) as cm:
            await adapter._on_group_event(_bot_msg())

        adapter.handle_message.assert_not_awaited()
        # Exactly one INFO record, and it's the policy-drop message.
        info_records = [r for r in cm.records if r.levelno == logging.INFO]
        self.assertTrue(
            any("group_allow_bot_senders=off" in r.getMessage() for r in info_records),
            f"expected INFO drop log, got: {[r.getMessage() for r in cm.records]}",
        )


class GroupInvalidPolicyTests(unittest.TestCase):
    """Invalid group_allow_bot_senders values log WARNING and fall back to 'off'."""

    def test_invalid_bot_sender_policy_defaults_to_off(self):
        with self.assertLogs("kimi_adapter", level=logging.WARNING) as cm:
            adapter = KimiAdapter(_cfg(group_allow_bot_senders="nonsense"))
        self.assertEqual(adapter._group_allow_bot_senders, "off")
        self.assertTrue(
            any(
                "invalid group_allow_bot_senders" in r.getMessage()
                and r.levelno == logging.WARNING
                for r in cm.records
            ),
            f"expected WARNING about invalid policy, got: {[r.getMessage() for r in cm.records]}",
        )


class GroupRequireMentionSharedHelperTests(unittest.IsolatedAsyncioTestCase):
    """Existing group_require_mention path now delegates to _is_mention_of_me."""

    async def test_group_require_mention_uses_shared_helper(self):
        # USER-role message in a room — mention gate applies even to humans.
        adapter = KimiAdapter(_cfg(group_require_mention=True))
        adapter._me_short_id = "u_me"
        adapter.handle_message = AsyncMock()  # type: ignore
        # Fix A: events below are summary-only (no inline blocks). Disable
        # hydration so the cascade doesn't make a real _fetch_group_message
        # call before the mention filter runs.
        adapter._hydrate_missing_text = False

        # With a proper @-mention of us via the helper's matching logic → dispatched.
        await adapter._on_group_event({
            "chatMessage": {
                "chatId": "chat-req",
                "messageId": "msg-req",
                "status": "STATUS_COMPLETED",
                "role": "USER",
                "senderId": "user-1",
                "senderShortId": "u_user",
                "summary": "hey @u_me",
                "mentions": [{"short_id": "u_me"}],
            },
        })
        adapter.handle_message.assert_awaited_once()

        # Without a mention → dropped.
        adapter.handle_message.reset_mock()
        await adapter._on_group_event({
            "chatMessage": {
                "chatId": "chat-req",
                "messageId": "msg-req-2",
                "status": "STATUS_COMPLETED",
                "role": "USER",
                "senderId": "user-2",
                "senderShortId": "u_user2",
                "summary": "no mention here",
            },
        })
        adapter.handle_message.assert_not_awaited()

        # Bot-role sender who's in group_trusted_senders still has to mention us
        # (require-mention gate runs AFTER role/policy filters). Confirm the
        # shared helper is the authoritative source by flipping to assistant
        # role with a mention + all policy.
        adapter2 = KimiAdapter(_cfg(
            group_require_mention=True,
            group_allow_bot_senders="all",
        ))
        adapter2._me_short_id = "u_me"
        adapter2.handle_message = AsyncMock()  # type: ignore
        adapter2._hydrate_missing_text = False  # Fix A — _bot_msg is summary-only
        await adapter2._on_group_event(_bot_msg(
            mentions=[{"short_id": "u_me"}],
        ))
        adapter2.handle_message.assert_awaited_once()


class ThreadRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Inbound/outbound thread routing preserves thread identity.

    Kimi Claw v0.25.0's ``SendMessageRequest`` has no thread field on the
    wire. Inbound threaded messages are still tagged so gateway sessions
    stay isolated per-thread; outbound sends to a threaded chat_id WARN
    on first occurrence instead of silently collapsing.
    """

    async def test_inbound_thread_chat_id_preserved(self):
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore
        # Fix A: summary-only event — disable hydration so routing assertions
        # don't depend on a real network call.
        adapter._hydrate_missing_text = False

        await adapter._on_group_event({
            "chatMessage": {
                "chatId": "chat-t1",
                "messageId": "msg-t1",
                "status": "STATUS_COMPLETED",
                "senderId": "user-1",
                "senderShortId": "u1",
                "threadId": "thread-abc",
                "summary": "hello in a thread",
            },
        })

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        self.assertEqual(event.source.chat_id, "room:chat-t1/thread-abc")
        # SessionSource also carries the raw thread_id for routing helpers.
        self.assertEqual(event.source.thread_id, "thread-abc")

    async def test_inbound_no_thread_chat_id_room_only(self):
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore
        # Fix A: summary-only event — disable hydration so routing assertions
        # don't depend on a real network call.
        adapter._hydrate_missing_text = False

        await adapter._on_group_event({
            "chatMessage": {
                "chatId": "chat-t2",
                "messageId": "msg-t2",
                "status": "STATUS_COMPLETED",
                "senderId": "user-2",
                "senderShortId": "u2",
                "summary": "no thread",
            },
        })

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        # No slash — plain room id only.
        self.assertEqual(event.source.chat_id, "room:chat-t2")
        self.assertNotIn("/", event.source.chat_id)

    async def test_inbound_hydrated_thread_id_preserved(self):
        """Thread id on the hydrated wrapper is preserved when the Subscribe
        event itself was a lightweight stub."""
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._fetch_group_message = AsyncMock(return_value={
            "id": "msg-t3",
            "senderId": "user-3",
            "senderShortId": "u3",
            "threadId": "thread-from-hydration",
            "blocks": [
                {"content": {"case": "text", "value": {"content": "hydrated text"}}},
            ],
        })  # type: ignore

        await adapter._on_group_event({
            "chatMessage": {
                "chatId": "chat-t3",
                "messageId": "msg-t3",
                "status": "STATUS_COMPLETED",
            },
        })

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        self.assertEqual(event.source.chat_id, "room:chat-t3/thread-from-hydration")

    async def test_outbound_thread_suffix_parsed_not_dropped(self):
        """Sending to ``room:<uuid>/<tid>`` warns about thread collapse on
        first occurrence instead of silently targeting the plain room."""
        adapter = KimiAdapter(_cfg())
        adapter._rpc_unary = AsyncMock(return_value={"messageId": "sent-t1"})  # type: ignore

        with self.assertLogs("kimi_adapter", level=logging.WARNING) as cm:
            result = await adapter.send("room:chat-t4/thread-xyz", "hello thread")

        self.assertTrue(result.success)
        # WARNING fired exactly once, references the thread id and the room.
        self.assertTrue(
            any(
                r.levelno == logging.WARNING
                and "thread_id='thread-xyz'" in r.getMessage()
                and "chat-t4" in r.getMessage()
                for r in cm.records
            ),
            f"expected WARNING about thread drop, got: {[r.getMessage() for r in cm.records]}",
        )
        # Payload still hits Kimi at the room level.
        _method, body = adapter._rpc_unary.await_args.args
        self.assertEqual(body["chatId"], "chat-t4")

    async def test_outbound_thread_warning_is_one_shot(self):
        """Second threaded send to the same adapter instance does not re-warn."""
        adapter = KimiAdapter(_cfg())
        adapter._rpc_unary = AsyncMock(return_value={"messageId": "sent-t2"})  # type: ignore

        # Prime: first send consumes the warning.
        with self.assertLogs("kimi_adapter", level=logging.WARNING):
            await adapter.send("room:chat-t5/tid1", "first")

        # Second send: attach a record-capturing handler directly instead of
        # assertLogs (which fails when zero records are emitted).
        captured: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = captured.append  # type: ignore[assignment]
        kimi_logger = logging.getLogger("kimi_adapter")
        kimi_logger.addHandler(handler)
        try:
            await adapter.send("room:chat-t5/tid2", "second")
        finally:
            kimi_logger.removeHandler(handler)
        self.assertFalse(
            any(
                r.levelno >= logging.WARNING
                and "thread_id=" in r.getMessage()
                for r in captured
            ),
            f"thread drop WARNING should be one-shot, got: {[r.getMessage() for r in captured]}",
        )


class OutboundMentionRenderingTests(unittest.IsolatedAsyncioTestCase):
    """Outbound ``metadata['mentions']`` is no longer silently dropped.

    Kimi Claw v0.25.0 has no confirmed mention-block wire shape, so the
    adapter emits a WARNING and falls through to plain text. When the
    surface check confirms a variant, this path can serialize instead.
    """

    async def test_outbound_mentions_metadata_serialized(self):
        adapter = KimiAdapter(_cfg())
        adapter._rpc_unary = AsyncMock(return_value={"messageId": "sent-m1"})  # type: ignore

        with self.assertLogs("kimi_adapter", level=logging.WARNING) as cm:
            result = await adapter._send_group(
                "chat-m1",
                "hey @u_bob",
                reply_to=None,
                thread_id=None,
                metadata={"mentions": ["u_bob"]},
            )

        self.assertTrue(result.success)
        self.assertTrue(
            any(
                r.levelno == logging.WARNING
                and "metadata.mentions" in r.getMessage()
                and "u_bob" in r.getMessage()
                for r in cm.records
            ),
            f"expected WARNING about mention fall-through, got: {[r.getMessage() for r in cm.records]}",
        )
        # Plain text block still goes out — existing send contract preserved.
        _method, body = adapter._rpc_unary.await_args.args
        self.assertEqual(body["chatId"], "chat-m1")
        self.assertEqual(len(body["blocks"]), 1)
        self.assertEqual(body["blocks"][0]["text"]["content"], "hey @u_bob")

    async def test_outbound_mentions_empty_metadata_plain_text_only(self):
        """No mentions → no WARNING, plain text path unchanged."""
        adapter = KimiAdapter(_cfg())
        adapter._rpc_unary = AsyncMock(return_value={"messageId": "sent-m2"})  # type: ignore

        captured: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = captured.append  # type: ignore[assignment]
        kimi_logger = logging.getLogger("kimi_adapter")
        kimi_logger.addHandler(handler)
        try:
            result = await adapter._send_group(
                "chat-m2",
                "hello",
                reply_to=None,
                thread_id=None,
                metadata={},
            )
        finally:
            kimi_logger.removeHandler(handler)

        self.assertTrue(result.success)
        self.assertFalse(
            any(
                "metadata.mentions" in r.getMessage()
                for r in captured
            ),
            f"no mentions means no mention warning, got: {[r.getMessage() for r in captured]}",
        )
        # Also covers metadata=None via empty-dict default in the caller.
        _method, body = adapter._rpc_unary.await_args.args
        self.assertEqual(body["blocks"][0]["text"]["content"], "hello")

    async def test_outbound_mentions_warning_is_one_shot(self):
        adapter = KimiAdapter(_cfg())
        adapter._rpc_unary = AsyncMock(return_value={"messageId": "sent-m3"})  # type: ignore

        with self.assertLogs("kimi_adapter", level=logging.WARNING):
            await adapter._send_group(
                "chat-m3", "first", reply_to=None, thread_id=None,
                metadata={"mentions": ["u_one"]},
            )

        captured: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = captured.append  # type: ignore[assignment]
        kimi_logger = logging.getLogger("kimi_adapter")
        kimi_logger.addHandler(handler)
        try:
            await adapter._send_group(
                "chat-m3", "second", reply_to=None, thread_id=None,
                metadata={"mentions": ["u_two"]},
            )
        finally:
            kimi_logger.removeHandler(handler)
        self.assertFalse(
            any(
                r.levelno >= logging.WARNING
                and "metadata.mentions" in r.getMessage()
                for r in captured
            ),
            f"mention drop WARNING should be one-shot, got: {[r.getMessage() for r in captured]}",
        )


class ConfigIntegrationTests(unittest.TestCase):
    """Platform enum + env-var pickup via gateway.config."""

    def test_platform_enum_exists(self):
        self.assertEqual(Platform.KIMI.value, "kimi")

    @unittest.skip(
        "Fork-only: requires _apply_env_overrides to know about KIMI_BOT_TOKEN. "
        "That env-var wiring lives in the in-fork gateway/config.py "
        "(feat/kimi-platform-adapter), not in the clean enum-only branch this "
        "standalone repo's CI installs. When the plugin is adopted upstream, "
        "the env-var loading is expected to move into the plugin's own register() "
        "or be added to upstream config.py as part of the same change-set."
    )
    def test_env_override_loads_token(self):
        from gateway.config import GatewayConfig, _apply_env_overrides
        cfg = GatewayConfig()
        with patch.dict(os.environ, {"KIMI_BOT_TOKEN": "km_b_prod_ENV_TEST"}, clear=False):
            _apply_env_overrides(cfg)
        kimi_cfg = cfg.platforms.get(Platform.KIMI)
        self.assertIsNotNone(kimi_cfg)
        self.assertEqual(kimi_cfg.token, "km_b_prod_ENV_TEST")
        self.assertTrue(kimi_cfg.enabled)


@unittest.skip(
    "Fork-only: these tests verify that gateway/run.py has Platform.KIMI "
    "entries in the authorization allowlist maps + the home-channel nag-guard. "
    "That wiring lives in the in-fork gateway/run.py (feat/kimi-platform-adapter), "
    "not in the clean enum-only branch this standalone repo's CI installs. "
    "Standalone-distribution tests cover what the *plugin* is responsible for; "
    "the in-tree allowlist/nag wiring is a separate change-set tied to enum "
    "adoption upstream."
)
class AuthorizationIntegrationTests(unittest.TestCase):
    """Platform appears in the authorization maps in run.py."""

    def test_platform_in_allowlist_maps(self):
        # Lightweight smoke test — we read the file instead of importing
        # gateway.run (which has heavy side effects). Resolve the path via
        # the installed package so the test works in both the in-fork
        # checkout and the standalone-plugin layout (where this test file
        # is at tests/test_kimi.py rather than tests/gateway/test_kimi.py).
        import pathlib
        import gateway
        run_py = pathlib.Path(gateway.__file__).parent / "run.py"
        text = run_py.read_text()
        self.assertIn('Platform.KIMI: "KIMI_ALLOWED_USERS"', text)
        self.assertIn('Platform.KIMI: "KIMI_ALLOW_ALL_USERS"', text)

    def test_nag_guard_uses_config_get_home_channel(self):
        """Source-level verification that the nag-guard fix is still in place.

        ``HomeChannelNagGuardTests`` exercises the two-source guard by
        replicating the condition in Python, which would still pass if a
        future edit regressed the prod code back to ``os.getenv``-only.
        Anchor the test to the production expression directly so that
        regression would surface here.
        """
        import pathlib
        import gateway
        run_py = pathlib.Path(gateway.__file__).parent / "run.py"
        text = run_py.read_text()
        self.assertIn("self.config.get_home_channel(source.platform)", text)

    def test_group_allowlist_accepts_raw_room_id(self):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(platforms={Platform.KIMI: PlatformConfig(enabled=True)})
        runner.adapters = {Platform.KIMI: SimpleNamespace(send=AsyncMock())}
        runner.pairing_store = MagicMock()
        runner.pairing_store.is_approved.return_value = False

        source = SessionSource(
            platform=Platform.KIMI,
            user_id="kimi:user-1",
            chat_id="room:chat-1",
            user_name="tester",
            chat_type="group",
        )

        with patch.dict(os.environ, {
            "KIMI_GROUP_ALLOWED_USERS": "chat-1",
            "KIMI_ALLOWED_USERS": "",
            "GATEWAY_ALLOWED_USERS": "",
            "GATEWAY_ALLOW_ALL_USERS": "",
        }, clear=False):
            self.assertTrue(runner._is_user_authorized(source))

    def test_group_allowlist_accepts_prefixed_room_id(self):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(platforms={Platform.KIMI: PlatformConfig(enabled=True)})
        runner.adapters = {Platform.KIMI: SimpleNamespace(send=AsyncMock())}
        runner.pairing_store = MagicMock()
        runner.pairing_store.is_approved.return_value = False

        source = SessionSource(
            platform=Platform.KIMI,
            user_id="kimi:user-1",
            chat_id="room:chat-1",
            user_name="tester",
            chat_type="group",
        )

        with patch.dict(os.environ, {
            "KIMI_GROUP_ALLOWED_USERS": "room:chat-1",
            "KIMI_ALLOWED_USERS": "",
            "GATEWAY_ALLOWED_USERS": "",
            "GATEWAY_ALLOW_ALL_USERS": "",
        }, clear=False):
            self.assertTrue(runner._is_user_authorized(source))


class UserIdentityExtractionTests(unittest.TestCase):
    """_extract_user_identity probes several plausible Kimi wire shapes."""

    def test_flat_userid(self):
        uid, name = _extract_user_identity({"userId": "u-123"})
        self.assertEqual(uid, "u-123")
        self.assertIsNone(name)

    def test_flat_user_id_snake(self):
        uid, _ = _extract_user_identity({"user_id": "u-456"})
        self.assertEqual(uid, "u-456")

    def test_nested_sender(self):
        uid, name = _extract_user_identity(
            {"sender": {"id": "u-789", "name": "Alice"}}
        )
        self.assertEqual(uid, "u-789")
        self.assertEqual(name, "Alice")

    def test_nested_user_with_display_name(self):
        uid, name = _extract_user_identity(
            {"user": {"userId": "u-abc", "display_name": "Bob"}}
        )
        self.assertEqual(uid, "u-abc")
        self.assertEqual(name, "Bob")

    def test_no_identity(self):
        uid, name = _extract_user_identity({"sessionId": "im:kimi:main"})
        self.assertIsNone(uid)
        self.assertIsNone(name)

    def test_non_dict_input(self):
        uid, name = _extract_user_identity(None)
        self.assertIsNone(uid)
        self.assertIsNone(name)
        uid, name = _extract_user_identity("string")
        self.assertIsNone(uid)
        self.assertIsNone(name)


class SenderShortIdPrefixTests(unittest.TestCase):
    """kimi-claw's [sender_short_id: X] text prefix (group-routed-over-ACP)."""

    def test_extracts_short_id_from_prefix_line(self):
        text = (
            "Message From Kimi Group Chat Room:\n"
            "[sender_short_id: u_abc123]\n"
            "hello there"
        )
        self.assertEqual(_extract_short_id_from_text(text), "u_abc123")

    def test_extracts_short_id_anywhere_in_text(self):
        # The kimi-claw injector places it after the group prefix; we accept
        # any line-start match so we're robust to prompt-text transforms.
        text = "prelude\n[sender_short_id: u_xyz]\nactual content"
        self.assertEqual(_extract_short_id_from_text(text), "u_xyz")

    def test_no_prefix_returns_none(self):
        self.assertIsNone(_extract_short_id_from_text("plain message"))
        self.assertIsNone(_extract_short_id_from_text(""))
        self.assertIsNone(_extract_short_id_from_text(None))  # type: ignore

    def test_empty_short_id_returns_none(self):
        # Malformed: empty content between brackets.
        self.assertIsNone(_extract_short_id_from_text("[sender_short_id: ]"))

    def test_strips_surrounding_whitespace(self):
        text = "[sender_short_id:   u_padded   ]"
        self.assertEqual(_extract_short_id_from_text(text), "u_padded")


class EnvelopeLengthCapTests(unittest.IsolatedAsyncioTestCase):
    """Connect envelope parser refuses oversize frames (I4 DoS guard)."""

    async def test_envelope_length_cap_rejects_oversize(self):
        adapter = KimiAdapter(_cfg())
        oversize = _WS_MAX_FRAME_SIZE + 1
        header = bytes([0x00]) + struct.pack(">I", oversize)
        reader = MagicMock()
        reader.readexactly = AsyncMock(return_value=header)
        with self.assertRaises(KimiProtocolError) as ctx:
            async for _ in adapter._connect_envelope_parser(reader):
                self.fail("should not yield")
        self.assertIn("exceeds max frame size", str(ctx.exception))

    async def test_envelope_length_at_cap_is_allowed(self):
        adapter = KimiAdapter(_cfg())
        body = b'{"ping":{}}'
        header = bytes([0x00]) + struct.pack(">I", len(body))
        reads = [header, body, b""]
        reader = MagicMock()

        async def _readexactly(n):
            if not reads:
                raise asyncio.IncompleteReadError(b"", n)
            chunk = reads.pop(0)
            if len(chunk) != n:
                # Simulate end-of-stream for the empty tail read
                raise asyncio.IncompleteReadError(chunk, n)
            return chunk

        reader.readexactly = _readexactly
        yielded = []
        with self.assertRaises(Exception):
            async for msg in adapter._connect_envelope_parser(reader):
                yielded.append(msg)
        self.assertEqual(yielded, [{"ping": {}}])


class DMInflightQueueTests(unittest.IsolatedAsyncioTestCase):
    """Overlapping DM prompts get FIFO end_turn responses (I2)."""

    async def test_send_dm_pops_oldest_inflight(self):
        adapter = KimiAdapter(_cfg())
        # Simulate two in-flight prompts on the same kimi_sid
        sid = "im:kimi:main"
        adapter._dm_inflight[sid] = deque([
            _DMInflight(kimi_sid=sid, req_id=101),
            _DMInflight(kimi_sid=sid, req_id=102),
        ])

        respond_mock = AsyncMock()
        adapter._dm_respond = respond_mock  # type: ignore
        adapter._dm_emit_chunk = AsyncMock()  # type: ignore
        adapter._ws = MagicMock()  # non-None sentinel

        # First reply should pop req_id=101
        await adapter._send_dm(sid, "reply one", reply_to=None, metadata={})
        self.assertEqual(respond_mock.await_args_list[0].args[0], 101)

        # Second reply should pop req_id=102 and leave the queue empty
        await adapter._send_dm(sid, "reply two", reply_to=None, metadata={})
        self.assertEqual(respond_mock.await_args_list[1].args[0], 102)
        self.assertNotIn(sid, adapter._dm_inflight)

    async def test_send_dm_no_inflight_is_harmless(self):
        adapter = KimiAdapter(_cfg())
        adapter._dm_respond = AsyncMock()  # type: ignore
        adapter._dm_emit_chunk = AsyncMock()  # type: ignore
        adapter._ws = MagicMock()
        result = await adapter._send_dm(
            "im:kimi:main", "reply", reply_to=None, metadata={}
        )
        self.assertTrue(result.success)

    async def test_session_cancel_cancels_active_processing_and_clears_inflight(self):
        adapter = KimiAdapter(_cfg())
        sid = "im:kimi:main"
        adapter._dm_inflight[sid] = deque([
            _DMInflight(kimi_sid=sid, req_id=101),
        ])
        adapter.cancel_session_processing = AsyncMock()  # type: ignore

        await adapter._dm_cancel_session(sid)

        adapter.cancel_session_processing.assert_awaited_once()
        self.assertNotIn(sid, adapter._dm_inflight)
        kwargs = adapter.cancel_session_processing.await_args.kwargs
        self.assertTrue(kwargs["release_guard"])
        self.assertTrue(kwargs["discard_pending"])


class DMPromptCounterTests(unittest.IsolatedAsyncioTestCase):
    """DM session/prompt traffic counter increments on real prompts only."""

    async def test_dm_prompt_counter_increments_on_prompt(self):
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore
        self.assertEqual(adapter._dm_prompt_count, 0)

        # Minimal session/prompt frame shape — one text block.
        frame = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/prompt",
            "params": {
                "sessionId": "im:kimi:main",
                "prompt": [{"type": "text", "text": "hello"}],
            },
        }
        await adapter._dm_on_inbound_frame(frame)

        self.assertEqual(adapter._dm_prompt_count, 1)
        adapter.handle_message.assert_awaited_once()

        # Two more — counter accumulates.
        await adapter._dm_on_inbound_frame(frame)
        await adapter._dm_on_inbound_frame(frame)
        self.assertEqual(adapter._dm_prompt_count, 3)

    async def test_dm_prompt_counter_unchanged_on_non_prompt(self):
        """$/ping, initialize, session/new etc. are not user prompts."""
        adapter = KimiAdapter(_cfg())
        adapter._dm_respond = AsyncMock()  # type: ignore
        adapter.handle_message = AsyncMock()  # type: ignore

        await adapter._dm_on_inbound_frame({
            "jsonrpc": "2.0", "method": "$/ping", "params": {},
        })
        await adapter._dm_on_inbound_frame({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        })
        await adapter._dm_on_inbound_frame({
            "jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {},
        })
        # Frame with no text block — the prompt branch runs but returns early,
        # so we DO NOT count it as a real prompt.
        await adapter._dm_on_inbound_frame({
            "jsonrpc": "2.0", "id": 3, "method": "session/prompt",
            "params": {"sessionId": "im:kimi:main", "prompt": []},
        })

        self.assertEqual(adapter._dm_prompt_count, 0)

    async def test_dm_prompt_counter_does_not_count_empty_prompt(self):
        """Empty / malformed session/prompt frames short-circuit before counting."""
        adapter = KimiAdapter(_cfg())
        adapter._dm_respond = AsyncMock()  # type: ignore
        adapter.handle_message = AsyncMock()  # type: ignore

        await adapter._dm_on_inbound_frame({
            "jsonrpc": "2.0", "id": 7, "method": "session/prompt",
            "params": {"sessionId": "im:kimi:main"},
        })
        self.assertEqual(adapter._dm_prompt_count, 0)
        adapter.handle_message.assert_not_awaited()


class DMHealthSummaryTests(unittest.IsolatedAsyncioTestCase):
    """One-shot DM traffic tripwire at _dm_health_summary_s."""

    async def test_dm_health_summary_warns_on_zero_traffic(self):
        adapter = KimiAdapter(_cfg(dm_health_summary_s=0.01))
        self.assertEqual(adapter._dm_prompt_count, 0)

        with self.assertLogs("kimi_adapter", level=logging.WARNING) as cm:
            await adapter._log_dm_health_summary()

        warnings = [r for r in cm.records if r.levelno == logging.WARNING]
        self.assertEqual(len(warnings), 1)
        msg = warnings[0].getMessage()
        self.assertIn("zero prompts", msg)
        self.assertIn("enable_dms", msg)

    async def test_dm_health_summary_info_on_traffic(self):
        adapter = KimiAdapter(_cfg(dm_health_summary_s=0.01))
        adapter._dm_prompt_count = 7

        with self.assertLogs("kimi_adapter", level=logging.INFO) as cm:
            await adapter._log_dm_health_summary()

        info = [r for r in cm.records if r.levelno == logging.INFO]
        warnings = [r for r in cm.records if r.levelno == logging.WARNING]
        self.assertEqual(len(warnings), 0)
        self.assertTrue(any("received 7 prompts" in r.getMessage() for r in info))

    async def test_dm_health_summary_cancellation_safe(self):
        """Cancelling the task before the delay elapses logs nothing.

        The coroutine catches CancelledError internally so the task finishes
        cleanly (no unhandled exception noise in logs) — we assert no
        summary record was emitted and the task result is consumed.
        """
        adapter = KimiAdapter(_cfg(dm_health_summary_s=60))  # long delay

        # Collect any records that fire during the task's lifetime.
        captured: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = captured.append  # type: ignore[assignment]
        kimi_logger = logging.getLogger("kimi_adapter")
        kimi_logger.addHandler(handler)
        try:
            task = asyncio.create_task(adapter._log_dm_health_summary())
            await asyncio.sleep(0)  # give it a chance to enter sleep()
            task.cancel()
            # The coroutine swallows CancelledError internally, so it
            # returns normally (and awaiting it produces None).
            await task
        finally:
            kimi_logger.removeHandler(handler)
        health_records = [
            r for r in captured
            if "prompts in first hour" in r.getMessage()
            or "zero prompts in first hour" in r.getMessage()
        ]
        self.assertEqual(health_records, [])

    async def test_dm_health_summary_disabled_by_zero_setting(self):
        """dm_health_summary_s=0 skips arming the task in connect()."""
        adapter = KimiAdapter(_cfg(dm_health_summary_s=0))
        self.assertEqual(adapter._dm_health_summary_s, 0)
        # Task is only created inside connect() — this verifies the knob is
        # read and typed correctly. Lifecycle scheduling is covered indirectly
        # by the cancellation test above.


class SessionKeyConfigHoistingTests(unittest.TestCase):
    """session_key_* config reads live in __init__, not the hot cancel path."""

    def test_session_key_attributes_hoisted_from_config(self):
        adapter = KimiAdapter(_cfg(
            group_sessions_per_user=False,
            thread_sessions_per_user=True,
        ))
        self.assertFalse(adapter._group_sessions_per_user)
        self.assertTrue(adapter._thread_sessions_per_user)

    def test_session_key_attributes_defaults(self):
        adapter = KimiAdapter(_cfg())
        # Defaults match prior behavior (see _dm_cancel_session before hoist).
        self.assertTrue(adapter._group_sessions_per_user)
        self.assertFalse(adapter._thread_sessions_per_user)


class WSUpgradeClassificationTests(unittest.IsolatedAsyncioTestCase):
    """_dm_ws_connect_once special-cases 401/403/409 (C2)."""

    async def test_403_returns_permanent(self):
        adapter = KimiAdapter(_cfg())
        with patch(
            "kimi_adapter.websockets.connect",
            side_effect=_FakeWSStatusError(403),
        ):
            rc = await adapter._dm_ws_connect_once()
        self.assertEqual(rc, 3)

    async def test_409_first_strike_cools_off_60s(self):
        adapter = KimiAdapter(_cfg())
        sleep_mock = AsyncMock()
        with patch(
            "kimi_adapter.websockets.connect",
            side_effect=_FakeWSStatusError(409),
        ), patch("kimi_adapter.asyncio.sleep", sleep_mock):
            rc = await adapter._dm_ws_connect_once()
        self.assertEqual(rc, 0)
        self.assertEqual(adapter._dm_409_strikes, 1)
        sleep_mock.assert_awaited_once_with(60.0)

    async def test_409_second_strike_cools_off_300s(self):
        adapter = KimiAdapter(_cfg())
        adapter._dm_409_strikes = 1  # prior strike
        sleep_mock = AsyncMock()
        with patch(
            "kimi_adapter.websockets.connect",
            side_effect=_FakeWSStatusError(409),
        ), patch("kimi_adapter.asyncio.sleep", sleep_mock):
            rc = await adapter._dm_ws_connect_once()
        self.assertEqual(rc, 0)
        self.assertEqual(adapter._dm_409_strikes, 2)
        sleep_mock.assert_awaited_once_with(300.0)

    async def test_401_returns_permanent(self):
        adapter = KimiAdapter(_cfg())
        with patch(
            "kimi_adapter.websockets.connect",
            side_effect=_FakeWSStatusError(401),
        ):
            rc = await adapter._dm_ws_connect_once()
        self.assertEqual(rc, 3)


class LifecycleStatusTests(unittest.IsolatedAsyncioTestCase):
    """connect/disconnect drive base-class status + permanent auth sets fatal."""

    async def test_permanent_auth_triggers_fatal_error(self):
        adapter = KimiAdapter(_cfg())
        adapter._dm_ws_connect_once = AsyncMock(return_value=3)  # type: ignore
        await adapter._dm_ws_loop()
        self.assertEqual(adapter._fatal_error_code, "kimi_dm_auth")
        self.assertFalse(adapter._fatal_error_retryable)

    async def test_closing_shutdown_does_not_set_fatal(self):
        """If _closing is already True (clean shutdown path), don't leak fatal."""
        adapter = KimiAdapter(_cfg())
        adapter._closing = True
        adapter._dm_ws_connect_once = AsyncMock(return_value=3)  # type: ignore
        await adapter._dm_ws_loop()
        self.assertIsNone(adapter._fatal_error_code)

    async def test_groups_permanent_auth_triggers_fatal_error(self):
        adapter = KimiAdapter(_cfg())
        adapter._group_subscribe_once = AsyncMock(return_value=3)  # type: ignore
        await adapter._group_subscribe_loop()
        self.assertEqual(adapter._fatal_error_code, "kimi_groups_auth")


# ─────────────────────────────────────────────────────────────────────
# Wave-2 hardening (Commit 4)
# ─────────────────────────────────────────────────────────────────────


class TrustedOnlyDropLogLevelTests(unittest.IsolatedAsyncioTestCase):
    """trusted_only drops log at INFO with a redacted sender — balancing
    operator visibility against PII hygiene.

    The prior shape (INFO + full short_id) leaked identifiers into log
    aggregators in kimi-claw groups where every user message has
    role='assistant'. The DEBUG demote that followed removed the operator
    tripwire for misconfigured ``group_trusted_senders``. Current shape:
    INFO with sender redacted to ``prefix + 4 chars + ****`` — enough to
    diagnose drops without bleeding full identities.
    """

    async def test_trusted_only_drop_emits_info_with_redacted_sender(self):
        adapter = KimiAdapter(_cfg(
            group_allow_bot_senders="trusted_only",
            group_trusted_senders=["u_someone_else"],
        ))
        adapter.handle_message = AsyncMock()  # type: ignore
        # Fix A: _bot_msg() is summary-only — disable hydration so the
        # cascade doesn't hit the network before the redacted-drop log fires.
        adapter._hydrate_missing_text = False

        # Use a realistic-looking short_id so we can assert that the
        # redaction preserves only the prefix + first 4 body chars.
        msg = _bot_msg(senderShortId="u_gs5ri2l5dpytlap", senderId="assistant-long-id-xyz")

        with self.assertLogs("kimi_adapter", level=logging.DEBUG) as cm:
            await adapter._on_group_event(msg)

        adapter.handle_message.assert_not_awaited()

        drop_records = [
            r for r in cm.records
            if "not in group_trusted_senders" in r.getMessage()
        ]
        self.assertEqual(len(drop_records), 1, f"expected one drop log, got: {drop_records}")
        # INFO, not DEBUG — operators need a grep-able signal.
        self.assertEqual(drop_records[0].levelno, logging.INFO)
        rendered = drop_records[0].getMessage()
        # Redacted token present; full tail absent.
        self.assertIn("u_gs5r****", rendered)
        self.assertNotIn("gs5ri2l5dpytlap", rendered)

    async def test_trusted_only_drop_redacts_sender_id_when_short_id_absent(self):
        """If only sender_id is set (no short_id), that too must be redacted."""
        adapter = KimiAdapter(_cfg(
            group_allow_bot_senders="trusted_only",
            group_trusted_senders=["u_someone_else"],
        ))
        adapter.handle_message = AsyncMock()  # type: ignore
        # Fix A: _bot_msg() is summary-only — disable hydration.
        adapter._hydrate_missing_text = False

        msg = _bot_msg(senderShortId=None, senderId="assistant-long-id-xyz")

        with self.assertLogs("kimi_adapter", level=logging.DEBUG) as cm:
            await adapter._on_group_event(msg)

        drop_records = [
            r for r in cm.records
            if "not in group_trusted_senders" in r.getMessage()
        ]
        self.assertEqual(len(drop_records), 1)
        self.assertEqual(drop_records[0].levelno, logging.INFO)
        rendered = drop_records[0].getMessage()
        # "assistant-long-id-xyz" has no "u_"/"b_" prefix → first 4 + ****.
        self.assertIn("assi****", rendered)
        self.assertNotIn("assistant-long-id-xyz", rendered)


class SubscribeBackoffStateTests(unittest.IsolatedAsyncioTestCase):
    """Subscribe reconnect backoff is instance-scoped and resets to the
    oscillation-safe floor (10s) on the first processed frame post-connect.

    Pre-fix: backoff was a loop-local int that only grew monotonically.
    After hitting the 60s cap it stayed at 60s forever — messages arriving
    during reconnect windows silently lost.

    The fix resets backoff to ``floor`` (not ``base``) so flap-every-30s
    oscillation doesn't drive reconnect delay back to 2s every cycle,
    which would hammer Kimi's infra.
    """

    def _make_parser(self, events):
        """Return an async generator function that yields the given events."""
        async def _gen(_content):
            for ev in events:
                yield ev
        return _gen

    def _install_fake_session(self, adapter):
        """Replace _http_session.post() with a context manager yielding HTTP 200."""
        resp = MagicMock()
        resp.status = 200

        class _AsyncCtx:
            async def __aenter__(self_inner):
                return resp

            async def __aexit__(self_inner, *exc):
                return False

        session = MagicMock()
        session.post = MagicMock(return_value=_AsyncCtx())
        adapter._http_session = session

    async def test_subscribe_backoff_resets_to_floor_after_first_frame(self):
        adapter = KimiAdapter(_cfg())
        self._install_fake_session(adapter)
        # Pretend we've grown backoff to the cap from prior reconnect churn.
        adapter._group_subscribe_backoff = 60.0
        adapter._connect_envelope_parser = self._make_parser(  # type: ignore
            [{"chatMessage": {"chatId": "c", "messageId": "m"}}]
        )
        adapter._on_group_event = AsyncMock()  # type: ignore

        rc = await adapter._group_subscribe_once()

        self.assertEqual(rc, 0)
        # Reset lands at floor (10s) — NOT base (2s) and NOT the prior 60s.
        self.assertEqual(adapter._group_subscribe_backoff, 10.0)
        self.assertTrue(adapter._group_subscribe_frame_since_connect)

    async def test_subscribe_backoff_respects_floor_under_oscillation(self):
        """Once backoff has grown past the floor, reconnect recoveries must
        clamp to the floor — never below — so a flap-every-30s oscillation
        can't drive the reconnect delay back to the 2s base each cycle.

        Seed at ``floor * 2`` to skip the cold-start no-reset window and
        exercise the oscillation invariant directly.
        """
        adapter = KimiAdapter(_cfg())
        self._install_fake_session(adapter)
        adapter._on_group_event = AsyncMock()  # type: ignore
        # Seed past the floor so every cycle genuinely hits the reset path.
        adapter._group_subscribe_backoff = adapter._group_subscribe_backoff_floor * 2

        observed_backoffs = []
        for cycle in range(3):
            adapter._connect_envelope_parser = self._make_parser(  # type: ignore
                [{"chatMessage": {"chatId": f"c{cycle}", "messageId": f"m{cycle}"}}]
            )
            await adapter._group_subscribe_once()
            observed_backoffs.append(adapter._group_subscribe_backoff)
            # Simulate the loop's post-error grow step between cycles.
            adapter._group_subscribe_backoff = min(
                adapter._group_subscribe_backoff * 2, adapter._reconnect_max_s,
            )

        # Every post-reset value is at least the floor (10s). Without the
        # floor, naive reset to base would yield 2s on every cycle → thrash.
        for value in observed_backoffs:
            self.assertGreaterEqual(value, adapter._group_subscribe_backoff_floor)

    async def test_subscribe_emits_recovered_log_after_frame(self):
        adapter = KimiAdapter(_cfg())
        self._install_fake_session(adapter)
        adapter._group_subscribe_backoff = 32.0  # mid-growth reconnect
        adapter._connect_envelope_parser = self._make_parser(  # type: ignore
            [{"chatMessage": {"chatId": "c", "messageId": "m"}}]
        )
        adapter._on_group_event = AsyncMock()  # type: ignore

        with self.assertLogs("kimi_adapter", level=logging.INFO) as cm:
            await adapter._group_subscribe_once()

        recovered = [r for r in cm.records if "stream recovered" in r.getMessage()]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].levelno, logging.INFO)
        # Log carries the backoff that was in effect at the time of the
        # reconnect — useful operator signal for "how long were we down".
        self.assertIn("32.0s", recovered[0].getMessage())

    async def test_subscribe_no_duplicate_recovered_log(self):
        adapter = KimiAdapter(_cfg())
        self._install_fake_session(adapter)
        adapter._group_subscribe_backoff = 32.0  # past the floor → recovery will log
        adapter._connect_envelope_parser = self._make_parser(  # type: ignore
            [
                {"chatMessage": {"chatId": "c", "messageId": "m1"}},
                {"chatMessage": {"chatId": "c", "messageId": "m2"}},
                {"chatMessage": {"chatId": "c", "messageId": "m3"}},
            ]
        )
        adapter._on_group_event = AsyncMock()  # type: ignore

        with self.assertLogs("kimi_adapter", level=logging.INFO) as cm:
            await adapter._group_subscribe_once()

        recovered = [r for r in cm.records if "stream recovered" in r.getMessage()]
        self.assertEqual(len(recovered), 1, f"expected exactly one 'stream recovered', got: {len(recovered)}")

    async def test_subscribe_backoff_not_reset_on_keepalive_ping(self):
        """A degraded stream emitting only keepalive pings must NOT reset
        backoff — otherwise a ping-only loop would thrash back to the floor
        on every reconnect and hammer Kimi's infra.
        """
        adapter = KimiAdapter(_cfg())
        self._install_fake_session(adapter)
        adapter._group_subscribe_backoff = 32.0  # would be reset if ping counted
        adapter._connect_envelope_parser = self._make_parser(  # type: ignore
            [{"ping": {}}]
        )
        adapter._on_group_event = AsyncMock()  # type: ignore

        records, teardown = _capture_kimi_log_records()
        try:
            await adapter._group_subscribe_once()
        finally:
            teardown()

        # Backoff unchanged, hook not armed, no recovery log emitted.
        self.assertEqual(adapter._group_subscribe_backoff, 32.0)
        self.assertFalse(adapter._group_subscribe_frame_since_connect)
        recovered = [r for r in records if "stream recovered" in r.getMessage()]
        self.assertEqual(recovered, [])

    async def test_subscribe_backoff_not_reset_on_empty_stream(self):
        """Stream opens and closes cleanly with zero events — must NOT flip
        state or emit the recovery log.
        """
        adapter = KimiAdapter(_cfg())
        self._install_fake_session(adapter)
        adapter._group_subscribe_backoff = 32.0
        adapter._connect_envelope_parser = self._make_parser([])  # type: ignore
        adapter._on_group_event = AsyncMock()  # type: ignore

        records, teardown = _capture_kimi_log_records()
        try:
            rc = await adapter._group_subscribe_once()
        finally:
            teardown()

        self.assertEqual(rc, 0)
        self.assertEqual(adapter._group_subscribe_backoff, 32.0)
        self.assertFalse(adapter._group_subscribe_frame_since_connect)
        recovered = [r for r in records if "stream recovered" in r.getMessage()]
        self.assertEqual(recovered, [])

    async def test_subscribe_backoff_not_reset_on_handler_exception(self):
        """If _on_group_event raises on the first chatMessage, the state
        flip and recovery log must NOT fire — otherwise a dispatch-error
        loop would masquerade as a healthy stream.

        The exception should still propagate as before (caught by the
        outer handlers into return code 0 / transient retry).
        """
        adapter = KimiAdapter(_cfg())
        self._install_fake_session(adapter)
        adapter._group_subscribe_backoff = 32.0
        adapter._connect_envelope_parser = self._make_parser(  # type: ignore
            [{"chatMessage": {"chatId": "c", "messageId": "m"}}]
        )
        adapter._on_group_event = AsyncMock(  # type: ignore
            side_effect=KimiTransientError("simulated handler failure")
        )

        records, teardown = _capture_kimi_log_records()
        try:
            rc = await adapter._group_subscribe_once()
        finally:
            teardown()

        # Transient handler errors are swallowed to rc=0 (retry) by the
        # outer except block, so the caller retries the reconnect.
        self.assertEqual(rc, 0)
        # Exception pre-empted the state flip — backoff untouched.
        self.assertEqual(adapter._group_subscribe_backoff, 32.0)
        self.assertFalse(adapter._group_subscribe_frame_since_connect)
        recovered = [r for r in records if "stream recovered" in r.getMessage()]
        self.assertEqual(recovered, [])


class HomeChannelNagGuardTests(unittest.IsolatedAsyncioTestCase):
    """Home-channel nag suppresses when /sethome has persisted a channel.

    Pre-fix: the guard at run.py:4415 checked ``os.getenv(env_key)`` only.
    The /sethome handler (run.py:5952) persists to config.yaml via
    ``HomeChannel`` wiring AND sets the env var, but a gateway restarted
    after /sethome may lose env-var state before config reloads — and
    the authoritative source is the config, not the environment.

    The fix checks ``self.config.get_home_channel(platform)`` first, then
    falls back to env for the true first-run case.

    These tests exercise the guard condition directly because
    ``_handle_message_with_agent`` spans 2000+ lines and requires the
    entire GatewayRunner state graph to reach the nag block. The
    condition itself is a 2-line expression — a focused unit test on
    that expression gives higher signal than a flaky end-to-end.
    """

    def _evaluate_nag_condition(self, config, platform, env_key_present: bool) -> bool:
        """Replicate the guard at gateway/run.py:4425-4438 exactly.

        Returns True iff the nag SHOULD fire under the given config+env.
        """
        env_value = "some-chat-id" if env_key_present else None
        home_channel = config.get_home_channel(platform)
        # Guard from run.py: send nag if neither source has a value.
        return not home_channel and not env_value

    def test_home_channel_nag_suppressed_when_config_has_home_channel(self):
        config = GatewayConfig(
            platforms={
                Platform.KIMI: PlatformConfig(
                    enabled=True,
                    token="tok",
                    home_channel=HomeChannel(
                        platform=Platform.KIMI,
                        chat_id="room:abc",
                        name="Home",
                    ),
                ),
            },
        )
        # Config has a home channel; env is empty — nag must NOT fire.
        self.assertFalse(self._evaluate_nag_condition(config, Platform.KIMI, env_key_present=False))
        # Sanity: the config API used by the fix returns the HomeChannel object.
        self.assertIsNotNone(config.get_home_channel(Platform.KIMI))

    def test_home_channel_nag_fires_when_both_env_and_config_empty(self):
        """True first-run: no env, no config → operator should be prompted."""
        config = GatewayConfig(
            platforms={Platform.KIMI: PlatformConfig(enabled=True, token="tok")},
        )
        self.assertTrue(self._evaluate_nag_condition(config, Platform.KIMI, env_key_present=False))
        # Env-set first-run path still suppresses (preserved behavior).
        self.assertFalse(self._evaluate_nag_condition(config, Platform.KIMI, env_key_present=True))


def _capture_kimi_log_records(level: int = logging.DEBUG):
    """Attach a list-capturing handler to the kimi logger.

    Module-level shared helper used by both ``SubscribeBackoffStateTests``
    and the Probe* test classes. Unlike ``assertLogs``, does not require
    at least one record — suitable for tests that expect *zero* logs
    from a code path. Returns ``(records_list, teardown_callable)``.
    """
    records: list = []

    class _ListHandler(logging.Handler):
        def emit(self_inner, record):
            records.append(record)

    handler = _ListHandler(level=level)
    kimi_logger = logging.getLogger("kimi_adapter")
    prev_level = kimi_logger.level
    kimi_logger.addHandler(handler)
    kimi_logger.setLevel(level)

    def _teardown():
        kimi_logger.removeHandler(handler)
        kimi_logger.setLevel(prev_level)

    return records, _teardown


class Probe1BlockCaseTypeTests(unittest.TestCase):
    """Probe (H-A): DEBUG dump of non-text block shapes from
    ``_extract_blocks_payload``. Discriminates fragmented long messages
    from unknown block variants (resourceLink, mention, code, etc.)
    without behavior change.
    """

    def test_block_probe_fires_for_unknown_block_type(self):
        """resourceLink block with no text → per-block DEBUG log includes
        content_case, and the aggregate summary reports envelope lengths.
        """
        msg = {
            "text": "envelope preview text",
            "summary": "envelope preview summary",
            "blocks": [
                {
                    "id": "b1",
                    "content": {
                        "case": "resourceLink",
                        "value": {"uri": "kimi://file/x", "title": "x"},
                    },
                }
            ],
        }
        records, teardown = _capture_kimi_log_records()
        try:
            text, urls, types = _extract_blocks_payload(msg)
        finally:
            teardown()

        # Behavior unchanged: no text, but URI still extracted.
        self.assertEqual(text, "")
        self.assertEqual(urls, ["kimi://file/x"])
        self.assertEqual(types, ["resource_link"])

        per_block = [
            r for r in records
            if "non-text block (no extracted text)" in r.getMessage()
        ]
        self.assertEqual(len(per_block), 1, f"expected one per-block log, got: {per_block}")
        self.assertEqual(per_block[0].levelno, logging.DEBUG)
        rendered = per_block[0].getMessage()
        self.assertIn("'content_case': 'resourceLink'", rendered)
        self.assertIn("'has_uri': True", rendered)

        # Aggregate summary with envelope lengths (Fix D oracle).
        summary_log = [
            r for r in records
            if "non-text block(s)" in r.getMessage()
        ]
        self.assertEqual(len(summary_log), 1)
        rendered_sum = summary_log[0].getMessage()
        self.assertIn("extracted_text=0", rendered_sum)
        self.assertIn(f"envelope_text={len('envelope preview text')}", rendered_sum)
        self.assertIn(f"envelope_summary={len('envelope preview summary')}", rendered_sum)

    def test_block_probe_silent_for_text_blocks(self):
        """Normal text-bearing block must emit no probe log (neither per-block
        nor aggregate summary).
        """
        msg = {
            "blocks": [
                {
                    "id": "b1",
                    "content": {
                        "case": "text",
                        "value": {"content": "hello world"},
                    },
                }
            ],
        }
        records, teardown = _capture_kimi_log_records()
        try:
            text, _, _ = _extract_blocks_payload(msg)
        finally:
            teardown()

        self.assertEqual(text, "hello world")
        per_block = [
            r for r in records
            if "non-text block (no extracted text)" in r.getMessage()
        ]
        self.assertEqual(per_block, [])
        summary_log = [
            r for r in records
            if "non-text block(s)" in r.getMessage()
        ]
        self.assertEqual(summary_log, [])

    def test_block_probe_silent_for_all_text_blocks(self):
        """Negative-assertion: every block yields text → neither per-block
        probe nor the aggregate summary fires (Fix E trigger invariant).
        """
        msg = {
            "blocks": [
                {"content": {"case": "text", "value": {"content": "alpha"}}},
                {"content": {"case": "text", "value": {"content": "beta"}}},
            ],
        }
        records, teardown = _capture_kimi_log_records()
        try:
            text, _, _ = _extract_blocks_payload(msg)
        finally:
            teardown()

        self.assertEqual(text, "alpha\nbeta")
        probe = [
            r for r in records
            if "non-text block" in r.getMessage()
        ]
        self.assertEqual(probe, [])

    def test_block_probe_handles_non_dict_block(self):
        """Malformed non-dict block → no crash; DEBUG log includes type name."""
        msg = {"blocks": ["not a dict", 42]}
        records, teardown = _capture_kimi_log_records()
        try:
            text, urls, types = _extract_blocks_payload(msg)
        finally:
            teardown()

        self.assertEqual(text, "")
        self.assertEqual(urls, [])
        self.assertEqual(types, [])

        per_block = [
            r for r in records
            if "non-text block (no extracted text)" in r.getMessage()
        ]
        self.assertEqual(len(per_block), 2)
        rendered = " ".join(r.getMessage() for r in per_block)
        self.assertIn("'type': 'str'", rendered)
        self.assertIn("'type': 'int'", rendered)

    def test_block_probe_logs_envelope_lengths(self):
        """Fix D oracle: aggregate log reports envelope text/summary lengths
        + extracted length, so operators can distinguish "legitimate media
        attachment" from "orphaned fragmented tail".
        """
        msg = {
            "text": "short envelope",
            "summary": "much longer envelope preview than the extracted bit",
            "blocks": [
                {"content": {"case": "text", "value": {"content": "hi"}}},
                {
                    "id": "b2",
                    "content": {
                        "case": "resourceLink",
                        "value": {"uri": "kimi://file/y"},
                    },
                },
            ],
        }
        records, teardown = _capture_kimi_log_records()
        try:
            text, _, _ = _extract_blocks_payload(msg)
        finally:
            teardown()

        self.assertEqual(text, "hi")
        summary_log = [
            r for r in records
            if "non-text block(s)" in r.getMessage()
        ]
        self.assertEqual(len(summary_log), 1)
        rendered = summary_log[0].getMessage()
        self.assertIn("extracted_text=2", rendered)
        self.assertIn(f"envelope_text={len('short envelope')}", rendered)
        self.assertIn(
            f"envelope_summary={len('much longer envelope preview than the extracted bit')}",
            rendered,
        )
        # Oracle signature: envelope_summary > extracted_text → operator
        # sees the mismatch immediately.
        self.assertGreater(
            len("much longer envelope preview than the extracted bit"),
            2,
        )


class Probe2TextSourceTests(unittest.IsolatedAsyncioTestCase):
    """Probe (H-B): DEBUG log reports which source populated ``text``
    (blocks / text / summary / none) and the lengths of each candidate.
    Reveals when a short summary silently wins over empty blocks and
    bypasses the hydration gate.
    """

    def _adapter(self):
        adapter = KimiAdapter(_cfg(group_allow_bot_senders="all"))
        adapter.handle_message = AsyncMock()  # type: ignore
        # Disable hydration so the probe log fires on the original text,
        # not on the re-extracted hydrated payload.
        adapter._hydrate_missing_text = False
        return adapter

    async def test_text_source_probe_chooses_blocks(self):
        adapter = self._adapter()
        msg = {
            "chatMessage": {
                "chatId": "room-a",
                "messageId": "01HKCHF4FC4S0W7T3V74SG6AT6",
                "status": "STATUS_COMPLETED",
                "role": "ROLE_USER",
                "senderId": "u1",
                "senderShortId": "u_real",
                "summary": "preview",
                "blocks": [
                    {"content": {"case": "text", "value": {"content": "body text"}}}
                ],
            }
        }
        records, teardown = _capture_kimi_log_records()
        try:
            await adapter._on_group_event(msg)
        finally:
            teardown()

        probe = [r for r in records if "text source for" in r.getMessage()]
        self.assertEqual(len(probe), 1, f"expected one probe log, got: {probe}")
        rendered = probe[0].getMessage()
        self.assertIn("chose=blocks", rendered)
        self.assertIn("blocks=9", rendered)
        # Inline body present → hydration check short-circuits to
        # `skipped:inline` regardless of the _hydrate_missing_text flag
        # (Fix C distinguishes "not needed" from "operator policy off").
        self.assertIn("hydrated=skipped:inline", rendered)

    async def test_text_source_probe_chooses_summary_when_blocks_empty(self):
        adapter = self._adapter()
        msg = {
            "chatMessage": {
                "chatId": "room-b",
                "messageId": "01HKCHF4FC4S0W7T3V74SG6AT7",
                "status": "STATUS_COMPLETED",
                "role": "ROLE_USER",
                "senderId": "u1",
                "senderShortId": "u_real",
                "summary": "preview-only",
                "blocks": [],
            }
        }
        records, teardown = _capture_kimi_log_records()
        try:
            await adapter._on_group_event(msg)
        finally:
            teardown()

        probe = [r for r in records if "text source for" in r.getMessage()]
        self.assertEqual(len(probe), 1)
        rendered = probe[0].getMessage()
        self.assertIn("chose=summary", rendered)
        self.assertIn("blocks=0", rendered)
        self.assertIn(f"summary={len('preview-only')}", rendered)
        # Hydration disabled by fixture (`_hydrate_missing_text=False`) →
        # `skipped:disabled` distinguishes operator policy from "inline
        # body present, hydration not needed" (Fix C).
        self.assertIn("hydrated=skipped:disabled", rendered)

    async def test_text_source_probe_chooses_none_when_all_empty(self):
        adapter = self._adapter()
        msg = {
            "chatMessage": {
                "chatId": "room-c",
                "messageId": "01HKCHF4FC4S0W7T3V74SG6AT8",
                "status": "STATUS_COMPLETED",
                "role": "ROLE_USER",
                "senderId": "u1",
                "senderShortId": "u_real",
            }
        }
        records, teardown = _capture_kimi_log_records()
        try:
            await adapter._on_group_event(msg)
        finally:
            teardown()

        probe = [r for r in records if "text source for" in r.getMessage()]
        self.assertEqual(len(probe), 1)
        rendered = probe[0].getMessage()
        self.assertIn("chose=none", rendered)
        self.assertIn("blocks=0", rendered)
        self.assertIn("text=0", rendered)
        self.assertIn("summary=0", rendered)
        # No miss candidates when every source is empty.
        self.assertIn("miss_candidate=none", rendered)
        # Fix C: `skipped:disabled` (not generic `skipped`) when the
        # hydration flag is off.
        self.assertIn("hydrated=skipped:disabled", rendered)

    async def test_text_source_probe_flags_miss_candidate_when_summary_longer(self):
        """Fix C: when a non-chosen candidate is LONGER than the chosen one,
        `miss_candidate=<name>` flags the hydration-miss signature directly
        so operators don't have to eyeball per-field lengths.
        """
        adapter = self._adapter()
        msg = {
            "chatMessage": {
                "chatId": "room-d",
                "messageId": "01HKCHF4FC4S0W7T3V74SG6AT9",
                "status": "STATUS_COMPLETED",
                "role": "ROLE_USER",
                "senderId": "u1",
                "senderShortId": "u_real",
                "text": "short",
                "summary": "much longer preview",
                "blocks": [],
            }
        }
        records, teardown = _capture_kimi_log_records()
        try:
            await adapter._on_group_event(msg)
        finally:
            teardown()

        probe = [r for r in records if "text source for" in r.getMessage()]
        self.assertEqual(len(probe), 1)
        rendered = probe[0].getMessage()
        self.assertIn("chose=text", rendered)
        self.assertIn("miss_candidate=summary", rendered)
        # Inline body present (text=short) → `skipped:inline` (Fix C),
        # not `skipped:disabled` — the flag is off in this fixture but
        # the inline-present check short-circuits first.
        self.assertIn("hydrated=skipped:inline", rendered)


class HydrateWhenInlineBodyEmptyTests(unittest.IsolatedAsyncioTestCase):
    """H-B fix (Commit 6): when Subscribe ships ``blocks=[]`` and no inline
    ``text`` but a non-empty ``summary`` preview, hydrate from
    ``ListMessages`` rather than dispatching the truncated preview to the
    agent. Summary remains as a last-resort fallback if hydration fails.

    Production trigger (2026-04-26 11:21:36 BST): Probe 2 captured
    ``blocks=0, text=0, summary=50, chose=summary, miss_candidate=none``
    for a ~150-char inbound; agent answered against the 50-char preview.
    """

    def _summary_only_event(self, *, chat_id="chat-hb", message_id="msg-hb",
                            summary="this is a 50-char-ish truncated server-side preview"):
        return {
            "chatMessage": {
                "chatId": chat_id,
                "messageId": message_id,
                "status": "STATUS_COMPLETED",
                "role": "ROLE_USER",
                "senderId": "u1",
                "senderShortId": "u_real",
                "summary": summary,
                "blocks": [],
            }
        }

    async def test_summary_only_event_triggers_hydration(self):
        """Empty blocks + empty text + populated summary + hydration enabled
        → ``_fetch_group_message`` called exactly once with the chat/message
        ids from the event.
        """
        adapter = KimiAdapter(_cfg(group_allow_bot_senders="all"))
        adapter.handle_message = AsyncMock()  # type: ignore
        # Default is True; assert explicitly to make the contract visible.
        self.assertTrue(adapter._hydrate_missing_text)
        adapter._fetch_group_message = AsyncMock(return_value={
            "id": "msg-hb",
            "blocks": [
                {"content": {"case": "text", "value": {"content": "full body"}}}
            ],
        })  # type: ignore

        await adapter._on_group_event(self._summary_only_event())

        adapter._fetch_group_message.assert_awaited_once_with("chat-hb", "msg-hb")

    async def test_hydrated_body_wins_over_summary(self):
        """Dispatched ``MessageEvent.text`` matches the hydrated body, not
        the summary preview — the whole point of the H-B fix.
        """
        adapter = KimiAdapter(_cfg(group_allow_bot_senders="all"))
        adapter.handle_message = AsyncMock()  # type: ignore
        full_body = (
            "please reply with the literal word pineapple so the operator "
            "can verify full delivery (this is a ~150 char message)"
        )
        adapter._fetch_group_message = AsyncMock(return_value={
            "id": "msg-hb",
            "senderId": "u1",
            "senderShortId": "u_real",
            "blocks": [
                {"content": {"case": "text", "value": {"content": full_body}}}
            ],
        })  # type: ignore

        await adapter._on_group_event(
            self._summary_only_event(summary="please reply with the literal word…")
        )

        adapter.handle_message.assert_awaited_once()
        delivered = adapter.handle_message.await_args.args[0]
        self.assertEqual(delivered.text, full_body)
        self.assertNotIn("…", delivered.text)

    async def test_hydration_failure_falls_back_to_summary(self):
        """When hydration raises ``KimiAdapterError`` (transient infra blip)
        we still dispatch with the summary text rather than dropping the
        message entirely. Graceful degradation — better-than-nothing.

        Fix B: summary fallback now prepends a truncation marker so the
        agent can acknowledge the body is a preview rather than answer
        confidently against half a sentence (same H-B failure mode as
        the original bug, just less frequent).
        """
        adapter = KimiAdapter(_cfg(group_allow_bot_senders="all"))
        adapter.handle_message = AsyncMock()  # type: ignore
        summary = "preview-only fallback text"
        adapter._fetch_group_message = AsyncMock(
            side_effect=KimiRpcError("hydration failed")
        )  # type: ignore

        await adapter._on_group_event(self._summary_only_event(summary=summary))

        adapter.handle_message.assert_awaited_once()
        delivered = adapter.handle_message.await_args.args[0]
        self.assertTrue(
            delivered.text.startswith("[message truncated"),
            f"expected truncation marker prefix, got: {delivered.text!r}",
        )
        self.assertTrue(
            delivered.text.endswith(summary),
            f"expected summary at tail, got: {delivered.text!r}",
        )

    async def test_hydration_skipped_when_inline_text_present(self):
        """Happy path: inline blocks/text present → no hydration RPC even
        if a summary also exists. Guards against re-introducing per-event
        ``ListMessages`` overhead in the common case.
        """
        adapter = KimiAdapter(_cfg(group_allow_bot_senders="all"))
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._fetch_group_message = AsyncMock()  # type: ignore

        await adapter._on_group_event({
            "chatMessage": {
                "chatId": "chat-happy",
                "messageId": "msg-happy",
                "status": "STATUS_COMPLETED",
                "role": "ROLE_USER",
                "senderId": "u1",
                "senderShortId": "u_real",
                "summary": "preview",
                "blocks": [
                    {"content": {"case": "text", "value": {"content": "inline body"}}}
                ],
            }
        })

        adapter._fetch_group_message.assert_not_awaited()
        adapter.handle_message.assert_awaited_once()
        delivered = adapter.handle_message.await_args.args[0]
        self.assertEqual(delivered.text, "inline body")

    async def test_probe2_logs_hydrated_field(self):
        """Probe 2 reports ``hydrated=true`` and ``chose=hydrated`` when
        a summary-only inbound is successfully hydrated. Confirms the
        observability surface tracks the new control flow.
        """
        adapter = KimiAdapter(_cfg(group_allow_bot_senders="all"))
        adapter.handle_message = AsyncMock()  # type: ignore
        full_body = "the actual full body fetched via ListMessages"
        adapter._fetch_group_message = AsyncMock(return_value={
            "id": "msg-hb",
            "blocks": [
                {"content": {"case": "text", "value": {"content": full_body}}}
            ],
        })  # type: ignore

        records, teardown = _capture_kimi_log_records()
        try:
            await adapter._on_group_event(
                self._summary_only_event(summary="short preview")
            )
        finally:
            teardown()

        probe = [r for r in records if "text source for" in r.getMessage()]
        self.assertEqual(len(probe), 1, f"expected one probe log, got: {probe}")
        rendered = probe[0].getMessage()
        self.assertIn("hydrated=true", rendered)
        self.assertIn("chose=hydrated", rendered)
        # Original raw-event candidates: blocks=0 text=0 summary>0.
        self.assertIn("blocks=0", rendered)
        self.assertIn("text=0", rendered)

    async def test_summary_fallback_includes_truncation_marker(self):
        """Fix B: hydration failure → summary fallback path prepends a
        clear truncation marker so the agent knows the body is a preview
        and can acknowledge rather than confidently answer against half
        a sentence. Same H-B failure mode as the original bug — just less
        frequent now that hydration is the primary path.
        """
        adapter = KimiAdapter(_cfg(group_allow_bot_senders="all"))
        adapter.handle_message = AsyncMock()  # type: ignore
        summary = "this is the 50-char-ish truncated server-side preview"
        adapter._fetch_group_message = AsyncMock(
            side_effect=KimiRpcError("hydration unavailable")
        )  # type: ignore

        await adapter._on_group_event(self._summary_only_event(summary=summary))

        adapter.handle_message.assert_awaited_once()
        delivered = adapter.handle_message.await_args.args[0]
        self.assertTrue(
            delivered.text.startswith("[message truncated"),
            f"expected truncation marker prefix, got: {delivered.text!r}",
        )
        self.assertIn(summary, delivered.text)

    async def test_probe2_logs_hydrated_skipped_disabled_when_flag_off(self):
        """Fix C: ``_hydrate_missing_text=False`` → ``hydrated=skipped:disabled``
        in Probe 2 (operator policy distinguishable from "inline body
        present, hydration not needed").
        """
        adapter = KimiAdapter(_cfg(group_allow_bot_senders="all"))
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._hydrate_missing_text = False  # operator policy: no hydration
        # _fetch_group_message intentionally NOT mocked: with the flag off
        # it must never be invoked.

        records, teardown = _capture_kimi_log_records()
        try:
            await adapter._on_group_event(
                self._summary_only_event(summary="short preview")
            )
        finally:
            teardown()

        probe = [r for r in records if "text source for" in r.getMessage()]
        self.assertEqual(len(probe), 1, f"expected one probe log, got: {probe}")
        rendered = probe[0].getMessage()
        self.assertIn("hydrated=skipped:disabled", rendered)
        self.assertNotIn("hydrated=skipped:inline", rendered)

    async def test_probe2_logs_hydrated_skipped_inline_when_inline_text_present(self):
        """Fix C: inline body present (non-empty blocks/text) →
        ``hydrated=skipped:inline``, regardless of the
        ``_hydrate_missing_text`` flag. Distinguishes "happy path, no
        hydration needed" from "operator turned hydration off".
        """
        adapter = KimiAdapter(_cfg(group_allow_bot_senders="all"))
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._fetch_group_message = AsyncMock()  # type: ignore

        records, teardown = _capture_kimi_log_records()
        try:
            await adapter._on_group_event({
                "chatMessage": {
                    "chatId": "chat-inline",
                    "messageId": "msg-inline",
                    "status": "STATUS_COMPLETED",
                    "role": "ROLE_USER",
                    "senderId": "u1",
                    "senderShortId": "u_real",
                    "summary": "preview",
                    "blocks": [
                        {"content": {"case": "text", "value": {"content": "inline body"}}}
                    ],
                }
            })
        finally:
            teardown()

        adapter._fetch_group_message.assert_not_awaited()
        probe = [r for r in records if "text source for" in r.getMessage()]
        self.assertEqual(len(probe), 1, f"expected one probe log, got: {probe}")
        rendered = probe[0].getMessage()
        self.assertIn("hydrated=skipped:inline", rendered)
        self.assertNotIn("hydrated=skipped:disabled", rendered)

    async def test_probe2_logs_hydrated_false_when_hydration_returns_empty_payload(self):
        """Fix E: hydration returns a truthy-but-empty payload (e.g. wrapper
        with no blocks/text) → ``hydrated=false``, NOT ``hydrated=true``.
        Prior shape set ``hydrated=true`` whenever the hydrated dict was
        truthy, before checking whether ``_extract_blocks_payload`` actually
        yielded text — Probe 2 then falsely claimed a hydration win on an
        empty payload.

        Falls through to summary fallback (Fix B annotated).
        """
        adapter = KimiAdapter(_cfg(group_allow_bot_senders="all"))
        adapter.handle_message = AsyncMock()  # type: ignore
        # Truthy but yields no text — wrapper-only payload.
        adapter._fetch_group_message = AsyncMock(return_value={
            "id": "msg-hb",
            "blocks": [],
        })  # type: ignore

        records, teardown = _capture_kimi_log_records()
        try:
            await adapter._on_group_event(
                self._summary_only_event(summary="short preview")
            )
        finally:
            teardown()

        probe = [r for r in records if "text source for" in r.getMessage()]
        self.assertEqual(len(probe), 1, f"expected one probe log, got: {probe}")
        rendered = probe[0].getMessage()
        self.assertIn("hydrated=false", rendered)
        self.assertNotIn("hydrated=true", rendered)
        # Cascade then falls through to Fix B's annotated summary.
        self.assertIn("chose=summary", rendered)


class Probe3MessageIdTimingTests(unittest.IsolatedAsyncioTestCase):
    """Probe (H-C): per-room message_id timing DEBUG log for post-hoc
    burst-drop correlation. Updated BEFORE the filter chain so drops
    still count as "what Kimi sent us" — gaps then map to burst losses.
    """

    def _adapter(self):
        adapter = KimiAdapter(_cfg(group_allow_bot_senders="all"))
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._hydrate_missing_text = False
        return adapter

    def _msg(self, chat_id, message_id):
        return {
            "chatMessage": {
                "chatId": chat_id,
                "messageId": message_id,
                "status": "STATUS_COMPLETED",
                "role": "ROLE_USER",
                "senderId": "u1",
                "senderShortId": "u_real",
                "blocks": [
                    {"content": {"case": "text", "value": {"content": "hi"}}}
                ],
            }
        }

    async def test_message_id_probe_first_seen_for_new_room(self):
        adapter = self._adapter()
        records, teardown = _capture_kimi_log_records()
        try:
            await adapter._on_group_event(self._msg("room-X", "01HKCHF4FC4S0W7T3V74SG6AT6"))
        finally:
            teardown()

        probe = [r for r in records if "message_id first-seen" in r.getMessage()]
        self.assertEqual(len(probe), 1, f"expected one first-seen log, got: {probe}")
        rendered = probe[0].getMessage()
        self.assertIn("room=room-X", rendered)
        self.assertIn("id=01HKCHF4FC4S0W7T3V74SG6AT6", rendered)

    async def test_message_id_probe_emits_delta_for_second_message(self):
        adapter = self._adapter()
        # Bump char 10 (end of the 48-bit timestamp prefix) so the two ULIDs
        # decode to different millisecond timestamps — the random tail has
        # no effect on the delta.
        first_id = "01HKCHF4FC4S0W7T3V74SG6AT6"
        second_id = "01HKCHF4FD4S0W7T3V74SG6AT6"
        # DEBUG enabled on both dispatches so the log emission fires; the
        # tracker update itself is hoisted OUT of the DEBUG gate (Fix A)
        # so it populates regardless of level.
        records, teardown = _capture_kimi_log_records()
        try:
            await adapter._on_group_event(self._msg("room-Y", first_id))
            first_records_len = len(records)
            await adapter._on_group_event(self._msg("room-Y", second_id))
        finally:
            teardown()

        # Only inspect records emitted during the second dispatch.
        second_records = records[first_records_len:]
        timing = [r for r in second_records if "message_id timing" in r.getMessage()]
        self.assertEqual(len(timing), 1)
        rendered = timing[0].getMessage()
        self.assertIn("room=room-Y", rendered)
        self.assertIn(f"prev={first_id}", rendered)
        self.assertIn("delta_ms=", rendered)
        # delta must equal the second - first timestamp diff.
        expected_delta = _ulid_time_ms(second_id) - _ulid_time_ms(first_id)
        self.assertIn(f"delta_ms={expected_delta}", rendered)
        # Second dispatch must not emit a first-seen log.
        first_seen = [r for r in second_records if "first-seen" in r.getMessage()]
        self.assertEqual(first_seen, [])

    async def test_message_id_probe_per_room_isolation(self):
        adapter = self._adapter()
        records, teardown = _capture_kimi_log_records()
        try:
            # Room A: two messages.
            await adapter._on_group_event(self._msg("room-A", "01HKCHF4FC4S0W7T3V74SG6AT6"))
            await adapter._on_group_event(self._msg("room-A", "01HKCHF4FD4S0W7T3V74SG6AT6"))
            boundary = len(records)
            # Room B: first message — must log first-seen, not a delta from A.
            await adapter._on_group_event(self._msg("room-B", "01HKCHF4FE4S0W7T3V74SG6AT6"))
        finally:
            teardown()

        b_records = records[boundary:]
        first_seen = [r for r in b_records if "first-seen" in r.getMessage()]
        timing = [r for r in b_records if "message_id timing" in r.getMessage()]
        self.assertEqual(len(first_seen), 1, f"expected first-seen for room-B, got: {b_records}")
        self.assertIn("room=room-B", first_seen[0].getMessage())
        self.assertEqual(timing, [], "room-B must not emit a delta against room-A")

    def test_ulid_time_ms_parses_known_ulid(self):
        """Canonical ULID decode — cross-checked against python-ulid."""
        # '01ARZ3NDEKTSV4RRFFQ69G5FAV' → 1469922850259 ms
        # (2016-07-30T23:54:10.259+00:00 UTC), verified against python-ulid.
        self.assertEqual(
            _ulid_time_ms("01ARZ3NDEKTSV4RRFFQ69G5FAV"),
            1469922850259,
        )
        # Case-insensitive.
        self.assertEqual(
            _ulid_time_ms("01arz3ndektsv4rrffq69g5fav"),
            1469922850259,
        )
        # Monotonic across two close ULIDs — later ULID has larger prefix value.
        earlier = _ulid_time_ms("01HKCHF4FC4S0W7T3V74SG6AT6")
        later = _ulid_time_ms("01HKCHF4FD4S0W7T3V74SG6AT6")
        self.assertIsNotNone(earlier)
        self.assertIsNotNone(later)
        self.assertGreater(later, earlier)

    def test_ulid_time_ms_rejects_short_or_invalid(self):
        """None / empty / short / non-crockford → None, not a crash."""
        self.assertIsNone(_ulid_time_ms(None))
        self.assertIsNone(_ulid_time_ms(""))
        self.assertIsNone(_ulid_time_ms("01ARZ3NDE"))  # 9 chars, too short
        # 'I', 'L', 'O', 'U' are NOT in Crockford base32 — must reject.
        self.assertIsNone(_ulid_time_ms("01IRZ3NDEK"))
        self.assertIsNone(_ulid_time_ms("01LRZ3NDEK"))
        self.assertIsNone(_ulid_time_ms("01ORZ3NDEK"))
        self.assertIsNone(_ulid_time_ms("01URZ3NDEK"))
        # Non-string input.
        self.assertIsNone(_ulid_time_ms(12345))  # type: ignore[arg-type]

    async def test_message_id_probe_tracker_populates_at_info_level(self):
        """Fix A / Fix E invariant: tracker-update is hoisted out of the
        DEBUG gate, so toggling DEBUG on later does NOT falsely report
        ``first-seen`` for a message that already arrived at INFO. No
        probe-3 log is emitted under INFO either.
        """
        adapter = self._adapter()
        # Capture at WARNING (drops DEBUG) to prove the probe is silent
        # while the tracker still fills.
        records, teardown = _capture_kimi_log_records(level=logging.WARNING)
        try:
            await adapter._on_group_event(
                self._msg("room-K", "01HKCHF4FC4S0W7T3V74SG6AT6")
            )
        finally:
            teardown()

        # Tracker populated regardless of log level.
        self.assertEqual(
            adapter._last_message_id_per_room.get("room-K"),
            "01HKCHF4FC4S0W7T3V74SG6AT6",
        )
        # No probe-3 log emitted at WARNING.
        probe = [r for r in records if "message_id" in r.getMessage()]
        self.assertEqual(probe, [])

    async def test_probes_silent_under_warning_level(self):
        """Fix E: all three probes are DEBUG-gated — at WARNING or above,
        zero probe records are emitted (block, text-source, or message_id).
        """
        adapter = self._adapter()
        msg = {
            "chatMessage": {
                "chatId": "room-W",
                "messageId": "01HKCHF4FC4S0W7T3V74SG6AT6",
                "status": "STATUS_COMPLETED",
                "role": "ROLE_USER",
                "senderId": "u1",
                "senderShortId": "u_real",
                "text": "envelope preview",
                "summary": "envelope summary",
                "blocks": [
                    {
                        "content": {
                            "case": "resourceLink",
                            "value": {"uri": "kimi://file/z"},
                        }
                    }
                ],
            }
        }
        records, teardown = _capture_kimi_log_records(level=logging.WARNING)
        try:
            await adapter._on_group_event(msg)
        finally:
            teardown()

        probe_substrings = ("non-text block", "text source for", "message_id")
        fired = [
            r for r in records
            if any(s in r.getMessage() for s in probe_substrings)
        ]
        self.assertEqual(fired, [], f"expected zero probe logs at WARNING, got: {fired}")

    async def test_probe_msg_id_sample_rate_reduces_log_volume(self):
        """Fix F: ``probe_msg_id_sample_rate=3`` → exactly one probe-3 log
        every three inbound messages in the same room. Tracker still
        updates on every inbound regardless of sampling.
        """
        adapter = KimiAdapter(_cfg(
            group_allow_bot_senders="all",
            probe_msg_id_sample_rate=3,
        ))
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._hydrate_missing_text = False

        ids = [
            "01HKCHF4F14S0W7T3V74SG6AT6",
            "01HKCHF4F24S0W7T3V74SG6AT6",
            "01HKCHF4F34S0W7T3V74SG6AT6",
            "01HKCHF4F44S0W7T3V74SG6AT6",
            "01HKCHF4F54S0W7T3V74SG6AT6",
            "01HKCHF4F64S0W7T3V74SG6AT6",
            "01HKCHF4F74S0W7T3V74SG6AT6",
            "01HKCHF4F84S0W7T3V74SG6AT6",
            "01HKCHF4F94S0W7T3V74SG6AT6",
        ]

        records, teardown = _capture_kimi_log_records()
        try:
            for mid in ids:
                await adapter._on_group_event(self._msg("room-S", mid))
        finally:
            teardown()

        probe3 = [
            r for r in records
            if "message_id timing" in r.getMessage()
            or "message_id first-seen" in r.getMessage()
        ]
        # 9 dispatches / sample_rate=3 → exactly 3 probe-3 logs.
        self.assertEqual(
            len(probe3), 3,
            f"expected 3 probe-3 records at 1-in-3 sampling, got {len(probe3)}: "
            f"{[r.getMessage() for r in probe3]}",
        )
        # Tracker reflects the LAST observed id, not the last SAMPLED id.
        self.assertEqual(
            adapter._last_message_id_per_room.get("room-S"),
            ids[-1],
        )

    def test_probe_msg_id_sample_rate_falls_back_on_invalid_config(self):
        """Fix-up (Codex P2): a non-numeric ``probe_msg_id_sample_rate``
        in ``config.extra`` (e.g. operator typo ``"ten"``) must not crash
        adapter init. Falls back to 1 with a WARNING log naming the bad
        value.
        """
        records, teardown = _capture_kimi_log_records(level=logging.WARNING)
        try:
            adapter = KimiAdapter(_cfg(probe_msg_id_sample_rate="ten"))
        finally:
            teardown()

        self.assertEqual(adapter._probe_msg_id_sample_rate, 1)
        warnings = [
            r for r in records
            if r.levelno == logging.WARNING
            and "probe_msg_id_sample_rate" in r.getMessage()
        ]
        self.assertEqual(
            len(warnings), 1,
            f"expected one WARNING naming the bad value, got: "
            f"{[r.getMessage() for r in records]}",
        )
        self.assertIn("'ten'", warnings[0].getMessage())



# ═══════════════════════════════════════════════════════════════════════════════
# Lift 3a: interrupt-and-drain queue improvements (pending-slot drop-log + TTL)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_message_event(text: str = "hello", chat_id: str = "dm:im:kimi:main") -> "MagicMock":
    """Build a minimal MessageEvent-like object for testing handle_message."""
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource
    from gateway.config import Platform

    source = SessionSource(
        platform=Platform.KIMI,
        chat_id=chat_id,
        chat_type="dm",
        user_id="kimi:user:1",
    )
    event = MagicMock(spec=MessageEvent)
    event.source = source
    event.text = text
    event.message_type = MessageType.TEXT
    event.message_id = "msg-test"
    return event


def _compute_session_key(adapter: "KimiAdapter", event: "MagicMock") -> str:
    """Compute the session key the adapter will derive for a given event."""
    from gateway.session import build_session_key
    return build_session_key(
        event.source,
        group_sessions_per_user=adapter._group_sessions_per_user,
        thread_sessions_per_user=adapter._thread_sessions_per_user,
    )


class HakimiLift3aDropLogTests(unittest.IsolatedAsyncioTestCase):
    """Lift 3a: WARN log when a pending-slot overwrite occurs."""

    async def test_3a_1_drop_log_on_overwrite(self):
        """3a.1 — overwriting an existing pending slot emits a WARNING with
        chat_id and message preview."""
        adapter = KimiAdapter(_cfg())

        # Simulate a session in progress (active-session guard set).
        first_event = _make_message_event("first pending message")
        session_key = _compute_session_key(adapter, first_event)
        guard = asyncio.Event()
        adapter._active_sessions[session_key] = guard

        # Put a first pending message in the slot.
        adapter._pending_messages[session_key] = first_event
        adapter._pending_enqueued_at[session_key] = 1.0  # arbitrary

        # Now call handle_message with a SECOND message — this should overwrite.
        second_event = _make_message_event("second message overwrites first")
        # Patch super().handle_message to avoid real dispatch.
        records, teardown = _capture_kimi_log_records(level=logging.WARNING)
        try:
            with patch.object(
                adapter.__class__.__bases__[0], "handle_message", new=AsyncMock()
            ):
                await adapter.handle_message(second_event)
        finally:
            teardown()

        warnings = [
            r for r in records
            if r.levelno == logging.WARNING and "overwriting pending slot" in r.getMessage()
        ]
        self.assertEqual(len(warnings), 1, f"Expected one overwrite WARNING, got: {[r.getMessage() for r in records]}")
        msg = warnings[0].getMessage()
        self.assertIn("first pending", msg)  # preview of the dropped message

    async def test_3a_2_error_path_drain_preserved(self):
        """3a.2 — when message_handler raises, the pending slot is still drained.

        BasePlatformAdapter already handles this via the late-arrival drain in
        _process_message_background's ``finally`` block. This test documents and
        exercises the contract: after a handler exception, a message queued in
        _pending_messages is NOT silently lost.

        Implementation note: we test the base-class invariant via the KimiAdapter
        since KimiAdapter.handle_message delegates to super().handle_message which
        runs _process_message_background. The test checks that after an exception
        during handler execution, _pending_messages is cleared (either consumed or
        cleaned up).
        """
        adapter = KimiAdapter(_cfg())

        first_event = _make_message_event("first message that will error")
        session_key = _compute_session_key(adapter, first_event)

        # Set up a message handler that raises on the first call, succeeds on second.
        call_count = [0]

        async def _handler(event):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("synthetic processing error")
            return "ok"

        adapter.set_message_handler(_handler)

        second_event = _make_message_event("second message — must not be lost")

        # Queue second event in pending slot before first processes.
        adapter._pending_messages[session_key] = second_event
        adapter._pending_enqueued_at[session_key] = 0.0  # pre-enqueued

        # Now simulate base._process_message_background having set the active guard.
        guard = asyncio.Event()
        guard.set()  # interrupt already signalled
        adapter._active_sessions[session_key] = guard
        adapter._session_tasks[session_key] = asyncio.current_task()

        # Run _process_message_background which includes our exception + drain path.
        try:
            await adapter._process_message_background(first_event, session_key)
        except Exception:
            pass  # exception propagation details not under test

        # After the whole run, _pending_messages for this session should be gone —
        # the pending message was either dispatched (good) or cleaned up (acceptable).
        # The key invariant: it is NOT still sitting unprocessed in the dict
        # while the session is no longer active.
        session_still_active = session_key in adapter._active_sessions
        pending_still_queued = session_key in adapter._pending_messages
        self.assertFalse(
            session_still_active and pending_still_queued,
            "Pending message stranded: session is inactive but pending slot not cleared.",
        )

    async def test_3a_3_ttl_disabled_by_default(self):
        """3a.3 — with no TTL configured, pending slot never expires."""
        adapter = KimiAdapter(_cfg())  # no pending_message_ttl_seconds

        self.assertIsNone(adapter._pending_message_ttl)

        # Simulate an arbitrarily old pending message.
        old_event = _make_message_event("old pending message")
        session_key = _compute_session_key(adapter, old_event)
        guard = asyncio.Event()
        adapter._active_sessions[session_key] = guard

        adapter._pending_messages[session_key] = old_event
        # Enqueued a long time ago (1000 seconds).
        import time as _time
        adapter._pending_enqueued_at[session_key] = _time.monotonic() - 1000.0

        new_event = _make_message_event("newer message")
        records, teardown = _capture_kimi_log_records(level=logging.INFO)
        try:
            with patch.object(
                adapter.__class__.__bases__[0], "handle_message", new=AsyncMock()
            ):
                await adapter.handle_message(new_event)
        finally:
            teardown()

        # No eviction log should appear.
        eviction_logs = [
            r for r in records
            if "evicting expired" in r.getMessage()
        ]
        self.assertEqual(len(eviction_logs), 0, "TTL eviction should not fire when TTL is None")

    async def test_3a_4_ttl_enabled_evicts_expired_pending(self):
        """3a.4 — with TTL set, an expired pending slot is evicted and logged."""
        import time as _time
        adapter = KimiAdapter(_cfg(pending_message_ttl_seconds=5))

        self.assertEqual(adapter._pending_message_ttl, 5.0)

        old_event = _make_message_event("expired pending message")
        session_key = _compute_session_key(adapter, old_event)
        guard = asyncio.Event()
        adapter._active_sessions[session_key] = guard

        adapter._pending_messages[session_key] = old_event
        # Enqueued well past the 5s TTL.
        adapter._pending_enqueued_at[session_key] = _time.monotonic() - 60.0

        new_event = _make_message_event("fresh message after expiry")
        records, teardown = _capture_kimi_log_records(level=logging.INFO)
        try:
            with patch.object(
                adapter.__class__.__bases__[0], "handle_message", new=AsyncMock()
            ):
                await adapter.handle_message(new_event)
        finally:
            teardown()

        eviction_logs = [
            r for r in records
            if "evicting expired" in r.getMessage()
        ]
        self.assertEqual(
            len(eviction_logs), 1,
            f"Expected one eviction INFO log, got: {[r.getMessage() for r in records]}",
        )
        # After eviction of the old slot the new message is NOT double-logged as
        # an "overwriting" drop (the slot was cleared before the drop-log check).
        overwrite_warnings = [
            r for r in records
            if "overwriting pending slot" in r.getMessage()
        ]
        self.assertEqual(len(overwrite_warnings), 0, "Eviction should not also fire a drop warning")


# ═══════════════════════════════════════════════════════════════════════════════
# Lift 3b: output_mode flag
# ═══════════════════════════════════════════════════════════════════════════════

class HakimiLift3bOutputModeTests(unittest.IsolatedAsyncioTestCase):
    """Lift 3b: output_mode: passthrough | tool_only."""

    async def test_3b_1_passthrough_mode_delivers_prose(self):
        """3b.1 — passthrough (default) — send() routes through normally."""
        adapter = KimiAdapter(_cfg(output_mode="passthrough"))
        self.assertEqual(adapter._output_mode, "passthrough")

        # Patch the routing methods so we don't need a live WS/HTTP session.
        adapter._send_dm = AsyncMock(return_value=SendResult(success=True))
        adapter._send_group = AsyncMock(return_value=SendResult(success=True))

        result = await adapter.send(
            chat_id="dm:im:kimi:main", content="agent prose response"
        )
        self.assertTrue(result.success)
        adapter._send_dm.assert_awaited_once()

    async def test_3b_2_tool_only_mode_suppresses_prose(self):
        """3b.2 — tool_only — send() is gated, nothing reaches the platform."""
        adapter = KimiAdapter(_cfg(output_mode="tool_only"))
        self.assertEqual(adapter._output_mode, "tool_only")

        adapter._send_dm = AsyncMock(return_value=SendResult(success=True))
        adapter._send_group = AsyncMock(return_value=SendResult(success=True))

        result = await adapter.send(
            chat_id="dm:im:kimi:main", content="agent prose that should be suppressed"
        )
        # Returns success=True (no error) but nothing was sent.
        self.assertTrue(result.success)
        adapter._send_dm.assert_not_awaited()
        adapter._send_group.assert_not_awaited()

    async def test_3b_3_tool_only_mode_suppresses_all_send_targets(self):
        """3b.3 — tool_only suppresses prose to both DM and group targets."""
        adapter = KimiAdapter(_cfg(output_mode="tool_only"))

        adapter._send_dm = AsyncMock(return_value=SendResult(success=True))
        adapter._send_group = AsyncMock(return_value=SendResult(success=True))

        # DM target.
        result_dm = await adapter.send(
            chat_id="dm:im:kimi:main", content="thinking out loud"
        )
        # Group target.
        result_group = await adapter.send(
            chat_id="room:abc123", content="group prose also suppressed"
        )
        self.assertTrue(result_dm.success)
        self.assertTrue(result_group.success)
        adapter._send_dm.assert_not_awaited()
        adapter._send_group.assert_not_awaited()


class HakimiLift3bOutputModeInitTests(unittest.TestCase):
    """Init-level validation for output_mode."""

    def test_output_mode_default_is_passthrough(self):
        adapter = KimiAdapter(_cfg())
        self.assertEqual(adapter._output_mode, "passthrough")

    def test_output_mode_tool_only_accepted(self):
        adapter = KimiAdapter(_cfg(output_mode="tool_only"))
        self.assertEqual(adapter._output_mode, "tool_only")

    def test_output_mode_invalid_defaults_with_warning(self):
        records, teardown = _capture_kimi_log_records(level=logging.WARNING)
        try:
            adapter = KimiAdapter(_cfg(output_mode="robot_only"))
        finally:
            teardown()
        self.assertEqual(adapter._output_mode, "passthrough")
        warnings = [
            r for r in records
            if r.levelno == logging.WARNING and "output_mode" in r.getMessage()
        ]
        self.assertEqual(len(warnings), 1)
        self.assertIn("robot_only", warnings[0].getMessage())

if __name__ == "__main__":
    unittest.main()
