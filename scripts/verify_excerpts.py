"""Check that every quote in config/relation_excerpts.yaml is verbatim in its paper.

Paper texts are not committed. This reads the paragraph files built by
scripts/build_paragraphs.py (work/paragraphs/<pmid>.json) and checks each quote
against the whole paper's raw paragraph text (see scripts/quotes.py for what
counts as a match).

Usage::

    python scripts/verify_excerpts.py [--paragraphs work/paragraphs]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from quotes import match, variants  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--paragraphs", type=Path, default=ROOT / "work/paragraphs")
    ap.add_argument("--excerpts", type=Path, default=ROOT / "config/relation_excerpts.yaml")
    args = ap.parse_args()
    if not args.excerpts.exists():
        print("no relation_excerpts.yaml yet; nothing to verify")
        return 0
    audit = yaml.safe_load(args.excerpts.read_text()) or {}
    cache: dict[str, list[str]] = {}
    bad = checked = spacing = missing = 0
    for section in ("excerpts", "not_added"):
        for entry in audit.get(section) or []:
            for ev in entry.get("evidence", []):
                quote = ev.get("quote")
                if not quote:
                    continue
                key = str(ev["paper"])
                if key not in cache:
                    path = args.paragraphs / f"{key}.json"
                    cache[key] = (variants("\n".join(p["raw_text"] for p in json.loads(path.read_text())["paragraphs"]))
                                  if path.exists() else [])
                if not cache[key]:
                    missing += 1
                    print(f"no paragraphs for PMID {key}: build them with scripts/build_paragraphs.py", file=sys.stderr)
                    continue
                checked += 1
                found = match(quote, cache[key])
                if found:
                    spacing += found == "spacing"
                    continue
                bad += 1
                print(f"NOT FOUND [{key}] {entry['edge']}: {quote[:90]}", file=sys.stderr)
    print(f"{checked - bad}/{checked} quotes verified ({spacing} only after ignoring spacing and hyphenation)"
          + (f"; {missing} could not be checked" if missing else ""))
    return 1 if bad or missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
