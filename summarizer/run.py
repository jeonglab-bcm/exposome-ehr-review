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
    GEMMA_BASE_URL  default https://mac-mini.tail5aee49.ts.net/v1
    GEMMA_MODEL      default gemma4-12b-qat-gguf
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

# Load .env if present (optional dependency — skipped if unavailable).
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from .extract import extract, pmcid_from_filename
from .llm_client import get_client, summarize_text, summarize_chunked
from .schema import ManuscriptChecklist, SummaryBatch


@dataclass
class PaperResult:
    """Result of processing a single paper."""
    pmcid: str
    status: str  # "ok" | "skipped" | "failed"
    checklist: ManuscriptChecklist | None = None
    error: str = ""  # why it failed — otherwise failures are silent

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


def load_all_summaries(summary_dir: Path) -> list[ManuscriptChecklist]:
    """Every valid per-paper checklist on disk.

    The combined file is rebuilt from this rather than from just the papers a
    given run touched — otherwise a partial or failed run truncates it (a failed
    single-paper run once left the combined file at n=0).
    """
    out: list[ManuscriptChecklist] = []
    for p in sorted(summary_dir.glob("*.json")):
        try:
            out.append(ManuscriptChecklist.model_validate_json(p.read_text()))
        except Exception as e:
            print(f"  ⚠ unreadable summary {p.name}: {type(e).__name__}: {e}")
    return out


def find_failed() -> list[Path]:
    """Files whose PMCID has no summary JSON yet (the failed/missing set)."""
    done = {p.stem for p in SUMMARY_DIR.glob("*.json")}
    return [f for f in discover_files() if pmcid_from_filename(f) not in done]


def _process_one(
    path: Path,
    meta: dict,
    client,
    model: str,
    chunked: bool,
    recover: bool,
    summary_dir: Path,
    force: bool = False,
) -> PaperResult:
    """Process a single paper: extract -> summarize -> write per-paper JSON.

    Thread-safe: each paper writes to its own file; the OpenAI client is
    safe for concurrent use. Returns a PaperResult (never raises).
    """
    pmcid = pmcid_from_filename(path)
    out_path = summary_dir / f"{pmcid}.json"

    # Skip if a valid cached summary exists (unless --force re-summarizes)
    if out_path.exists() and not force:
        try:
            checklist = ManuscriptChecklist.model_validate_json(out_path.read_text())
            return PaperResult(pmcid=pmcid, status="skipped", checklist=checklist)
        except Exception:
            pass  # cached file invalid → re-summarize

    m = meta.get(pmcid, {})
    title = m.get("title", path.stem)
    year = m.get("year", "")

    try:
        text, src_fmt = extract(path)
        if chunked:
            checklist = summarize_chunked(
                text=text, pmcid=pmcid, title=title, year=year,
                source_format=src_fmt, client=client, model=model,
            )
        else:
            try:
                checklist = summarize_text(
                    text=text, pmcid=pmcid, title=title, year=year,
                    source_format=src_fmt, client=client, model=model,
                )
            except Exception as single_err:
                if not recover:
                    raise
                checklist = summarize_chunked(
                    text=text, pmcid=pmcid, title=title, year=year,
                    source_format=src_fmt, client=client, model=model,
                )
        out_path.write_text(checklist.model_dump_json(indent=2))
        return PaperResult(pmcid=pmcid, status="ok", checklist=checklist)
    except Exception as e:
        return PaperResult(pmcid=pmcid, status="failed", checklist=None,
                           error=f"{type(e).__name__}: {e}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pmcid", nargs="+",
                    help="Summarize specific papers by PMCID "
                         "(space- or comma-separated).")
    ap.add_argument("--limit", type=int, help="Only process the first N papers.")
    ap.add_argument("--force", action="store_true",
                    help="Re-summarize even if a cached JSON exists.")
    ap.add_argument("--recover", action="store_true",
                    help="Only process papers without a summary yet; fall back to "
                         "chunked (dissect-into-two) extraction when single-shot fails.")
    ap.add_argument("--chunked", action="store_true",
                    help="Always use chunked extraction (dissect into chunks + merge).")
    ap.add_argument("--workers", type=int, default=1,
                    help="Number of concurrent LLM workers (default 1 = serial). "
                         "The OpenAI client is thread-safe; useful when the "
                         "bottleneck is network-bound LLM calls.")
    args = ap.parse_args(argv)

    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    meta = load_metadata()

    if args.recover:
        files = find_failed()
    elif args.pmcid:
        wanted = {
            p if p.startswith("PMC") else f"PMC{p}"
            for raw in args.pmcid for part in raw.split(",")
            if (p := part.strip().upper())
        }
        files = [f for f in discover_files() if pmcid_from_filename(f) in wanted]
        missing = wanted - {pmcid_from_filename(f) for f in files}
        if missing:
            print(f"No downloaded file for: {', '.join(sorted(missing))}", file=sys.stderr)
        if not files:
            return 1
    else:
        files = discover_files()
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
    print(f"Workers: {args.workers}")
    print("=" * 60)

    ok = failed = skipped = 0
    checklists: list[ManuscriptChecklist] = []

    def _handle(result: PaperResult) -> None:
        nonlocal ok, failed, skipped
        if result.status == "skipped":
            skipped += 1
            print(f"  {result.pmcid}  [skip — cached]")
        elif result.status == "ok":
            ok += 1
            checklists.append(result.checklist)
            ehr = "EHR" if result.checklist.ehr_used else "no-EHR"
            print(f"  {result.pmcid}  ✓ {ehr} | "
                  f"{len(result.checklist.pathologies_diseases)} disease(s) | "
                  f"{len(result.checklist.captured_features)} feature(s)")
        else:
            failed += 1
            print(f"  {result.pmcid}  ✗ failed — {result.error or 'unknown error'}")

    if args.workers <= 1:
        for idx, path in enumerate(files, 1):
            result = _process_one(
                path=path, meta=meta, client=client, model=model,
                chunked=args.chunked, recover=args.recover,
                summary_dir=SUMMARY_DIR, force=args.force,
            )
            _handle(result)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(
                    _process_one, path=p, meta=meta, client=client, model=model,
                    chunked=args.chunked, recover=args.recover,
                    summary_dir=SUMMARY_DIR, force=args.force,
                ): p for p in files
            }
            for fut in as_completed(futures):
                _handle(fut.result())

    # ── combined file ───────────────────────────────────────────────────
    # Rebuilt from every per-paper JSON on disk, so a partial run adds to the
    # combined file instead of replacing it with only what it processed.
    all_checklists = load_all_summaries(SUMMARY_DIR)
    all_checklists.sort(key=lambda c: (c.year, c.pmcid))
    batch = SummaryBatch(n=len(all_checklists), model=model, summaries=all_checklists)
    COMBINED_PATH.write_text(batch.model_dump_json(indent=2))
    n_ehr = sum(1 for c in all_checklists if c.ehr_used)
    checklists = all_checklists

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
