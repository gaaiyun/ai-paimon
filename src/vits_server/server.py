"""
Paimon VITS Text-to-Speech Server

Loads a pre-trained VITS checkpoint (``paimon.pth``) and exposes a
FastAPI endpoint compatible with Open-LLM-VTuber's ``x_tts`` backend.

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
import logging
import os
import sys

import soundfile as sf
import torch
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response

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

_DEFAULT_CONFIG = os.path.join(_THIS_DIR, "VITS", "configs", "biaobei_base.json")
CONFIG_PATH: str = os.getenv("VITS_CONFIG_PATH", _DEFAULT_CONFIG)
MODEL_PATH: str = os.getenv("VITS_MODEL_PATH", os.path.join(_THIS_DIR, "..", "..", "paimon.pth"))


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
# Model initialisation
# ---------------------------------------------------------------------------

logger.info("Loading VITS model…")
logger.info("  Config : %s", CONFIG_PATH)
logger.info("  Weights: %s", MODEL_PATH)

hps = utils.get_hparams_from_file(CONFIG_PATH)

device = "cuda" if torch.cuda.is_available() else "cpu"
logger.info("  Device : %s", device)

net_g = SynthesizerTrn(
    len(symbols),
    hps.data.filter_length // 2 + 1,
    hps.train.segment_size // hps.data.hop_length,
    **hps.model,
).to(device)
net_g.eval()

utils.load_checkpoint(MODEL_PATH, net_g, None)
logger.info("✅ VITS model loaded successfully!")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Paimon VITS TTS",
    description="VITS speech synthesis server for Paimon voice",
)


@app.post("/tts_to_audio")
async def tts_to_audio(request: Request) -> Response:
    """Synthesise speech from text (Open-LLM-VTuber ``x_tts`` compatible)."""
    data = await request.json()
    text: str = data.get("text", "")
    if not text:
        return Response(status_code=400, content="Empty text")

    logger.info("TTS request: %s", text)

    length_scale = float(data.get("length_scale", 1.0))
    stn_tst = _get_text(text, hps)

    with torch.no_grad():
        x_tst = stn_tst.to(device).unsqueeze(0)
        x_tst_lengths = torch.LongTensor([stn_tst.size(0)]).to(device)
        audio = (
            net_g.infer(
                x_tst,
                x_tst_lengths,
                noise_scale=0.667,
                noise_scale_w=0.8,
                length_scale=length_scale,
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
    return {"status": "ok", "device": device, "model": os.path.basename(MODEL_PATH)}


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Paimon VITS TTS server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=int(os.getenv("VITS_PORT", "8020")))
    args = parser.parse_args()

    logger.info("🎙️ Starting Paimon VITS server on http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
