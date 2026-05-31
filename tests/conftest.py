"""Shared pytest fixtures and a fake OpenClaw WebSocket.

These tests are fully offline: no real OpenClaw Gateway, no network. The
:class:`FakeWebSocket` scripts the server side of the v3 protocol so we can
assert the bridge's handshake / session / send contract.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import pytest

# Make ``src`` importable and inject a dummy Ed25519 key BEFORE importing the
# bridge so the module-level config picks up a valid (test-only) key.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# A throwaway Ed25519 private key generated for tests only (never used in prod).
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    NoEncryption,
    PrivateFormat,
)

_TEST_KEY_PEM = (
    Ed25519PrivateKey.generate()
    .private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    .decode()
)
os.environ.setdefault("OPENCLAW_PRIVATE_KEY", _TEST_KEY_PEM)
os.environ.setdefault("OPENCLAW_DEVICE_ID", "test-device")
os.environ.setdefault("OPENCLAW_TOKEN", "test-token")


class FakeWebSocket:
    """Scripts the OpenClaw Gateway side of the v3 protocol for tests.

    The bridge reads handshake frames via ``recv()`` and then iterates the
    socket (``async for``) in its receive loop. This fake records everything
    the bridge sends, and replies to ``req`` frames with scripted ``res``
    frames plus session events, pushed onto an internal queue that backs both
    ``recv()`` and async iteration.
    """

    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict[str, Any]] = []
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        self._challenge_sent = False
        # session_key -> already created (for idempotency assertions)
        self.created_sessions: list[str] = []
        self._session_counter = 0
        # When True, ``sessions.send`` is answered AND a reply event is queued.
        self.reply_text = "派蒙在这里哦！"

    # -- helpers ------------------------------------------------------------

    def _push(self, obj: dict[str, Any]) -> None:
        self._inbox.put_nowait(json.dumps(obj))

    # -- protocol surface used by the bridge --------------------------------

    async def recv(self) -> str:
        # First read is always the connect.challenge.
        if not self._challenge_sent:
            self._challenge_sent = True
            return json.dumps({
                "event": "connect.challenge",
                "payload": {"nonce": "test-nonce-1234567890abcdef"},
            })
        return await self._inbox.get()

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        method = msg.get("method")
        msg_id = msg.get("id")

        if method == "connect":
            # Accept the device auth and return hello-ok.
            self._push({
                "type": "res",
                "id": msg_id,
                "ok": True,
                "payload": {"type": "hello-ok", "auth": {"scopes": ["operator.read"]}},
            })
        elif method == "sessions.create":
            self._session_counter += 1
            key = f"session-{self._session_counter}"
            self.created_sessions.append(key)
            self._push({"type": "res", "id": msg_id, "ok": True, "payload": {"key": key}})
        elif method == "sessions.subscribe":
            self._push({"type": "res", "id": msg_id, "ok": True, "payload": {}})
        elif method == "sessions.send":
            key = msg.get("params", {}).get("key", "")
            # Ack the send.
            self._push({"type": "res", "id": msg_id, "ok": True, "payload": {}})
            # Then stream the assistant reply as session events.
            self._push({
                "type": "event",
                "event": "agent",
                "payload": {
                    "key": key,
                    "stream": "assistant",
                    "data": {"text": self.reply_text},
                },
            })
            self._push({
                "type": "event",
                "event": "agent",
                "payload": {
                    "key": key,
                    "stream": "lifecycle",
                    "data": {"phase": "end"},
                },
            })

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        if self.closed:
            raise StopAsyncIteration
        return await self._inbox.get()


@pytest.fixture
def fake_ws() -> FakeWebSocket:
    return FakeWebSocket()


@pytest.fixture
def patched_bridge(monkeypatch, fake_ws):
    """Return a fresh bridge whose ``websockets.connect`` yields ``fake_ws``."""
    import clawbot_bridge

    async def _fake_connect(*_args, **_kwargs):
        return fake_ws

    monkeypatch.setattr(clawbot_bridge.websockets, "connect", _fake_connect)
    bridge = clawbot_bridge.OpenClawBridge()
    return bridge, fake_ws
