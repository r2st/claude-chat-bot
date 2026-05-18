"""
Image generation — create images using OpenAI's DALL-E API.

Ported from openclaw's src/image-generation module.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import tempfile
from dataclasses import dataclass

log = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
IMAGE_MODEL = os.getenv("IMAGE_GEN_MODEL", "dall-e-3")
IMAGE_SIZE = os.getenv("IMAGE_GEN_SIZE", "1024x1024")
IMAGE_QUALITY = os.getenv("IMAGE_GEN_QUALITY", "standard")
IMAGE_GEN_ENABLED = os.getenv("IMAGE_GEN_ENABLED", "false").lower() in ("1", "true", "yes")

VALID_SIZES = {"256x256", "512x512", "1024x1024", "1024x1792", "1792x1024"}


@dataclass
class ImageResult:
    image_path: str
    prompt: str
    revised_prompt: str
    model: str
    size: str
    error: str | None = None


def is_available() -> bool:
    return IMAGE_GEN_ENABLED and bool(OPENAI_API_KEY)


async def generate(
    prompt: str,
    size: str = IMAGE_SIZE,
    quality: str = IMAGE_QUALITY,
    model: str = IMAGE_MODEL,
) -> ImageResult:
    if not OPENAI_API_KEY:
        return ImageResult(image_path="", prompt=prompt, revised_prompt="",
                           model=model, size=size, error="OPENAI_API_KEY not set")
    if size not in VALID_SIZES:
        size = IMAGE_SIZE

    try:
        import aiohttp
    except ImportError:
        return ImageResult(image_path="", prompt=prompt, revised_prompt="",
                           model=model, size=size, error="aiohttp not installed")

    url = "https://api.openai.com/v1/images/generations"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": size,
        "quality": quality,
        "response_format": "b64_json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return ImageResult(image_path="", prompt=prompt, revised_prompt="",
                                       model=model, size=size,
                                       error=f"OpenAI API error {resp.status}: {body[:200]}")
                data = await resp.json()
                img_data = data["data"][0]
                b64 = img_data.get("b64_json", "")
                revised = img_data.get("revised_prompt", prompt)
                if not b64:
                    return ImageResult(image_path="", prompt=prompt, revised_prompt=revised,
                                       model=model, size=size, error="Empty image data")
                image_bytes = base64.b64decode(b64)
                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                tmp.write(image_bytes)
                tmp.close()
                return ImageResult(
                    image_path=tmp.name, prompt=prompt, revised_prompt=revised,
                    model=model, size=size,
                )
    except asyncio.TimeoutError:
        return ImageResult(image_path="", prompt=prompt, revised_prompt="",
                           model=model, size=size, error="Image generation timed out")
    except Exception as e:
        return ImageResult(image_path="", prompt=prompt, revised_prompt="",
                           model=model, size=size, error=str(e)[:200])
