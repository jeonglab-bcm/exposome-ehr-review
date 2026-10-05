"""Verbatim-quote matching, shared by the judge and the excerpt check.

Ported from POTS-phenotyping's scripts/verify_excerpts.py. Quotes match after
whitespace is collapsed, typographic quotes and dashes are folded, and words
hyphenated across a PDF line break are rejoined. A quote that only matches once
all whitespace and hyphens are ignored is accepted as 'spacing' and reported
separately.
"""

from __future__ import annotations

import re

FOLD = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "−": "-", " ": " ", " ": " "})


def variants(text: str) -> list[str]:
    text = text.translate(FOLD)
    joined = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)      # "in-\ncreased" -> "increased"
    kept = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1-\2", text)       # "lower-\nbody" -> "lower-body"
    return [re.sub(r"\s+", " ", t) for t in (joined, kept, text)]


def norm(q: str) -> str:
    return re.sub(r"\s+", " ", q.translate(FOLD)).strip()


def match(quote: str, texts: list[str]) -> str | None:
    """'exact', 'spacing' (only once whitespace and hyphens are ignored), or None."""
    if not quote.strip():
        return None
    if any(norm(quote) in v for v in texts):
        return "exact"
    flat = re.sub(r"[\s-]+", "", norm(quote))
    if any(flat in re.sub(r"[\s-]+", "", v) for v in texts):
        return "spacing"
    return None
