"""Fetch content from URLs — DJcode web tool."""

from __future__ import annotations

import httpx


async def execute_web_fetch(url: str, max_chars: int = 10000) -> str:
    """Fetch a URL and return its text content.

    Args:
        url: The URL to fetch.
        max_chars: Maximum characters to return (default 10000).

    Returns:
        The text content of the response, truncated to max_chars.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text = resp.text[:max_chars]
            return text
    except httpx.ConnectError:
        return f"Error: Cannot connect to {url}"
    except httpx.HTTPStatusError as e:
        return f"Error: HTTP {e.response.status_code} fetching {url}"
    except httpx.ReadTimeout:
        return f"Error: Timeout fetching {url}"
    except Exception as e:
        return f"Error fetching {url}: {e}"
