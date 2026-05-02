"""
ClawBot (OpenClaw) WebSocket → OpenAI-Compatible API Bridge

Bridges the OpenClaw Gateway's WebSocket protocol (including Ed25519
challenge-response authentication) to a standard OpenAI
``/v1/chat/completions`` HTTP endpoint so that any OpenAI-compatible
client (e.g. Open-LLM-VTuber) can talk to ClawBot seamlessly.

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
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Logging — ensure UTF-8 on Windows console
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
# Configuration — loaded from environment / .env file
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


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------

def _sign_nonce(nonce: str) -> str:
    """Sign *nonce* with the Ed25519 private key and return a base64url signature."""
    private_key: Ed25519PrivateKey = load_pem_private_key(
        OPENCLAW_PRIVATE_KEY.encode(), password=None
    )  # type: ignore[assignment]
    signature = private_key.sign(nonce.encode("utf-8"))
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


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
            if self._connected and self.ws and self.ws.open:
                return
            await self._do_connect()

    async def _do_connect(self) -> None:
        """Perform the full connect → challenge → solve handshake."""
        logger.info("Connecting to OpenClaw Gateway: %s", OPENCLAW_WS_URL)
        self.ws = await websockets.connect(OPENCLAW_WS_URL)

        # Step 1: send ``connect`` request
        connect_id = str(uuid.uuid4())
        connect_req = {
            "type": "req",
            "id": connect_id,
            "method": "connect",
            "params": {
                "auth": {"token": OPENCLAW_TOKEN},
                "client": {
                    "id": "cli",
                    "mode": "cli",
                    "version": "1.0.0",
                    "platform": "win32",
                },
                "maxProtocol": 1,
                "role": "operator",
                "scopes": ["operator.admin"],
            },
        }
        await self.ws.send(json.dumps(connect_req))

        # Step 2: handle challenge
        raw = await asyncio.wait_for(self.ws.recv(), timeout=10)
        msg: dict[str, Any] = json.loads(raw)
        logger.info(
            "  Received: type=%s, method=%s",
            msg.get("type"),
            msg.get("method", msg.get("event", "")),
        )

        if msg.get("type") == "event" and "challenge" in str(msg.get("event", "")):
            payload = msg.get("payload", msg.get("data", msg.get("params", {})))
            nonce = payload.get("nonce", "")
            logger.info("  Challenge nonce received: %s…", nonce[:20])

            signature = _sign_nonce(nonce)
            signed_at = int(time.time() * 1000)

            solve_req = {
                "type": "req",
                "id": str(uuid.uuid4()),
                "method": "connect.solve",
                "params": {
                    "deviceId": OPENCLAW_DEVICE_ID,
                    "signature": signature,
                    "signedAt": signed_at,
                },
            }
            await self.ws.send(json.dumps(solve_req))

            raw2 = await asyncio.wait_for(self.ws.recv(), timeout=10)
            msg2: dict[str, Any] = json.loads(raw2)
            logger.info("  Solve response: type=%s, error=%s", msg2.get("type"), msg2.get("error"))

            if msg2.get("type") == "res" and msg2.get("error") is None:
                logger.info("✅ Authentication successful!")
                self._connected = True
                return

            # There may be extra messages in the queue
            try:
                raw3 = await asyncio.wait_for(self.ws.recv(), timeout=5)
                msg3: dict[str, Any] = json.loads(raw3)
                if msg3.get("type") == "res" and msg3.get("error") is None:
                    logger.info("✅ Authentication successful!")
                    self._connected = True
                    return
            except asyncio.TimeoutError:
                pass

            raise ConnectionError(f"Authentication failed: {msg2}")

        elif msg.get("type") == "res" and msg.get("error") is None:
            logger.info("✅ Direct authentication successful!")
            self._connected = True
        else:
            raise ConnectionError(f"Unexpected auth response: {msg}")

    # -- chat ---------------------------------------------------------------

    async def send_chat(self, message: str, timeout: float = 120) -> str:
        """Send a chat message and collect the assistant reply."""
        await self.connect()
        assert self.ws is not None

        req_id = str(uuid.uuid4())
        chat_req = {
            "type": "req",
            "id": req_id,
            "method": "chat.send",
            "params": {
                "sessionKey": OPENCLAW_SESSION,
                "message": message,
            },
        }
        await self.ws.send(json.dumps(chat_req))
        logger.info("📤 Sent: %s…", message[:50])

        reply_parts: list[str] = []
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                remaining = max(0.1, deadline - time.time())
                raw = await asyncio.wait_for(
                    self.ws.recv(), timeout=min(remaining, 30)
                )
                msg: dict[str, Any] = json.loads(raw)
            except asyncio.TimeoutError:
                if reply_parts:
                    break
                continue
            except websockets.ConnectionClosed:
                self._connected = False
                break

            msg_type = msg.get("type", "")
            msg_method = msg.get("method", msg.get("event", ""))
            msg_id = msg.get("id", "")

            # Direct response to our chat.send request
            if msg_type == "res" and msg_id == req_id:
                result = msg.get("result", {})
                if isinstance(result, dict):
                    text = result.get("text", result.get("content", result.get("message", "")))
                    if text:
                        reply_parts.append(str(text))
                        break
                continue

            # Chat-related events
            if msg_type == "event":
                data = msg.get("data", msg.get("payload", msg.get("params", {})))

                if "chat" in str(msg_method):
                    if isinstance(data, dict):
                        text = (
                            data.get("text", "")
                            or data.get("content", "")
                            or data.get("message", "")
                        )
                        role = data.get("role", data.get("from", ""))
                        if text and role != "user":
                            reply_parts.append(str(text))
                        if data.get("done") or data.get("finished") or data.get("complete"):
                            break
                    elif isinstance(data, str) and data:
                        reply_parts.append(data)

                if "agent" in str(msg_method) and any(
                    kw in str(msg_method) for kw in ("done", "stop", "complete")
                ):
                    if reply_parts:
                        break

                if "session" in str(msg_method) and "end" in str(msg_method):
                    break

        reply = "".join(reply_parts).strip()
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
    """OpenAI-compatible Chat Completions endpoint."""
    body = await request.json()
    messages = body.get("messages", [])

    user_msg = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            user_msg = msg.get("content", "")
            break

    if not user_msg:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "No user message found", "type": "invalid_request"}},
        )

    try:
        reply = await bridge.send_chat(user_msg)
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
                "prompt_tokens": len(user_msg),
                "completion_tokens": len(reply),
                "total_tokens": len(user_msg) + len(reply),
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
