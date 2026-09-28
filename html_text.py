"""Minimal stdlib-only HTML-to-plain-text, good enough for job descriptions."""

import html
import re
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3",
    "h4", "h5", "h6", "section", "article",
}
_SKIP_TAGS = {"script", "style", "head", "noscript"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self._skip_depth and data:
            self.chunks.append(data)


def html_to_text(raw_html):
    """Strip tags, decode entities, collapse whitespace. Never raises."""
    if not raw_html:
        return ""
    # Greenhouse's `content` field is HTML that's *also* entity-escaped
    # (e.g. "&lt;div&gt;") -- unescape until it stabilizes, then parse tags.
    text = raw_html
    for _ in range(2):
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped

    parser = _TextExtractor()
    try:
        parser.feed(text)
    except Exception:
        # Fall back to a crude tag strip if the parser chokes on malformed markup.
        return re.sub(r"<[^>]+>", " ", text)

    joined = "".join(parser.chunks)
    joined = html.unescape(joined)
    # Collapse runs of blank lines/spaces left by block-tag markers.
    joined = re.sub(r"[ \t]+", " ", joined)
    joined = re.sub(r"\n[ \t]*\n+", "\n\n", joined)
    return joined.strip()
