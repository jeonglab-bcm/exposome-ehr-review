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
    GEMMA_BASE_URL  default https://llm.bioinfolder.com/v1
    GEMMA_API_KEY    required
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


def load_all_summaries(summary_dir: Path = SUMMARY_DIR) -> list[ManuscriptChecklist]:
    """Every valid per-paper checklist on disk, not just this run's.

    The combined file describes the whole corpus, so it is rebuilt from
    ``papers/summaries/`` rather than from the papers this invocation happened
    to touch — otherwise ``--recover`` / ``--pmcid`` / ``--limit`` runs (and
    fully-cached re-runs, where every paper is skipped) would overwrite it with
    a near-empty batch.
    """
    out: list[ManuscriptChecklist] = []
    for f in sorted(summary_dir.glob("*.json")):
        try:
            out.append(ManuscriptChecklist.model_validate_json(f.read_text()))
        except Exception as e:
            print(f"  ! skipping unreadable summary {f.name}: {type(e).__name__}",
                  file=sys.stderr)
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

    # Skip if a valid cached summary exists (unless --force re-extracts)
    if not force and out_path.exists():
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
    except Exception:
        return PaperResult(pmcid=pmcid, status="failed", checklist=None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pmcid", help="Summarize a single paper by PMCID.")
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

    if args.recover and not args.force:
        files = find_failed()
    elif args.force and not args.pmcid:
        # --force re-extracts everything, including papers --recover would skip
        files = discover_files()
    elif args.pmcid:
        pmcid = args.pmcid.upper()
        if not pmcid.startswith("PMC"):
            pmcid = f"PMC{pmcid}"
        files = [f for f in discover_files() if pmcid_from_filename(f) == pmcid]
        if not files:
            print(f"No downloaded file found for {pmcid}", file=sys.stderr)
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

    def _handle(result: PaperResult) -> None:
        nonlocal ok, failed, skipped
        if result.status == "skipped":
            skipped += 1
            print(f"  {result.pmcid}  [skip — cached]")
        elif result.status == "ok":
            ok += 1
            ehr = "EHR" if result.checklist.ehr_used else "no-EHR"
            print(f"  {result.pmcid}  ✓ {ehr} | "
                  f"{len(result.checklist.pathologies_diseases)} disease(s) | "
                  f"{len(result.checklist.captured_features)} feature(s)")
        else:
            failed += 1
            print(f"  {result.pmcid}  ✗ failed")

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
    # rebuilt from every per-paper JSON on disk, so a partial run never drops
    # papers it did not process out of the combined artifact
    checklists = load_all_summaries()
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
