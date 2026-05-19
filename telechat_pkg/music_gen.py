"""
Music generation — generate music/audio using Replicate API (MusicGen model).

Supports Meta's MusicGen via Replicate, or any compatible API.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass

log = logging.getLogger(__name__)

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", "")
MUSIC_GEN_MODEL = os.getenv("MUSIC_GEN_MODEL", "meta/musicgen:671ac645ce5e552cc63a54a2bbff63fcf798043055d2dac5fc9e36a837eedcfb")
MUSIC_GEN_ENABLED = os.getenv("MUSIC_GEN_ENABLED", "false").lower() in ("1", "true", "yes")
MUSIC_DURATION = int(os.getenv("MUSIC_GEN_DURATION", "10"))  # seconds


@dataclass
class MusicResult:
    audio_url: str
    audio_path: str
    prompt: str
    duration: int
    error: str | None = None


def is_available() -> bool:
    return MUSIC_GEN_ENABLED and bool(REPLICATE_API_TOKEN)


async def generate(
    prompt: str,
    duration: int = MUSIC_DURATION,
) -> MusicResult:
    """Generate music from a text prompt using Replicate's MusicGen."""
    if not REPLICATE_API_TOKEN:
        return MusicResult(audio_url="", audio_path="", prompt=prompt,
                           duration=duration, error="REPLICATE_API_TOKEN not set")

    try:
        import aiohttp
    except ImportError:
        return MusicResult(audio_url="", audio_path="", prompt=prompt,
                           duration=duration, error="aiohttp not installed")

    # Create prediction
    create_url = "https://api.replicate.com/v1/predictions"
    headers = {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
        "Prefer": "wait",
    }
    payload = {
        "version": MUSIC_GEN_MODEL.split(":")[-1] if ":" in MUSIC_GEN_MODEL else MUSIC_GEN_MODEL,
        "input": {
            "prompt": prompt,
            "duration": min(max(duration, 1), 30),
            "model_version": "stereo-melody-large",
            "output_format": "mp3",
        },
    }

    try:
        async with aiohttp.ClientSession() as session:
            # Start prediction (with Prefer: wait, it blocks until done)
            async with session.post(create_url, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    return MusicResult(audio_url="", audio_path="", prompt=prompt,
                                       duration=duration,
                                       error=f"Replicate API error {resp.status}: {body[:200]}")
                data = await resp.json()

            # If prediction isn't complete yet, poll for it
            status = data.get("status", "")
            pred_url = data.get("urls", {}).get("get", "")
            poll_count = 0
            while status not in ("succeeded", "failed", "canceled") and pred_url and poll_count < 60:
                await asyncio.sleep(2)
                async with session.get(pred_url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as poll_resp:
                    data = await poll_resp.json()
                    status = data.get("status", "")
                poll_count += 1

            if status == "failed":
                error = data.get("error", "Generation failed")
                return MusicResult(audio_url="", audio_path="", prompt=prompt,
                                   duration=duration, error=str(error)[:200])

            if status != "succeeded":
                return MusicResult(audio_url="", audio_path="", prompt=prompt,
                                   duration=duration, error=f"Prediction timed out (status: {status})")

            output = data.get("output")
            if not output:
                return MusicResult(audio_url="", audio_path="", prompt=prompt,
                                   duration=duration, error="No output from model")

            audio_url = output if isinstance(output, str) else output[0] if isinstance(output, list) else ""
            if not audio_url:
                return MusicResult(audio_url="", audio_path="", prompt=prompt,
                                   duration=duration, error="No audio URL in output")

            # Download the audio file
            async with session.get(audio_url, timeout=aiohttp.ClientTimeout(total=30)) as dl_resp:
                if dl_resp.status != 200:
                    return MusicResult(audio_url=audio_url, audio_path="", prompt=prompt,
                                       duration=duration, error=f"Failed to download audio: HTTP {dl_resp.status}")
                audio_data = await dl_resp.read()
                tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
                tmp.write(audio_data)
                tmp.close()
                return MusicResult(
                    audio_url=audio_url, audio_path=tmp.name, prompt=prompt,
                    duration=duration,
                )

    except asyncio.TimeoutError:
        return MusicResult(audio_url="", audio_path="", prompt=prompt,
                           duration=duration, error="Music generation timed out")
    except Exception as e:
        return MusicResult(audio_url="", audio_path="", prompt=prompt,
                           duration=duration, error=str(e)[:200])
