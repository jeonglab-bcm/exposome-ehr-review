"""Helpers for presenting model provenance in generated review artifacts."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any


def model_counts(records: Iterable[Mapping[str, Any]]) -> Counter[str]:
    """Count non-empty per-record model identifiers.

    The per-paper records are the provenance source of truth.  In particular,
    this deliberately does not trust a batch-level label left over from an
    earlier run.
    """
    return Counter(
        model
        for record in records
        if (model := str(record.get("model") or "").strip())
    )


def model_provenance(records: Iterable[Mapping[str, Any]]) -> str:
    """Return a compact, deterministic label derived from record metadata."""
    rows = list(records)
    counts = model_counts(rows)
    missing = len(rows) - sum(counts.values())

    if not counts:
        return "not recorded"
    if len(counts) == 1 and missing == 0:
        return next(iter(counts))

    parts = [
        f"{model} ({count})"
        for model, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    if missing:
        parts.append(f"not recorded ({missing})")
    return ", ".join(parts)


def dominant_model(records: Iterable[Mapping[str, Any]]) -> str:
    """Return the most frequent record model (first record wins a count tie)."""
    counts = model_counts(records)
    if not counts:
        return ""
    return counts.most_common(1)[0][0]
