"""Bounded screenshot conversion shared by the supported provider adapters."""

from __future__ import annotations

import base64
from pathlib import Path


def data_urls(paths):
    result = []
    for value in paths[-4:]:
        path = Path(value)
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Screenshot exceeds 8 MiB")
        data = path.read_bytes()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Screenshot must be a PNG")
        result.append("data:image/png;base64," + base64.b64encode(data).decode())
    return result


def openai_content(text, images):
    return (
        [
            {"type": "text", "text": text},
            *[{"type": "image_url", "image_url": {"url": url}} for url in images],
        ]
        if images
        else text
    )
