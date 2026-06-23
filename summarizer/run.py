#!/usr/bin/env python3
"""
Summarize collected manuscripts into Pydantic-validated JSON checklists via
Gemma 4 12B.

Usage:
    python -m summarizer.run                 # all downloaded papers
    python -m summarizer.run --pmcid PMC7145790   # single paper
    python -m summarizer.run --limit 5       # pilot on first N
    python -m summarizer.run --force          # re-summarize even if cached

Env:
    GEMMA_BASE_URL  default http://bioinfolder.com:8000/v1
    GEMMA_API_KEY    required
    GEMMA_MODEL      default unsloth/gemma-4-12B-it-qat-GGUF
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Load .env if present (optional dependency — skipped if unavailable).
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from .extract import extract, pmcid_from_filename
from .llm_client import get_client, summarize_text
from .schema import ManuscriptChecklist, SummaryBatch

PAPERS_DIR = Path("papers")
SUMMARY_DIR = PAPERS_DIR / "summaries"
COMBINED_PATH = PAPERS_DIR / "manuscript_summaries.json"
LOG_PATH = Path("papers/download_log.json")


def load_metadata() -> dict[str, dict]:
    """pmcid -> {title, year, journal, authors} from the download log."""
    if not LOG_PATH.exists():
        return {}
    log = json.loads(LOG_PATH.read_text())
    return {p["pmcid"]: p for p in log.get("papers", [])}


def discover_files() -> list[Path]:
    """All downloaded full-text files (PDF + XML), pmcid-sorted."""
    files = sorted(
        list(PAPERS_DIR.glob("*.pdf")) + list(PAPERS_DIR.glob("*.xml")),
        key=lambda p: pmcid_from_filename(p),
    )
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pmcid", help="Summarize a single paper by PMCID.")
    ap.add_argument("--limit", type=int, help="Only process the first N papers.")
    ap.add_argument("--force", action="store_true",
                    help="Re-summarize even if a cached JSON exists.")
    args = ap.parse_args()

    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    meta = load_metadata()

    files = discover_files()
    if args.pmcid:
        pmcid = args.pmcid.upper()
        if not pmcid.startswith("PMC"):
            pmcid = f"PMC{pmcid}"
        files = [f for f in files if pmcid_from_filename(f) == pmcid]
        if not files:
            print(f"No downloaded file found for {pmcid}", file=sys.stderr)
            return 1
    if args.limit:
        files = files[: args.limit]

    if not files:
        print("No papers found under papers/. Run `make download` first.", file=sys.stderr)
        return 1

    try:
        client, model = get_client()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    print(f"Model: {model}")
    print(f"Papers to summarize: {len(files)}")
    print("=" * 60)

    ok = failed = skipped = 0
    checklists: list[ManuscriptChecklist] = []

    for idx, path in enumerate(files, 1):
        pmcid = pmcid_from_filename(path)
        out_path = SUMMARY_DIR / f"{pmcid}.json"

        if out_path.exists() and not args.force:
            try:
                checklists.append(ManuscriptChecklist.model_validate_json(
                    out_path.read_text()))
                skipped += 1
                print(f"  [{idx:>3}/{len(files)}] {pmcid}  [skip — cached]")
                continue
            except Exception:
                pass  # cached file invalid → re-summarize

        m = meta.get(pmcid, {})
        title = m.get("title", path.stem)
        year = m.get("year", "")

        print(f"  [{idx:>3}/{len(files)}] {pmcid}  {title[:50]}")
        try:
            text, src_fmt = extract(path)
            checklist = summarize_text(
                text=text, pmcid=pmcid, title=title, year=year,
                source_format=src_fmt, client=client, model=model,
            )
            out_path.write_text(checklist.model_dump_json(indent=2))
            checklists.append(checklist)
            ehr = "EHR" if checklist.ehr_used else "no-EHR"
            print(f"           ✓ {ehr} | "
                  f"{len(checklist.pathologies_diseases)} disease(s) | "
                  f"{len(checklist.captured_features)} feature(s)")
            ok += 1
        except Exception as e:
            print(f"           ✗ {type(e).__name__}: {str(e)[:120]}")
            failed += 1

        time.sleep(0.3)

    # ── combined file ───────────────────────────────────────────────────
    checklists.sort(key=lambda c: (c.year, c.pmcid))
    batch = SummaryBatch(n=len(checklists), model=model, summaries=checklists)
    COMBINED_PATH.write_text(batch.model_dump_json(indent=2))
    n_ehr = sum(1 for c in checklists if c.ehr_used)

    print("=" * 60)
    print(f"  Summarized : {ok}")
    print(f"  Skipped    : {skipped}  (cached)")
    print(f"  Failed     : {failed}")
    print(f"  Total in combined file: {len(checklists)}")
    print(f"  EHR-based  : {n_ehr} / {len(checklists)}")
    print(f"  Per-paper  : {SUMMARY_DIR}/")
    print(f"  Combined   : {COMBINED_PATH}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
