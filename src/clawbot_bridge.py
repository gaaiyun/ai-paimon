"""
ClawBot (OpenClaw) WebSocket → OpenAI-Compatible API Bridge

Bridges the OpenClaw Gateway's WebSocket protocol v3 (including Ed25519
device authentication) to a standard OpenAI ``/v1/chat/completions``
HTTP endpoint so that any OpenAI-compatible client can talk to ClawBot.

Configuration
-------------
All secrets are loaded from environment variables.  Copy ``.env.example``
to ``.env`` and fill in your values, or export them in your shell.

Usage::

    pip install fastapi uvicorn websockets cryptography python-dotenv
    python src/clawbot_bridge.py            # defaults to port 5001
    python src/clawbot_bridge.py --port 6000
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_private_key,
)
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
import sys as _sys

if _sys.platform == "win32":
    for _stream in (_sys.stdout, _sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("clawbot_bridge")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

OPENCLAW_WS_URL: str = os.getenv("OPENCLAW_WS_URL", "ws://127.0.0.1:18789")
OPENCLAW_TOKEN: str = os.getenv("OPENCLAW_TOKEN", "")
OPENCLAW_SESSION: str = os.getenv("OPENCLAW_SESSION", "agent:main:main")
OPENCLAW_DEVICE_ID: str = os.getenv("OPENCLAW_DEVICE_ID", "")
OPENCLAW_DEVICE_TOKEN: str = os.getenv("OPENCLAW_DEVICE_TOKEN", "")
OPENCLAW_PRIVATE_KEY: str = os.getenv(
    "OPENCLAW_PRIVATE_KEY",
    "-----BEGIN PRIVATE KEY-----\nREPLACE_ME\n-----END PRIVATE KEY-----\n",
)

SCOPES = [
    "operator.admin",
    "operator.write",
    "operator.read",
    "operator.approvals",
    "operator.pairing",
    "operator.talk.secrets",
]


# ---------------------------------------------------------------------------
# Crypto helpers (OpenClaw v3 protocol)
# ---------------------------------------------------------------------------

def _b64url_encode(data: bytes) -> str:
    """Base64url encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _get_raw_public_key_b64url(pem: str) -> str:
    """Extract raw Ed25519 public key bytes and return base64url encoded."""
    private_key: Ed25519PrivateKey = load_pem_private_key(
        pem.encode(), password=None
    )
    raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return _b64url_encode(raw)


def _build_auth_payload_v3(
    *,
    device_id: str,
    client_id: str,
    client_mode: str,
    role: str,
    scopes: list[str],
    signed_at_ms: int,
    token: str,
    nonce: str,
    platform: str,
) -> str:
    """Build the v3 pipe-delimited payload for device signing."""
    return "|".join([
        "v3",
        device_id,
        client_id,
        client_mode,
        role,
        ",".join(scopes),
        str(signed_at_ms),
        token or "",
        nonce,
        (platform or "").strip().lower(),
        "",  # deviceFamily (empty)
    ])


def _sign_payload(pem: str, payload: str) -> str:
    """Sign the payload string with Ed25519 private key, return base64url."""
    private_key: Ed25519PrivateKey = load_pem_private_key(
        pem.encode(), password=None
    )
    sig = private_key.sign(payload.encode("utf-8"))
    return _b64url_encode(sig)


# ---------------------------------------------------------------------------
# OpenClaw WebSocket Bridge
# ---------------------------------------------------------------------------

class OpenClawBridge:
    """Maintains a persistent WebSocket connection to the OpenClaw Gateway."""

    def __init__(self) -> None:
        self.ws: websockets.WebSocketClientProtocol | None = None
        self._connected: bool = False
        self._lock = asyncio.Lock()

    # -- connection / auth --------------------------------------------------

    async def connect(self) -> None:
        """Connect (if not already) and authenticate with the gateway."""
        async with self._lock:
            if self._connected and self.ws:
                try:
                    if not getattr(self.ws, "closed", False):
                        return
                except Exception:
                    pass
            await self._do_connect()

    async def _do_connect(self) -> None:
        """Perform the full connect → challenge → connect handshake."""
        logger.info("Connecting to OpenClaw Gateway: %s", OPENCLAW_WS_URL)
        self.ws = await websockets.connect(
            OPENCLAW_WS_URL,
            ping_interval=None,
            ping_timeout=None,
        )

        # Step 1: receive challenge event with nonce
        raw = await asyncio.wait_for(self.ws.recv(), timeout=10)
        msg: dict[str, Any] = json.loads(raw)

        if msg.get("event") != "connect.challenge":
            raise ConnectionError(f"Expected connect.challenge, got: {msg}")

        nonce = msg["payload"]["nonce"]
        logger.info("  Challenge nonce: %s…", nonce[:20])

        # Step 2: build v3 device auth payload and sign it
        signed_at_ms = int(time.time() * 1000)
        public_key_b64url = _get_raw_public_key_b64url(OPENCLAW_PRIVATE_KEY)

        payload_str = _build_auth_payload_v3(
            device_id=OPENCLAW_DEVICE_ID,
            client_id="cli",
            client_mode="cli",
            role="operator",
            scopes=SCOPES,
            signed_at_ms=signed_at_ms,
            token=OPENCLAW_TOKEN,
            nonce=nonce,
            platform="win32",
        )
        signature = _sign_payload(OPENCLAW_PRIVATE_KEY, payload_str)

        # Step 3: send connect request with device identity
        connect_req = {
            "type": "req",
            "id": str(uuid.uuid4()),
            "method": "connect",
            "params": {
                "minProtocol": 3,
                "maxProtocol": 3,
                "client": {
                    "id": "cli",
                    "mode": "cli",
                    "version": "1.0.0",
                    "platform": "win32",
                },
                "auth": {
                    "token": OPENCLAW_TOKEN,
                    "deviceToken": OPENCLAW_DEVICE_TOKEN,
                },
                "role": "operator",
                "scopes": SCOPES,
                "device": {
                    "id": OPENCLAW_DEVICE_ID,
                    "publicKey": public_key_b64url,
                    "signature": signature,
                    "signedAt": signed_at_ms,
                    "nonce": nonce,
                },
            },
        }
        await self.ws.send(json.dumps(connect_req))
        logger.info("  Sent connect with device identity")

        # Step 4: read responses until we get hello-ok or error
        for _ in range(10):
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=10)
            except asyncio.TimeoutError:
                raise ConnectionError("Timeout waiting for connect response")

            resp: dict[str, Any] = json.loads(raw)

            if resp.get("type") == "res":
                if resp.get("ok"):
                    payload = resp.get("payload", {})
                    if isinstance(payload, dict) and payload.get("type") == "hello-ok":
                        granted = payload.get("auth", {}).get("scopes", [])
                        logger.info(
                            "✅ Authentication successful! Granted scopes: %s",
                            granted,
                        )
                        self._connected = True
                        return
                    # Some other ok response — keep reading
                    logger.info("  Got ok response: %s", json.dumps(resp, ensure_ascii=False)[:300])
                else:
                    err = resp.get("error", {})
                    raise ConnectionError(
                        f"Authentication failed: {err}"
                    )
            elif resp.get("type") == "event":
                # Drain events (health, etc.) while waiting for hello-ok
                logger.debug("  Draining event: %s", resp.get("event", ""))

        raise ConnectionError("Never received hello-ok")

    # -- channel / talk -----------------------------------------------------

    async def _ensure_session(self) -> str:
        """Create a session and subscribe to its events. Returns the session key."""
        assert self.ws is not None
        sid = str(uuid.uuid4())
        await self.ws.send(json.dumps({
            "type": "req",
            "id": sid,
            "method": "sessions.create",
            "params": {},
        }))

        session_key = ""
        for _ in range(10):
            raw = await asyncio.wait_for(self.ws.recv(), timeout=10)
            msg: dict[str, Any] = json.loads(raw)
            if msg.get("id") == sid and msg.get("ok"):
                pd = msg.get("payload", {})
                session_key = pd.get("key", "")
                break

        if not session_key:
            raise ConnectionError("Failed to create session")

        # Subscribe to session events to receive assistant replies
        sub_id = str(uuid.uuid4())
        await self.ws.send(json.dumps({
            "type": "req",
            "id": sub_id,
            "method": "sessions.subscribe",
            "params": {"key": session_key},
        }))

        for _ in range(5):
            raw = await asyncio.wait_for(self.ws.recv(), timeout=5)
            msg = json.loads(raw)
            if msg.get("id") == sub_id:
                break

        logger.info("  Session created: %s", session_key)
        return session_key

    async def send_chat(self, message: str, timeout: float = 120) -> str:
        """Send a chat message and collect the assistant reply."""
        async with self._lock:
            if not self._connected or not self.ws or getattr(self.ws, "closed", False):
                await self._do_connect()

            session_key = await self._ensure_session()
            assert self.ws is not None

            req_id = str(uuid.uuid4())
            await self.ws.send(json.dumps({
                "type": "req",
                "id": req_id,
                "method": "sessions.send",
                "params": {
                    "key": session_key,
                    "message": message,
                },
            }))
            logger.info("📤 Sent msg_id=%s via session %s", req_id, session_key[:30])

            # Collect assistant text from session events
            assistant_texts: list[str] = []
            deadline = time.time() + timeout

            while time.time() < deadline:
                try:
                    remaining = max(0.1, deadline - time.time())
                    raw = await asyncio.wait_for(
                        self.ws.recv(), timeout=min(remaining, 30)
                    )
                    msg: dict[str, Any] = json.loads(raw)
                except asyncio.TimeoutError:
                    if assistant_texts:
                        break
                    continue
                except websockets.ConnectionClosed:
                    self._connected = False
                    break

                msg_type = msg.get("type", "")
                msg_id = msg.get("id", "")
                event = msg.get("event", "")

                # Direct response to our send request
                if msg_type == "res" and msg_id == req_id:
                    if msg.get("ok") is False:
                        err = msg.get("error", {})
                        err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                        logger.error("  sessions.send error: %s", err_msg)
                        raise RuntimeError(f"sessions.send failed: {err_msg}")
                    continue

                # Session events carry the AI's reply
                if msg_type == "event":
                    payload = msg.get("payload", msg.get("data", {}))
                    if not isinstance(payload, dict):
                        continue

                    # agent stream events carry the assistant text
                    if event == "agent":
                        stream = payload.get("stream", "")
                        data = payload.get("data", {})
                        if stream == "assistant" and isinstance(data, dict):
                            text = data.get("text", "")
                            if text:
                                # If replace=True, overwrite last entry
                                if data.get("replace") and assistant_texts:
                                    assistant_texts[-1] = text
                                else:
                                    assistant_texts.append(text)
                        elif stream == "lifecycle" and isinstance(data, dict):
                            if data.get("phase") == "end":
                                break

                    # chat events with final state contain the complete reply
                    elif event == "chat":
                        state = payload.get("state", "")
                        chat_msg = payload.get("message", {})
                        if isinstance(chat_msg, dict) and chat_msg.get("role") == "assistant":
                            content = chat_msg.get("content", "")
                            if isinstance(content, list):
                                for item in content:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        assistant_texts.append(item.get("text", ""))
                            elif isinstance(content, str) and content:
                                assistant_texts.append(content)
                        if state == "final":
                            break

            # Take the last assistant text (the final reply)
            reply = assistant_texts[-1].strip() if assistant_texts else ""
            if not reply:
                reply = "（ClawBot did not return a valid reply — check Gateway status and Agent config）"
            logger.info("📥 Reply: %s…", reply[:80])
            return reply

    # -- lifecycle ----------------------------------------------------------

    async def close(self) -> None:
        """Gracefully close the WebSocket connection."""
        if self.ws:
            await self.ws.close()
        self._connected = False


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

bridge = OpenClawBridge()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Connect on startup; close on shutdown."""
    try:
        await bridge.connect()
    except Exception as exc:
        logger.warning("Initial connection failed (will retry on request): %s", exc)
    yield
    await bridge.close()


app = FastAPI(
    title="ClawBot Bridge",
    description="OpenClaw WebSocket → OpenAI-compatible REST bridge",
    lifespan=lifespan,
)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    """OpenAI-compatible Chat Completions endpoint.

    Extracts conversation history and the latest user message, formats them
    into a single structured message for OpenClaw's ``sessions.send``.

    System prompt is intentionally NOT injected here — OpenClaw manages
    persona through its workspace files (IDENTITY.md / SOUL.md), and the
    VTuber persona_prompt is already conveyed via conversation context.
    """
    body = await request.json()
    messages = body.get("messages", [])

    conversation_history = [
        msg for msg in messages
        if msg.get("role") in ("user", "assistant")
    ]

    # Truncate to last 10 messages to avoid token overflow
    if len(conversation_history) > 10:
        conversation_history = conversation_history[-10:]

    user_msg = ""
    for msg in reversed(conversation_history):
        if msg.get("role") == "user":
            user_msg = msg.get("content", "")
            break

    if not user_msg:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "No user message found", "type": "invalid_request"}},
        )

    # Build structured message with conversation context + current input
    full_message = ""
    if len(conversation_history) > 1:
        full_message += "[对话历史]\n"
        for msg in conversation_history[:-1]:
            role_label = "用户" if msg.get("role") == "user" else "派蒙"
            full_message += f"{role_label}：{msg.get('content', '')}\n"
        full_message += "\n"
    full_message += f"[当前输入]\n用户：{user_msg}"

    try:
        reply = await bridge.send_chat(full_message)
    except Exception as exc:
        logger.error("Communication error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"message": f"ClawBot error: {exc}", "type": "server_error"}},
        )

    return JSONResponse(
        content={
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "clawbot",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": len(full_message),
                "completion_tokens": len(reply),
                "total_tokens": len(full_message) + len(reply),
            },
        }
    )


@app.get("/v1/models")
async def list_models() -> JSONResponse:
    """List available models."""
    return JSONResponse(
        content={"data": [{"id": "clawbot", "object": "model", "owned_by": "openclaw"}]}
    )


@app.get("/health")
async def health() -> JSONResponse:
    """Health check."""
    return JSONResponse(content={"status": "ok", "connected": bridge._connected})


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="ClawBot OpenAI-compatible bridge")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=5001, help="Bind port")
    args = parser.parse_args()

    logger.info("🦞 ClawBot Bridge starting…")
    logger.info("   OpenClaw: %s", OPENCLAW_WS_URL)
    logger.info("   API: http://%s:%d/v1/chat/completions", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
