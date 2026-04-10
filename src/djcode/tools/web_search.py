"""Web search tool — search the internet via DuckDuckGo or Brave Search.

DuckDuckGo HTML scraping requires no API key.
Brave Search (optional) uses BRAVE_API_KEY env var for higher quality results.
"""

from __future__ import annotations

import html
import os
import re
from urllib.parse import quote_plus

import httpx

# Brave Search endpoint
BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"

# DuckDuckGo HTML search endpoint
DDG_HTML_URL = "https://html.duckduckgo.com/html/"

# Common browser-like headers to avoid bot detection
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


async def execute_web_search(query: str, num_results: int = 5) -> str:
    """Search the web and return formatted results.

    Tries Brave Search first (if BRAVE_API_KEY is set), then falls back
    to DuckDuckGo HTML scraping.

    Args:
        query: The search query string.
        num_results: Number of results to return (1-20, default 5).

    Returns:
        Formatted search results with title, URL, and snippet for each result.
    """
    if not query or not query.strip():
        return "Error: Empty search query"

    num_results = max(1, min(20, num_results))

    # Try Brave Search first if API key is available
    brave_key = os.environ.get("BRAVE_API_KEY", "").strip()
    if brave_key:
        result = await _search_brave(query, num_results, brave_key)
        if not result.startswith("Error:"):
            return result

    # Fall back to DuckDuckGo
    return await _search_duckduckgo(query, num_results)


async def _search_brave(query: str, num_results: int, api_key: str) -> str:
    """Search via Brave Search API."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                BRAVE_API_URL,
                params={"q": query, "count": num_results},
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": api_key,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        web_results = data.get("web", {}).get("results", [])
        if not web_results:
            return "Error: No results from Brave Search"

        return _format_results(
            [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": _clean_html(r.get("description", "")),
                }
                for r in web_results[:num_results]
            ],
            source="Brave Search",
            query=query,
        )

    except httpx.HTTPStatusError as e:
        return f"Error: Brave Search API returned {e.response.status_code}"
    except httpx.ConnectError:
        return "Error: Cannot connect to Brave Search API"
    except httpx.ReadTimeout:
        return "Error: Brave Search request timed out"
    except Exception as e:
        return f"Error: Brave Search failed: {e}"


async def _search_duckduckgo(query: str, num_results: int) -> str:
    """Search via DuckDuckGo HTML scraping (no API key needed)."""
    try:
        async with httpx.AsyncClient(
            timeout=15.0, follow_redirects=True
        ) as client:
            resp = await client.post(
                DDG_HTML_URL,
                data={"q": query, "b": ""},
                headers=_HEADERS,
            )
            resp.raise_for_status()
            body = resp.text

        results = _parse_ddg_html(body, num_results)

        if not results:
            # Fallback: try DuckDuckGo Lite
            results = await _search_ddg_lite(query, num_results)

        if not results:
            return f"No results found for: {query}"

        return _format_results(results, source="DuckDuckGo", query=query)

    except httpx.ConnectError:
        return "Error: Cannot connect to DuckDuckGo"
    except httpx.ReadTimeout:
        return "Error: DuckDuckGo request timed out"
    except Exception as e:
        return f"Error: DuckDuckGo search failed: {e}"


async def _search_ddg_lite(query: str, num_results: int) -> list[dict[str, str]]:
    """Fallback: DuckDuckGo Lite endpoint."""
    try:
        async with httpx.AsyncClient(
            timeout=15.0, follow_redirects=True
        ) as client:
            resp = await client.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": query},
                headers=_HEADERS,
            )
            resp.raise_for_status()
            body = resp.text

        return _parse_ddg_lite_html(body, num_results)
    except Exception:
        return []


def _parse_ddg_html(body: str, num_results: int) -> list[dict[str, str]]:
    """Parse DuckDuckGo HTML search results page.

    The HTML structure contains result blocks with class 'result__a' for links
    and 'result__snippet' for descriptions.
    """
    results: list[dict[str, str]] = []

    # Extract result links: <a class="result__a" href="...">title</a>
    link_pattern = re.compile(
        r'<a\s+[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    # Extract snippets: <a class="result__snippet" ...>snippet</a>
    snippet_pattern = re.compile(
        r'<a\s+[^>]*class="result__snippet"[^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )

    links = link_pattern.findall(body)
    snippets = snippet_pattern.findall(body)

    for i, (url, title) in enumerate(links):
        if len(results) >= num_results:
            break

        # DDG wraps URLs through their redirect — extract actual URL
        actual_url = _extract_ddg_url(url)
        if not actual_url or actual_url.startswith("https://duckduckgo.com"):
            continue

        snippet = _clean_html(snippets[i]) if i < len(snippets) else ""
        clean_title = _clean_html(title)

        if clean_title and actual_url:
            results.append({
                "title": clean_title,
                "url": actual_url,
                "snippet": snippet,
            })

    return results


def _parse_ddg_lite_html(body: str, num_results: int) -> list[dict[str, str]]:
    """Parse DuckDuckGo Lite results page."""
    results: list[dict[str, str]] = []

    # Lite version has simpler HTML: links in <a class="result-link"> or just plain <a> tags
    # within result rows
    link_pattern = re.compile(
        r'<a\s+[^>]*rel="nofollow"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    # Snippet text follows in <td> tags with class "result-snippet"
    snippet_pattern = re.compile(
        r'<td\s+[^>]*class="result-snippet"[^>]*>(.*?)</td>',
        re.DOTALL | re.IGNORECASE,
    )

    links = link_pattern.findall(body)
    snippets = snippet_pattern.findall(body)

    for i, (url, title) in enumerate(links):
        if len(results) >= num_results:
            break

        actual_url = _extract_ddg_url(url)
        if not actual_url or "duckduckgo.com" in actual_url:
            continue

        snippet = _clean_html(snippets[i]) if i < len(snippets) else ""
        clean_title = _clean_html(title)

        if clean_title and actual_url:
            results.append({
                "title": clean_title,
                "url": actual_url,
                "snippet": snippet,
            })

    return results


def _extract_ddg_url(raw_url: str) -> str:
    """Extract actual URL from DuckDuckGo redirect wrapper.

    DDG wraps URLs like: //duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com&...
    """
    if "uddg=" in raw_url:
        match = re.search(r"uddg=([^&]+)", raw_url)
        if match:
            from urllib.parse import unquote
            return unquote(match.group(1))

    # Direct URL (no redirect wrapper)
    if raw_url.startswith("http"):
        return raw_url
    if raw_url.startswith("//"):
        return "https:" + raw_url

    return raw_url


def _clean_html(text: str) -> str:
    """Strip HTML tags and decode entities."""
    # Remove tags
    clean = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities
    clean = html.unescape(clean)
    # Normalize whitespace
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def _format_results(
    results: list[dict[str, str]],
    source: str,
    query: str,
) -> str:
    """Format search results into a readable string."""
    if not results:
        return f"No results found for: {query}"

    lines = [f"Search results for '{query}' ({source}):", ""]

    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        snippet = r.get("snippet", "")

        lines.append(f"{i}. {title}")
        lines.append(f"   URL: {url}")
        if snippet:
            # Wrap snippet to reasonable width
            if len(snippet) > 300:
                snippet = snippet[:297] + "..."
            lines.append(f"   {snippet}")
        lines.append("")

    lines.append(f"({len(results)} results)")
    return "\n".join(lines)
