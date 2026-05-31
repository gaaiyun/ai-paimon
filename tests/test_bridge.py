"""Offline contract tests for the OpenClaw → OpenAI bridge.

All network I/O is mocked via ``FakeWebSocket`` (see conftest). No real
Gateway is required, and no secrets are used beyond a throwaway test key.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import clawbot_bridge
from clawbot_bridge import ChatCompletionRequest

# ---------------------------------------------------------------------------
# v3 handshake contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v3_handshake_sends_signed_connect(patched_bridge):
    """connect() completes the v3 challenge → signed connect → hello-ok flow."""
    bridge, fake_ws = patched_bridge

    await bridge.connect()
    assert bridge._connected is True

    connect_frames = [m for m in fake_ws.sent if m.get("method") == "connect"]
    assert len(connect_frames) == 1

    params = connect_frames[0]["params"]
    # Protocol version pinned to 3.
    assert params["minProtocol"] == 3 and params["maxProtocol"] == 3
    device = params["device"]
    # The device block must carry a public key, a signature, and the nonce.
    assert device["id"] == "test-device"
    assert device["publicKey"]
    assert device["signature"]
    assert device["nonce"] == "test-nonce-1234567890abcdef"

    await bridge.close()


# ---------------------------------------------------------------------------
# Session reuse (fixes P1-b)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_is_reused_across_turns(patched_bridge):
    """Two turns on the same conversation must reuse one OpenClaw session."""
    bridge, fake_ws = patched_bridge

    reply1 = await bridge.send_chat("你好", conversation_id="conv-A")
    reply2 = await bridge.send_chat("再问一句", conversation_id="conv-A")

    assert reply1 == fake_ws.reply_text
    assert reply2 == fake_ws.reply_text
    # Exactly ONE session created despite two sends — memory is preserved.
    assert len(fake_ws.created_sessions) == 1

    send_frames = [m for m in fake_ws.sent if m.get("method") == "sessions.send"]
    assert len(send_frames) == 2
    # Both sends targeted the same session key.
    keys = {m["params"]["key"] for m in send_frames}
    assert len(keys) == 1

    await bridge.close()


@pytest.mark.asyncio
async def test_distinct_conversations_get_distinct_sessions(patched_bridge):
    """Different conversation ids must map to different sessions."""
    bridge, fake_ws = patched_bridge

    await bridge.send_chat("hi", conversation_id="conv-A")
    await bridge.send_chat("hi", conversation_id="conv-B")

    assert len(fake_ws.created_sessions) == 2

    await bridge.close()


# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_chat_collects_assistant_reply(patched_bridge):
    """The assistant text streamed via session events is returned."""
    bridge, fake_ws = patched_bridge
    fake_ws.reply_text = "甜甜花酿鸡最好吃啦！"

    reply = await bridge.send_chat("吃什么", conversation_id="conv-X")

    assert reply == "甜甜花酿鸡最好吃啦！"

    await bridge.close()


# ---------------------------------------------------------------------------
# End-to-end through the HTTP endpoint (TestClient drives lifespan)
# ---------------------------------------------------------------------------

def test_chat_completions_endpoint(monkeypatch, fake_ws):
    """POST /v1/chat/completions returns an OpenAI-shaped completion."""

    async def _fake_connect(*_a, **_k):
        return fake_ws

    monkeypatch.setattr(clawbot_bridge.websockets, "connect", _fake_connect)
    # Use a fresh bridge instance bound to the app for isolation.
    monkeypatch.setattr(clawbot_bridge, "bridge", clawbot_bridge.OpenClawBridge())

    with TestClient(clawbot_bridge.app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "clawbot",
                "user": "traveler-1",
                "messages": [
                    {"role": "user", "content": "你好派蒙"},
                ],
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"] == fake_ws.reply_text


# ---------------------------------------------------------------------------
# Pydantic validation (fixes P1-d)
# ---------------------------------------------------------------------------

def test_request_model_rejects_empty_messages():
    """An empty messages list is rejected by the request model."""
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate({"messages": []})


def test_request_model_normalises_list_content():
    """OpenAI structured content parts collapse to a plain string."""
    req = ChatCompletionRequest.model_validate({
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "嗨"}, {"type": "text", "text": "派蒙"}]}
        ]
    })
    assert req.messages[0].content == "嗨派蒙"


def test_endpoint_rejects_malformed_body(monkeypatch, fake_ws):
    """The HTTP endpoint returns 422 for a body with no messages."""

    async def _fake_connect(*_a, **_k):
        return fake_ws

    monkeypatch.setattr(clawbot_bridge.websockets, "connect", _fake_connect)
    monkeypatch.setattr(clawbot_bridge, "bridge", clawbot_bridge.OpenClawBridge())

    with TestClient(clawbot_bridge.app) as client:
        resp = client.post("/v1/chat/completions", json={"model": "clawbot"})

    assert resp.status_code == 422
