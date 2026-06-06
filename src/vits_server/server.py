"""
Paimon VITS Text-to-Speech Server

Loads a pre-trained VITS checkpoint (``paimon.pth``) and exposes a
FastAPI endpoint compatible with Open-LLM-VTuber's ``x_tts`` backend.

The 417 MB model is loaded inside the FastAPI ``lifespan`` (not at import
time) so the module can be imported — and unit-tested — without the weights
present.

Configuration
-------------
Set ``VITS_MODEL_PATH`` and ``VITS_CONFIG_PATH`` environment variables,
or place ``.env`` in the project root.

Usage::

    python src/vits_server/server.py                 # port 8020
    python src/vits_server/server.py --port 9000
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
from contextlib import asynccontextmanager

import soundfile as sf
import torch
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

# ---------------------------------------------------------------------------
# Logging — ensure UTF-8 on Windows console
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("vits_server")

# ---------------------------------------------------------------------------
# Resolve paths — make VITS importable regardless of working directory
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import VITS.commons as commons  # noqa: E402
import VITS.utils as utils  # noqa: E402
from VITS.models import SynthesizerTrn  # noqa: E402
from VITS.text import text_to_sequence  # noqa: E402
from VITS.text.symbols import symbols  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

_PAIMON_CONFIG = os.path.join(_THIS_DIR, "..", "..", "models", "vits", "paimon", "paimon6k.json")
_FALLBACK_CONFIG = os.path.join(_THIS_DIR, "VITS", "configs", "biaobei_base.json")
_DEFAULT_CONFIG = _PAIMON_CONFIG if os.path.isfile(_PAIMON_CONFIG) else _FALLBACK_CONFIG
CONFIG_PATH: str = os.getenv("VITS_CONFIG_PATH", _DEFAULT_CONFIG)
_PAIMON_MODEL = os.path.join(_THIS_DIR, "..", "..", "models", "vits", "paimon", "paimon6k_390000.pth")
_FALLBACK_MODEL = os.path.join(_THIS_DIR, "..", "..", "paimon.pth")
_DEFAULT_MODEL = _PAIMON_MODEL if os.path.isfile(_PAIMON_MODEL) else _FALLBACK_MODEL
MODEL_PATH: str = os.getenv("VITS_MODEL_PATH", _DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# Request model (reject malformed input)
# ---------------------------------------------------------------------------

class TTSRequest(BaseModel):
    """Open-LLM-VTuber ``x_tts`` synthesis request."""

    text: str = Field(..., min_length=1)
    length_scale: float = Field(default=1.0, gt=0)

    model_config = {"extra": "ignore"}


# ---------------------------------------------------------------------------
# Detect symbols from config file (if present)
# ---------------------------------------------------------------------------
def _load_config_symbols(config_path: str) -> list[str] | None:
    """Read symbols list from a VITS JSON config, if the 'symbols' key exists."""
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        if "symbols" in cfg:
            return cfg["symbols"]
    except Exception:
        pass
    return None


def _resolve_symbol_count(config_path: str) -> int:
    """Resolve the symbol vocabulary size, honouring a config override.

    If the config carries its own ``symbols`` list, monkey-patch the text
    module's mappings so ``text_to_sequence`` uses the matching vocabulary.
    Returns the symbol count the model must be built with.
    """
    config_symbols = _load_config_symbols(config_path)
    if config_symbols is not None:
        import VITS.text as _text_mod
        _text_mod.symbols = config_symbols
        _text_mod._symbol_to_id = {s: i for i, s in enumerate(config_symbols)}
        _text_mod._id_to_symbol = {i: s for i, s in enumerate(config_symbols)}
        logger.info("  Symbols: %d (from config)", len(config_symbols))
        return len(config_symbols)
    logger.info("  Symbols: %d (from symbols.py)", len(symbols))
    return len(symbols)


# ---------------------------------------------------------------------------
# Text helper
# ---------------------------------------------------------------------------

def _get_text(text: str, hps) -> torch.LongTensor:
    """Convert text to a normalised integer sequence for VITS."""
    text_norm = text_to_sequence(text, hps.data.text_cleaners)
    if hps.data.add_blank:
        text_norm = commons.intersperse(text_norm, 0)
    return torch.LongTensor(text_norm)


# ---------------------------------------------------------------------------
# Model initialisation (called from lifespan, NOT at import time)
# ---------------------------------------------------------------------------

def load_model(config_path: str, model_path: str) -> dict:
    """Load hparams + VITS checkpoint. Returns a state dict for the app.

    Heavy work (parsing config, building the network, loading the ~417 MB
    checkpoint) lives here so importing this module stays cheap and testable.
    """
    logger.info("Loading VITS model…")
    logger.info("  Config : %s", config_path)
    logger.info("  Weights: %s", model_path)

    n_symbols = _resolve_symbol_count(config_path)
    hps = utils.get_hparams_from_file(config_path)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info("  Device : %s", device)

    net_g = SynthesizerTrn(
        n_symbols,
        hps.data.filter_length // 2 + 1,
        hps.train.segment_size // hps.data.hop_length,
        **hps.model,
    ).to(device)
    net_g.eval()

    utils.load_checkpoint(model_path, net_g, None)
    logger.info("[OK] VITS model loaded successfully!")
    return {"hps": hps, "net_g": net_g, "device": device}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the model on startup and stash it on ``app.state``."""
    state = load_model(CONFIG_PATH, MODEL_PATH)
    app.state.hps = state["hps"]
    app.state.net_g = state["net_g"]
    app.state.device = state["device"]
    yield


app = FastAPI(
    title="Paimon VITS TTS",
    description="VITS speech synthesis server for Paimon voice",
    lifespan=lifespan,
)


@app.post("/tts_to_audio")
async def tts_to_audio(payload: dict) -> Response:
    """Synthesise speech from text (Open-LLM-VTuber ``x_tts`` compatible)."""
    try:
        req = TTSRequest.model_validate(payload)
    except ValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"error": {"message": exc.errors(), "type": "invalid_request"}},
        )

    logger.info("TTS request: %s", req.text)

    hps = app.state.hps
    net_g = app.state.net_g
    device = app.state.device

    stn_tst = _get_text(req.text, hps)

    with torch.no_grad():
        x_tst = stn_tst.to(device).unsqueeze(0)
        x_tst_lengths = torch.LongTensor([stn_tst.size(0)]).to(device)
        audio = (
            net_g.infer(
                x_tst,
                x_tst_lengths,
                noise_scale=0.667,
                noise_scale_w=0.8,
                length_scale=req.length_scale,
            )[0][0, 0]
            .data.cpu()
            .float()
            .numpy()
        )

    buf = io.BytesIO()
    sf.write(buf, audio, samplerate=hps.data.sampling_rate, format="WAV")

    return Response(content=buf.getvalue(), media_type="audio/wav")


@app.get("/health")
async def health() -> dict:
    """Health check."""
    loaded = getattr(app.state, "net_g", None) is not None
    device = getattr(app.state, "device", "unknown")
    return {"status": "ok", "device": device, "model": os.path.basename(MODEL_PATH), "loaded": loaded}


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Paimon VITS TTS server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=int(os.getenv("VITS_PORT", "8020")))
    args = parser.parse_args()

    logger.info("Starting Paimon VITS server on http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
