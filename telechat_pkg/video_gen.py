"""
Video generation — generate short videos from text prompts via Replicate API.

Supports various video models via Replicate (e.g., Luma, Minimax, Kling).
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass

log = logging.getLogger(__name__)

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", "")
VIDEO_GEN_MODEL = os.getenv("VIDEO_GEN_MODEL", "luma/ray")
VIDEO_GEN_ENABLED = os.getenv("VIDEO_GEN_ENABLED", "false").lower() in ("1", "true", "yes")


@dataclass
class VideoResult:
    video_url: str
    video_path: str
    prompt: str
    error: str | None = None


def is_available() -> bool:
    return VIDEO_GEN_ENABLED and bool(REPLICATE_API_TOKEN)


async def generate(
    prompt: str,
    model: str = VIDEO_GEN_MODEL,
) -> VideoResult:
    """Generate a short video from a text prompt using Replicate."""
    if not REPLICATE_API_TOKEN:
        return VideoResult(video_url="", video_path="", prompt=prompt,
                           error="REPLICATE_API_TOKEN not set")

    try:
        import aiohttp
    except ImportError:
        return VideoResult(video_url="", video_path="", prompt=prompt,
                           error="aiohttp not installed")

    create_url = "https://api.replicate.com/v1/models/{}/predictions".format(model)
    headers = {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
        "Prefer": "wait",
    }
    payload = {
        "input": {
            "prompt": prompt,
        },
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(create_url, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=180)) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    return VideoResult(video_url="", video_path="", prompt=prompt,
                                       error=f"Replicate API error {resp.status}: {body[:200]}")
                data = await resp.json()

            # Poll for completion
            status = data.get("status", "")
            pred_url = data.get("urls", {}).get("get", "")
            poll_count = 0
            while status not in ("succeeded", "failed", "canceled") and pred_url and poll_count < 90:
                await asyncio.sleep(3)
                async with session.get(pred_url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as poll_resp:
                    data = await poll_resp.json()
                    status = data.get("status", "")
                poll_count += 1

            if status == "failed":
                error = data.get("error", "Generation failed")
                return VideoResult(video_url="", video_path="", prompt=prompt,
                                   error=str(error)[:200])

            if status != "succeeded":
                return VideoResult(video_url="", video_path="", prompt=prompt,
                                   error=f"Video generation timed out (status: {status})")

            output = data.get("output")
            video_url = output if isinstance(output, str) else (output[0] if isinstance(output, list) else "")
            if not video_url:
                return VideoResult(video_url="", video_path="", prompt=prompt,
                                   error="No video URL in output")

            # Download
            async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=60)) as dl_resp:
                if dl_resp.status != 200:
                    return VideoResult(video_url=video_url, video_path="", prompt=prompt,
                                       error=f"Download failed: HTTP {dl_resp.status}")
                video_data = await dl_resp.read()
                tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
                tmp.write(video_data)
                tmp.close()
                return VideoResult(
                    video_url=video_url, video_path=tmp.name, prompt=prompt,
                )

    except asyncio.TimeoutError:
        return VideoResult(video_url="", video_path="", prompt=prompt,
                           error="Video generation timed out")
    except Exception as e:
        return VideoResult(video_url="", video_path="", prompt=prompt,
                           error=str(e)[:200])
