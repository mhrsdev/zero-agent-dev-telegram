"""Real keyless web search tool (round 5 — Hermes parity gap).

``WebSearchCfg`` in the management config was a stub: the wizard, the
doctor, and ``zero websearch status`` all referenced a capability that
no runtime tool ever implemented. This module makes it real:

- Keyless DuckDuckGo Lite endpoint (``lite.duckduckgo.com/lite``) over
  the standard httpx client — no API key, no new dependency.
- Bounded fetch (10 s), bounded parse (top 5 results), bounded output.
- Registered as an INLINE tool handler: it runs in-process under the
  normal grant/redaction/audit pipeline (``ToolService.invoke``), so a
  project must grant ``web_search`` to a scope before any model can
  call it — same capability contract as every other tool.
- Reachability was verified live from this environment (22 KB result
  page); network failures return a structured error payload to the
  model instead of crashing the loop.

Parsing note: DDG Lite renders results as a table of ``result-link``
anchors with sibling ``result-snippet`` cells. The regexes are anchored
to those exact class names; a layout change yields zero results (an
honest empty page) rather than garbage.
"""

from __future__ import annotations

import html
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

WEB_SEARCH_ENDPOINT = "https://lite.duckduckgo.com/lite/"
_WEB_SEARCH_TIMEOUT_SECONDS = 10.0
_MAX_RESULTS = 5
_MAX_QUERY_CHARS = 400
_MAX_SNIPPET_CHARS = 400

_LINK_A_RE = re.compile(
    r"<a[^>]*href=['\"]([^'\"]+)['\"][^>]*class=['\"]result-link['\"][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_LINK_B_RE = re.compile(
    r"<a[^>]*class=['\"]result-link['\"][^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_SNIPPET_RE = re.compile(
    r"<td[^>]*class=['\"]result-snippet['\"][^>]*>(.*?)</td>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_REDIR_RE = re.compile(r"[?&]uddg=([^&']+)")


def _unwrap_url(raw: str) -> str:
    """Resolve a DDG redirect wrapper (``//duckduckgo.com/l/?uddg=...``)
    to the real target URL; pass direct links through untouched."""
    value = str(raw).strip()
    if "duckduckgo.com/l/" in value or "uddg=" in value:
        match = _REDIR_RE.search(value)
        if match:
            from urllib.parse import unquote, urlparse

            candidate = unquote(match.group(1))
            parsed = urlparse(candidate)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                return candidate
    return value


def _strip_tags(fragment: str) -> str:
    text = _TAG_RE.sub("", fragment)
    text = html.unescape(text)
    return " ".join(text.split())


def parse_ddg_lite(html_text: str) -> list[dict[str, str]]:
    """Extract bounded results from a DuckDuckGo Lite HTML page."""
    links = _LINK_A_RE.findall(html_text) or _LINK_B_RE.findall(html_text)
    snippets = _SNIPPET_RE.findall(html_text)
    results: list[dict[str, str]] = []
    for index, (url, title) in enumerate(links[:_MAX_RESULTS]):
        clean_url = _unwrap_url(html.unescape(str(url)))
        if not clean_url.startswith(("http://", "https://")):
            continue
        snippet = _strip_tags(snippets[index])[:_MAX_SNIPPET_CHARS] if index < len(snippets) else ""
        results.append(
            {
                "title": _strip_tags(title)[:200],
                "url": clean_url,
                "snippet": snippet,
            }
        )
    return results


WEB_SEARCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_QUERY_CHARS,
            "description": "The web search query.",
        }
    },
    "required": ["query"],
    "additionalProperties": False,
}

WEB_SEARCH_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "snippet": {"type": "string"},
                },
                "required": ["title", "url", "snippet"],
                "additionalProperties": False,
            },
        },
        "error": {"type": "string"},
    },
    "required": ["query", "results"],
    "additionalProperties": False,
}


def make_web_search_handler(
    *,
    transport: Any = None,
    endpoint: str = WEB_SEARCH_ENDPOINT,
    timeout_seconds: float = _WEB_SEARCH_TIMEOUT_SECONDS,
    fetcher=None,
    fetch_attempts: int = 2,
):
    """Build the inline ``web_search`` handler.

    ``fetcher`` is a test seam: ``fetcher(query) -> html_text``. In
    production it performs the real HTTP GET through httpx.

    Resilience (2026-08-31, live run): the DDG Lite backend proved
    INTERMITTENTLY unreachable from a filtered network — one-shot
    fetches flapped between real results and ConnectTimeout errors
    within minutes. The fetch now retries transient failures
    (connect/timeout/reset) with a short pause before reporting the
    structured unreachable error to the model.
    """

    def _fetch(query: str) -> str:
        if fetcher is not None:
            return fetcher(query)
        import httpx

        response = httpx.get(
            endpoint,
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (compatible; zero-agent/1.0)"},
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.text

    def _fetch_with_retry(query: str) -> str:
        attempts = max(1, int(fetch_attempts))
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return _fetch(query)
            except Exception as exc:  # noqa: BLE001 - retryable fetch errors
                transient = type(exc).__name__ in {
                    "ConnectTimeout",
                    "ReadTimeout",
                    "ConnectError",
                    "ReadError",
                    "RemoteProtocolError",
                    "PoolingTimeout",
                }
                last_exc = exc
                if not transient or attempt == attempts - 1:
                    raise
                time.sleep(1.0 * (attempt + 1))
        raise last_exc  # pragma: no cover - defensive

    def handler(input_data: dict[str, Any], context: Any) -> dict[str, Any]:
        query = str((input_data or {}).get("query", "")).strip()
        if not query:
            return {"query": "", "results": [], "error": "empty query"}
        query = query[:_MAX_QUERY_CHARS]
        try:
            page = _fetch_with_retry(query)
        except Exception as exc:  # noqa: BLE001 - network failure is a result
            logger.info("web_search fetch failed: %s", type(exc).__name__)
            return {
                "query": query,
                "results": [],
                "error": f"search backend unreachable ({type(exc).__name__})",
            }
        results = parse_ddg_lite(page)
        if not results:
            return {
                "query": query,
                "results": [],
                "error": "no results parsed (backend layout may have changed)",
            }
        return {"query": query, "results": results}

    return handler


__all__ = [
    "WEB_SEARCH_ENDPOINT",
    "WEB_SEARCH_INPUT_SCHEMA",
    "WEB_SEARCH_OUTPUT_SCHEMA",
    "make_web_search_handler",
    "parse_ddg_lite",
]
