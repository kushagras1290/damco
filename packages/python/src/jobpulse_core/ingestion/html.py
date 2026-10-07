"""HTML sanitisation (nh3) and text extraction (selectolax)."""

from __future__ import annotations

import html as html_lib
import re

import nh3
from selectolax.lexbor import LexborHTMLParser as HTMLParser

ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "a",
        "b",
        "blockquote",
        "br",
        "code",
        "div",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "i",
        "li",
        "ol",
        "p",
        "pre",
        "span",
        "strong",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    },
)
ALLOWED_ATTRIBUTES: dict[str, set[str]] = {"a": {"href", "title"}}
ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})
MAX_HTML_CHARS = 500_000
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_BLOCK_TAGS = ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "ul", "ol")


def sanitize_html(raw_html: str) -> str:
    """Strip scripts, styles, event handlers and unknown tags. Output is safe to render."""
    if not raw_html:
        return ""
    # Greenhouse returns entity-escaped HTML; unescape once before sanitising.
    candidate = raw_html[:MAX_HTML_CHARS]
    if "&lt;" in candidate and "<" not in candidate:
        candidate = html_lib.unescape(candidate)
    return nh3.clean(
        candidate,
        tags=set(ALLOWED_TAGS),
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=set(ALLOWED_URL_SCHEMES),
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
    )


def html_to_text(raw_html: str) -> str:
    """Readable plain text with paragraph breaks preserved."""
    if not raw_html:
        return ""
    tree = HTMLParser(raw_html)
    for node in tree.css("script, style, noscript, template"):
        node.decompose()
    for tag in _BLOCK_TAGS:
        for node in tree.css(tag):
            node.insert_after("\n")
    root = tree.body or tree.root
    text = root.text(separator=" ") if root is not None else ""
    text = html_lib.unescape(text)
    lines = [_WS_RE.sub(" ", line).strip() for line in text.split("\n")]
    joined = "\n".join(lines)
    return _BLANK_LINES_RE.sub("\n\n", joined).strip()
