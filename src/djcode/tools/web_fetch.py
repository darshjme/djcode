"""Fetch content from URLs — DJcode web tool."""

from __future__ import annotations

import httpx

#: Memory bound, same role as ``bash.run_process``'s ``output_limit``: httpx has
#: already materialised the whole body by the time we see it, so this only stops
#: a pathological page from being carried further. It is NOT the output policy.
RESPONSE_LIMIT = 2_000_000


async def execute_web_fetch(url: str) -> str:
    """Fetch a URL and return its text content.

    W3-2 removed the ``max_chars`` parameter (default 10 000). It was the worst
    of the six ad-hoc truncations in the tool layer, because it was the SILENT
    one: the response text was sliced to that length with no marker of any kind,
    so a model reading a long page could not tell it had been handed a fragment.
    It was also a model-facing schema parameter, which made "discard the rest of
    this page irrecoverably" something the model could ask for by name. The
    chokepoint now bounds the result and writes the full page to a spill file
    whose path it links.

    Args:
        url: The URL to fetch.

    Returns:
        The text content of the response.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text[:RESPONSE_LIMIT]
    except httpx.ConnectError:
        return f"Error: Cannot connect to {url}"
    except httpx.HTTPStatusError as e:
        return f"Error: HTTP {e.response.status_code} fetching {url}"
    except httpx.ReadTimeout:
        return f"Error: Timeout fetching {url}"
    except Exception as e:
        return f"Error fetching {url}: {e}"
