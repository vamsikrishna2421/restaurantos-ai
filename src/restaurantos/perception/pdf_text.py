"""The text of a text-based PDF, read without a PDF library (pass 13).

Purchase bills arrive as photos or as PDFs. The app carries no PDF dependency, so this reads the
common case itself: content streams (FlateDecode via ``zlib``, or plain) and the strings their
``Tj``/``TJ`` text-showing operators draw, in stream order. That covers the bills accounting tools
and suppliers actually export. Scanned-image PDFs carry no text layer — ``extract_text`` returns
None for them and the caller says so plainly (take a photo of the bill instead) rather than guessing.
"""

from __future__ import annotations

import re
import zlib

_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
_TEXT_OBJECT = re.compile(rb"BT(.*?)ET", re.DOTALL)
_SHOW = re.compile(rb"\((?:\\.|[^\\()])*\)\s*(?:Tj|')|\[(?:[^\[\]]|\([^)]*\))*\]\s*TJ", re.DOTALL)
_STRING = re.compile(rb"\((?:\\.|[^\\()])*\)")
MAX_TEXT_CHARS = 20_000


def _unescape(raw: bytes) -> str:
    out = raw.decode("latin-1")
    out = out.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
    out = re.sub(r"\\[0-7]{1,3}", " ", out)
    return out.replace("\\n", " ").replace("\\r", " ").replace("\\t", " ")


def extract_text(data: bytes) -> str | None:
    """The PDF's drawn text, one line per text object, or None when there is no usable text layer."""
    if not data.startswith(b"%PDF"):
        return None
    lines: list[str] = []
    for match in _STREAM.finditer(data):
        raw = match.group(1)
        try:
            content = zlib.decompress(raw)
        except zlib.error:
            content = raw
        for block in _TEXT_OBJECT.finditer(content):
            parts = [_unescape(s.group(0)[1:-1]) for show in _SHOW.finditer(block.group(1))
                     for s in _STRING.finditer(show.group(0))]
            line = " ".join(p for p in (part.strip() for part in parts) if p)
            if line:
                lines.append(line)
    text = "\n".join(lines).strip()
    return text[:MAX_TEXT_CHARS] if len(text) >= 8 else None
