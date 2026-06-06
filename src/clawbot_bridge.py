"""
ClawBot (OpenClaw) WebSocket → OpenAI-Compatible API Bridge

Bridges the OpenClaw Gateway's WebSocket protocol v3 (including Ed25519
device authentication) to a standard OpenAI ``/v1/chat/completions``
HTTP endpoint so that any OpenAI-compatible client can talk to ClawBot.

This bridge is the canonical OpenClaw → OpenAI entry point: Open-LLM-VTuber
points its ``base_url`` at this service (default ``http://127.0.0.1:5001/v1``),
and the bridge owns the single WebSocket connection to the Gateway.

Design
------
* **One connection, one receive loop.** A single background task reads every
  frame from the Gateway and dispatches it: responses go to the pending
  request that owns the matching ``id``; session events go to per-session
  queues. No two coroutines ever call ``recv()`` concurrently, so frames are
  never lost or stolen between requests.
* **Sessions are reused.** A conversation maps to a single OpenClaw session
  (cached by ``conversation_id``), so the agent keeps its shared memory across
  turns instead of getting a fresh session every message.
* **Per-session locking.** Requests on the *same* conversation serialise (they
  share message ordering); requests on *different* conversations run
  concurrently.

Configuration
-------------
All secrets are loaded from environment variables.  Copy ``.env.example``
to ``.env`` and fill in your values, or export them in your shell.

Usage::

    pip install fastapi uvicorn websockets cryptography python-dotenv pydantic
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
import sys as _sys
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
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
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
# Pydantic request models (reject malformed input)
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    """A single OpenAI chat message."""

    role: str
    content: Any = ""

    @field_validator("content")
    @classmethod
    def _stringify_content(cls, v: Any) -> str:
        """Normalise content into a plain string.

        OpenAI clients may send a string or a list of content parts
        (``[{"type": "text", "text": "..."}]``); accept both.
        """
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            parts: list[str] = []
            for item in v:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif isinstance(item, str):
                    parts.append(item)
            return "".join(parts)
        if v is None:
            return ""
        return str(v)


class ChatCompletionRequest(BaseModel):
    """Subset of the OpenAI Chat Completions request we accept."""

    messages: list[ChatMessage] = Field(..., min_length=1)
    model: str = "clawbot"
    # ``user`` (OpenAI standard) doubles as our conversation/session key so
    # one conversation reuses one OpenClaw session and keeps shared memory.
    user: str | None = None
    stream: bool = False
    temperature: float | None = None

    model_config = {"extra": "ignore"}


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
    """Persistent WebSocket connection to the OpenClaw Gateway.

    A single background task (:meth:`_receive_loop`) owns the socket reads and
    fans frames out to per-request futures and per-session event queues, so
    concurrent HTTP requests never compete for ``recv()``.
    """

    def __init__(self) -> None:
        self.ws: websockets.WebSocketClientProtocol | None = None
        self._connected: bool = False
        # Serialises connect/reconnect only — NOT individual chat requests.
        self._conn_lock = asyncio.Lock()
        self._recv_task: asyncio.Task | None = None

        # req_id -> Future resolved with the matching ``res`` frame.
        self._pending: dict[str, asyncio.Future] = {}
        # session_key -> queue of event frames belonging to that session.
        self._session_events: dict[str, asyncio.Queue] = {}

        # conversation_id -> session_key  (enables session reuse / memory).
        self._sessions: dict[str, str] = {}
        # conversation_id -> lock  (serialise turns within one conversation).
        self._session_locks: dict[str, asyncio.Lock] = {}

    # -- connection / auth --------------------------------------------------

    def _is_open(self) -> bool:
        return bool(self._connected and self.ws and not getattr(self.ws, "closed", False))

    async def connect(self) -> None:
        """Connect (if not already) and authenticate with the gateway."""
        async with self._conn_lock:
            if self._is_open():
                return
            await self._do_connect()

    async def _do_connect(self) -> None:
        """Perform the full connect → challenge → connect handshake.

        Caller must hold ``self._conn_lock``.
        """
        # Tear down any previous receive loop / cached session state — a new
        # socket means old session keys are no longer valid.
        await self._teardown_locked()

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

        # Step 4: read responses until we get hello-ok or error.  This runs
        # before the background receive loop starts, so reading here is safe.
        for _ in range(10):
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=10)
            except asyncio.TimeoutError as exc:
                raise ConnectionError("Timeout waiting for connect response") from exc

            resp: dict[str, Any] = json.loads(raw)

            if resp.get("type") == "res":
                if resp.get("ok"):
                    payload = resp.get("payload", {})
                    if isinstance(payload, dict) and payload.get("type") == "hello-ok":
                        granted = payload.get("auth", {}).get("scopes", [])
                        logger.info(
                            "[OK] Authentication successful. Granted scopes: %s",
                            granted,
                        )
                        self._connected = True
                        # Start the single background receive loop.
                        self._recv_task = asyncio.create_task(self._receive_loop())
                        return
                    # Some other ok response — keep reading
                    logger.info(
                        "  Got ok response: %s",
                        json.dumps(resp, ensure_ascii=False)[:300],
                    )
                else:
                    err = resp.get("error", {})
                    raise ConnectionError(f"Authentication failed: {err}")
            elif resp.get("type") == "event":
                # Drain events (health, etc.) while waiting for hello-ok
                logger.debug("  Draining event: %s", resp.get("event", ""))

        raise ConnectionError("Never received hello-ok")

    # -- receive loop -------------------------------------------------------

    async def _receive_loop(self) -> None:
        """Single owner of ``ws.recv()``; fan frames out to waiters.

        Responses (``type == "res"``) resolve the future registered under
        their ``id``.  Session events are routed to the queue for their
        session key.  This is the only place that reads the socket once the
        connection is live, which is what makes concurrent requests safe.
        """
        ws = self.ws
        assert ws is not None
        try:
            async for raw in ws:
                try:
                    msg: dict[str, Any] = json.loads(raw)
                except (ValueError, TypeError):
                    continue

                msg_type = msg.get("type", "")
                msg_id = msg.get("id", "")

                if msg_type == "res" and msg_id in self._pending:
                    fut = self._pending.get(msg_id)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                    continue

                if msg_type == "event":
                    key = self._event_session_key(msg)
                    if key and key in self._session_events:
                        self._session_events[key].put_nowait(msg)
                    else:
                        # Unkeyed events get broadcast to all active sessions so
                        # an in-flight request still observes its reply even if
                        # the Gateway omits the session key on the frame.
                        for q in self._session_events.values():
                            q.put_nowait(msg)
        except websockets.ConnectionClosed:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Receive loop terminated: %s", exc)
        finally:
            self._connected = False
            # Fail any in-flight requests so callers don't hang.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("WebSocket closed"))

    @staticmethod
    def _event_session_key(msg: dict[str, Any]) -> str:
        """Best-effort extraction of the session key an event belongs to."""
        payload = msg.get("payload", msg.get("data", {}))
        if isinstance(payload, dict):
            for field in ("key", "session", "sessionKey"):
                val = payload.get(field)
                if isinstance(val, str) and val:
                    return val
        for field in ("key", "session", "sessionKey"):
            val = msg.get(field)
            if isinstance(val, str) and val:
                return val
        return ""

    # -- request plumbing ---------------------------------------------------

    async def _request(self, method: str, params: dict[str, Any], timeout: float = 10) -> dict[str, Any]:
        """Send a req frame and await its matching res frame."""
        assert self.ws is not None
        req_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        try:
            await self.ws.send(json.dumps({
                "type": "req",
                "id": req_id,
                "method": method,
                "params": params,
            }))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

    # -- session management -------------------------------------------------

    async def _get_session(self, conversation_id: str) -> str:
        """Return the session key for a conversation, creating it on demand.

        Sessions are cached so each conversation reuses one OpenClaw session
        (preserving the agent's shared memory across turns).
        """
        cached = self._sessions.get(conversation_id)
        if cached and cached in self._session_events:
            return cached

        res = await self._request("sessions.create", {})
        if not res.get("ok"):
            raise ConnectionError(f"Failed to create session: {res.get('error')}")
        session_key = res.get("payload", {}).get("key", "")
        if not session_key:
            raise ConnectionError("sessions.create returned no key")

        # Register the event queue BEFORE subscribing so no early event is lost.
        self._session_events.setdefault(session_key, asyncio.Queue())

        sub = await self._request("sessions.subscribe", {"key": session_key})
        if sub.get("ok") is False:
            raise ConnectionError(f"sessions.subscribe failed: {sub.get('error')}")

        self._sessions[conversation_id] = session_key
        logger.info("  Session ready for %s: %s", conversation_id, session_key)
        return session_key

    def _conv_lock(self, conversation_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[conversation_id] = lock
        return lock

    # -- chat ---------------------------------------------------------------

    async def send_chat(
        self,
        message: str,
        conversation_id: str = "default",
        timeout: float = 120,
    ) -> str:
        """Send a chat message on a conversation and collect the reply.

        Turns within the same ``conversation_id`` serialise (they share an
        OpenClaw session and must keep message ordering); different
        conversations proceed concurrently.
        """
        if not self._is_open():
            await self.connect()

        async with self._conv_lock(conversation_id):
            session_key = await self._get_session(conversation_id)
            queue = self._session_events[session_key]
            # Drain any stale events left from a previous turn.
            while not queue.empty():
                queue.get_nowait()

            send_res = await self._request(
                "sessions.send",
                {"key": session_key, "message": message},
                timeout=min(timeout, 30),
            )
            if send_res.get("ok") is False:
                err = send_res.get("error", {})
                err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                raise RuntimeError(f"sessions.send failed: {err_msg}")
            logger.info("[send] session=%s", session_key[:30])

            reply = await self._collect_reply(queue, timeout=timeout)
            logger.info("[recv] reply: %s…", reply[:80])
            return reply

    async def _collect_reply(self, queue: asyncio.Queue, timeout: float) -> str:
        """Collect assistant text from a session's event stream."""
        assistant_texts: list[str] = []
        deadline = time.time() + timeout

        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=min(remaining, 30))
            except asyncio.TimeoutError:
                if assistant_texts:
                    break
                continue

            if msg.get("type") != "event":
                continue
            event = msg.get("event", "")
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

        reply = assistant_texts[-1].strip() if assistant_texts else ""
        if not reply:
            reply = "（ClawBot did not return a valid reply — check Gateway status and Agent config）"
        return reply

    # -- lifecycle ----------------------------------------------------------

    async def _teardown_locked(self) -> None:
        """Reset connection state. Caller must hold ``self._conn_lock``."""
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
        self._recv_task = None
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = None
        self._connected = False
        self._pending.clear()
        self._session_events.clear()
        self._sessions.clear()

    async def close(self) -> None:
        """Gracefully close the WebSocket connection."""
        async with self._conn_lock:
            await self._teardown_locked()


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
async def chat_completions(payload: dict) -> JSONResponse:
    """OpenAI-compatible Chat Completions endpoint.

    Extracts conversation history and the latest user message, formats them
    into a single structured message for OpenClaw's ``sessions.send``.

    System prompt is intentionally NOT injected here — OpenClaw manages
    persona through its workspace files (IDENTITY.md / SOUL.md), and the
    VTuber persona_prompt is already conveyed via conversation context.
    """
    # Validate the request body (reject malformed input).
    try:
        req = ChatCompletionRequest.model_validate(payload)
    except ValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"error": {"message": exc.errors(), "type": "invalid_request"}},
        )

    conversation_history = [
        msg for msg in req.messages if msg.role in ("user", "assistant")
    ]

    # Truncate to last 10 messages to avoid token overflow
    if len(conversation_history) > 10:
        conversation_history = conversation_history[-10:]

    user_msg = ""
    for msg in reversed(conversation_history):
        if msg.role == "user":
            user_msg = msg.content
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
            role_label = "用户" if msg.role == "user" else "派蒙"
            full_message += f"{role_label}：{msg.content}\n"
        full_message += "\n"
    full_message += f"[当前输入]\n用户：{user_msg}"

    # ``user`` doubles as the conversation key so a conversation reuses one
    # OpenClaw session (shared memory). Fall back to a single shared session.
    conversation_id = req.user or "default"

    try:
        reply = await bridge.send_chat(full_message, conversation_id=conversation_id)
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

    logger.info("ClawBot Bridge starting…")
    logger.info("   OpenClaw: %s", OPENCLAW_WS_URL)
    logger.info("   API: http://%s:%d/v1/chat/completions", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
