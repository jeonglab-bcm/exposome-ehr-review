"""Regenerate the literature links in relations.yaml from relation_excerpts.yaml.

The excerpt file is the source of truth: every link that rests on the
literature starts life there as a quoted claim. This script rewrites the block
of `relations.yaml` between the generated-section markers so that each claimed
edge appears exactly once, with provenance naming the papers (PMIDs) that back it.
Taxonomy edges (`is_a` structure) live outside the block and are not touched.

Usage::

    python scripts/build_relations.py          # rewrite the block in place
    python scripts/build_relations.py --check  # exit 1 if the block is stale
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from exposome_ehr.config import load_vocabulary  # noqa: E402

RELATIONS = ROOT / "config/relations.yaml"
EXCERPTS = ROOT / "config/relation_excerpts.yaml"
BEGIN = "  # >>> generated from config/relation_excerpts.yaml by scripts/build_relations.py; do not edit\n"
END = "  # <<< end of generated links\n"



def order() -> list[str]:
    """Section order follows the predicates in relations.yaml (the competency questions)."""
    return [p for p in load_vocabulary().predicates if p != "is_a"]


def generated_block() -> str:
    entries = (yaml.safe_load(EXCERPTS.read_text()) or {}).get("excerpts") or [] if EXCERPTS.exists() else []
    by_pred: dict[str, list[str]] = {p: [] for p in order()}
    for entry in entries:
        s, p, o = entry["edge"]
        papers: list[str] = []
        for ev in entry["evidence"]:
            if ev["verdict"] in ("supports", "partial") and ev["paper"] not in papers:
                papers.append(ev["paper"])
        if not papers:
            continue  # a claim nobody backs is not a link
        prov = "; ".join(f"pmid:{paper}" for paper in papers)
        by_pred.setdefault(p, []).append(f"  - [{s}, {p}, {o}, '{prov}']\n")
    out = [BEGIN]
    for p in by_pred:
        if by_pred[p]:
            out.append(f"\n  # {p}\n")
            out.extend(sorted(by_pred[p]))
    out.append(END)
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="fail if relations.yaml is out of date")
    args = ap.parse_args()
    text = RELATIONS.read_text()
    if BEGIN not in text or END not in text:
        print(f"{RELATIONS.relative_to(ROOT)} has no generated-section markers", file=sys.stderr)
        return 2
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    new = head + generated_block() + tail
    if args.check:
        if new != text:
            print("relations.yaml is out of date; run python scripts/build_relations.py", file=sys.stderr)
            return 1
        return 0
    RELATIONS.write_text(new)
    n = generated_block().count("\n  - [")
    print(f"wrote {n} literature links into {RELATIONS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
