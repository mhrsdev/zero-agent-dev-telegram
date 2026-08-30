"""Telegram outbound rendering: markdown → HTML and UTF-16-safe chunking.

Hermes-agent parity (audit 2026-08-29, round 5):

Hermes converts agent markdown to MarkdownV2 with placeholder stashing,
measures message length in UTF-16 code units, and chunks long replies
while preserving code fences. Zero historically HTML-escaped the raw
markdown and hard-truncated at 4096 characters — model output showed
literal ``**bold**`` markers and long execution summaries were silently
cut in half (data loss on the primary delivery channel).

This module is escape-first: everything passes ``html.escape`` BEFORE any
markup is introduced, so provider/model text can never inject Telegram
HTML entities. Only the recognized markdown subset produces real tags;
unmatched markers stay literal (deterministic, never lossy).

Security notes:
- The renderer never interpolates exception text or callback authority.
- Link URLs are restricted to http/https/telegram schemes; anything else
  renders as plain text.
- Chunking operates on the SOURCE text (before rendering) so a code
  fence split across chunks stays a valid fence after rendering.
"""

from __future__ import annotations

import html
import re

TELEGRAM_MESSAGE_LIMIT = 4096

# Reserve inside each chunk when a continuation indicator is appended
# (Hermes reserves 10 for " (i/n)"; we reserve the same budget).
_CHUNK_INDICATOR_RESERVE = 10

_LINK_RE = re.compile(r"\[([^\]\n]{1,512})\]\((https?://[^\s<>()]+|tg://[^\s<>()]+)\)")
_BAD_URL_RE = re.compile(r"\[([^\]\n]{1,512})\]\((?!https?://|tg://)[^\s<>()]*\)")
_CODE_FENCE_RE = re.compile(r"```([A-Za-z0-9_+\-]*)[ \t]*\n(.*?)(?:```|\Z)", re.DOTALL)
# Inline code spans: backtick runs (1-3) around content without newlines.
_INLINE_CODE_RE = re.compile(r"(?<!`)(`{1,3})(?!`)([^`]+?)(?<!`)\1(?!`)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])")
_ITALIC_UNDERSCORE_RE = re.compile(r"(?<![\w_])_([^_\n]+?)_(?![\w_])")
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_HEADER_RE = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*#*\s*$")
_BLOCKQUOTE_RE = re.compile(r"(?m)^&gt;\s?(.*)$")


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units — the unit Telegram limits count in.

    Astral-plane characters (emoji, CJK extension B) count as 2, matching
    Hermes' ``message_len_fn = utf16_len``.
    """
    return len(text.encode("utf-16-le")) // 2


def _escape(value: str) -> str:
    return html.escape(value, quote=False)


def _render_link(match: re.Match[str]) -> str:
    label, url = match.group(1), match.group(2)
    return f'<a href="{_escape(url)}">{_escape(label)}</a>'


def render_telegram_html(text: str) -> str:
    """Render a conservative markdown subset into Telegram-safe HTML.

    Supported (Hermes-parity subset, deterministic):
    - fenced code blocks ```lang?\\n...``` → <pre><code>
    - inline code ``...`` / `...` → <code>
    - **bold** → <b>, *italic* / _italic_ → <i>, ~~strike~~ → <s>
    - [label](http(s)/tg url) → <a>; other schemes degrade to plain text
    - ATX headers #..###### → a bold line (Telegram has no headers)
    - blockquote > → <blockquote>

    Everything else is escaped verbatim. Unmatched markers (a lone ``*``,
    an unclosed fence) survive as literal characters instead of being
    dropped — rendering is never lossy.
    """
    source = str(text or "")
    if not source:
        return ""

    # Pass 1 — stash fenced code blocks: their bodies must NOT be
    # markdown-processed, only escaped once at restore time.
    stash: list[tuple[str, str]] = []

    def _stash_fence(match: re.Match[str]) -> str:
        lang, body = match.group(1), match.group(2)
        token = f"\x00FENCE{len(stash)}\x00"
        inner = _escape(body.rstrip("\n"))
        rendered = (
            f'<pre><code class="language-{_escape(lang)}">{inner}</code></pre>'
            if lang
            else f"<pre><code>{inner}</code></pre>"
        )
        stash.append((token, rendered))
        return token

    source = _CODE_FENCE_RE.sub(_stash_fence, source)

    # Pass 2 — escape everything that remains. Later substitutions
    # operate on escaped text and only INSERT tags around escaped spans.
    source = _escape(source)

    # Pass 3 — links (before other passes so label punctuation survives).
    source = _LINK_RE.sub(_render_link, source)
    # Non-approved schemes degrade to plain text — a ``jabber:`` or
    # ``file:`` URI must never become a clickable tag.
    source = _BAD_URL_RE.sub(lambda m: f"{m.group(1)} (link removed)", source)

    # Pass 4 — headers become bold lines (Telegram has no header entity).
    source = _HEADER_RE.sub(lambda m: f"<b>{m.group(2)}</b>", source)

    # Pass 5 — inline code (content is already escaped; backticks
    # themselves survive html.escape untouched).
    source = _INLINE_CODE_RE.sub(lambda m: f"<code>{m.group(2)}</code>", source)

    # Pass 6 — emphasis.
    source = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", source)
    source = _ITALIC_STAR_RE.sub(lambda m: f"<i>{m.group(1)}</i>", source)
    source = _ITALIC_UNDERSCORE_RE.sub(lambda m: f"<i>{m.group(1)}</i>", source)
    source = _STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", source)

    # Pass 7 — blockquotes. In escaped text ">" became "&gt;", hence the
    # pattern above matches the escaped form.
    source = _BLOCKQUOTE_RE.sub(lambda m: f"<blockquote>{m.group(1)}</blockquote>", source)

    # Pass 8 — restore stashed code fences (already fully rendered).
    for token, rendered in stash:
        source = source.replace(token, rendered)

    return source


def render_telegram_html_bounded(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> str:
    """Render markdown to HTML that fits ``limit`` UTF-16 units.

    The bound is applied to the SOURCE text (via :func:`chunk_telegram_text`)
    and never to the rendered result: slicing rendered HTML can cut a tag or a
    character entity in half (``<b`` , ``&am``) and Telegram then rejects the
    whole message with 400 "can't parse entities".

    Rendering expands text — ``&`` becomes ``&amp;``, emphasis gains tags — so
    a source that fits the bound can still render over it. The source budget
    therefore shrinks until the rendered frame fits.

    Only the first chunk is returned: a single message (or in-place edit) has
    one text field, so the bound is a real limit rather than a split point.
    """
    source = str(text or "")
    if not source:
        return ""
    bound = max(64, int(limit))
    budget = bound
    while True:
        chunks = chunk_telegram_text(source, limit=budget)
        rendered = render_telegram_html(chunks[0]) if chunks else ""
        units = utf16_len(rendered)
        if units <= bound or budget <= 64:
            return rendered
        # Scale by the observed expansion ratio, but always give up at
        # least 5% so a near-boundary frame cannot stall the loop.
        budget = max(64, min(budget * bound // units, budget - budget // 20))


def _split_at(text: str, limit: int) -> tuple[str, str]:
    """Split ``text`` so the head fits ``limit`` UTF-16 units.

    Prefers paragraph breaks, then line breaks, then spaces, then a hard
    split. A hard split never lands inside a surrogate pair because the
    budget is consumed per character in UTF-16 units.
    """
    if utf16_len(text) <= limit:
        return text, ""
    units = 0
    hard_index = 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit:
            hard_index = index
            break
        units += width
        hard_index = index + 1
    window = text[:hard_index]
    cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
    if cut <= 0:
        return text[:hard_index], text[hard_index:]
    return text[:cut], text[cut:].lstrip(" ")


def chunk_telegram_text(
    text: str,
    limit: int = TELEGRAM_MESSAGE_LIMIT,
    *,
    with_indicators: bool = False,
) -> list[str]:
    """Split ``text`` into Telegram-sized chunks measured in UTF-16 units.

    Code fences are preserved across splits: a fence still open at a
    chunk boundary is closed at the end of that chunk and reopened at the
    start of the next chunk with the same language tag (Hermes
    ``truncate_message`` parity). Content is never dropped: when a single
    fence exceeds the whole budget it is hard-split inside, still with
    balanced open/close markers per chunk.
    """
    source = str(text or "")
    if not source:
        return []
    budget = max(64, int(limit) - (_CHUNK_INDICATOR_RESERVE if with_indicators else 0))

    # Phase 1 — segment the source into ordered blocks: plain text runs
    # and whole fenced blocks (opener line + body + closing fence when
    # present; the fence regex is non-greedy so a block always ends at
    # its own closer, or at end-of-source for an unclosed fence).
    blocks: list[tuple[str, str, str | None]] = []
    cursor = 0
    for match in _CODE_FENCE_RE.finditer(source):
        if match.start() > cursor:
            blocks.append(("text", source[cursor : match.start()], None))
        blocks.append(("fence", match.group(0), match.group(1) or ""))
        cursor = match.end()
    if cursor < len(source):
        blocks.append(("text", source[cursor:], None))

    chunks: list[str] = []
    current: list[str] = []
    current_units = 0

    def _flush() -> None:
        nonlocal current, current_units
        if current:
            chunks.append("".join(current))
            current = []
            current_units = 0

    def _append(piece: str) -> None:
        nonlocal current_units
        current.append(piece)
        current_units += utf16_len(piece)

    def _remaining() -> int:
        return budget - current_units

    for kind, block_text, lang in blocks:
        if kind == "text":
            remainder = block_text
            while remainder:
                if utf16_len(remainder) <= _remaining():
                    _append(remainder)
                    remainder = ""
                    break
                head, remainder = _split_at(remainder, _remaining())
                if not head:
                    # Degenerate remaining budget (cannot happen with
                    # budget >= 64, but never loop forever).
                    head, remainder = remainder[:1], remainder[1:]
                _append(head)
                _flush()
            continue

        # Fence block: re-emit with balanced markers around any split.
        opener = f"```{lang}\n"
        closer = "\n```"
        body = block_text
        newline_index = body.find("\n")
        body = body[newline_index + 1 :] if newline_index != -1 else ""
        if body.endswith("```"):
            body = body[:-3]
        while True:
            if utf16_len(opener) > _remaining():
                _flush()
            _append(opener)
            usable = _remaining() - utf16_len(closer)
            if utf16_len(body) <= usable:
                _append(body)
                _append(closer)
                _flush()
                break
            head, body = _split_at(body, usable)
            if not head:
                # Degenerate usable budget — close this chunk and retry.
                _append(closer)
                _flush()
                continue
            _append(head)
            _append(closer)
            _flush()
    _flush()

    if with_indicators and len(chunks) > 1:
        total = len(chunks)
        return [f"{chunk} ({index}/{total})" for index, chunk in enumerate(chunks, start=1)]
    return chunks


__all__ = [
    "TELEGRAM_MESSAGE_LIMIT",
    "chunk_telegram_text",
    "render_telegram_html",
    "render_telegram_html_bounded",
    "utf16_len",
]
