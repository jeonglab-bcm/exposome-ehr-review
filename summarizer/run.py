#!/usr/bin/env python3
"""
Summarize collected manuscripts into Pydantic-validated JSON checklists via
the configured OpenAI-compatible model.

Usage:
    python -m summarizer.run                 # all downloaded papers
    python -m summarizer.run --pmcid PMC7145790   # single paper
    python -m summarizer.run --limit 5       # pilot on first N
    python -m summarizer.run --force          # re-summarize even if cached

Env:
    EXPOSOME_LLM_BASE_URL  default https://mac-mini.tail5aee49.ts.net/v1
    EXPOSOME_LLM_API_KEY   optional (not needed for the tailnet-internal endpoint)
    EXPOSOME_LLM_MODEL     default ornith-1.5-35b
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
from paper_manifest import (
    PaperManifest,
    atomic_write_text,
    canonical_manifest_path,
    discover_included_papers,
    sha256_file,
    sha256_text,
    summary_cache_matches,
    write_summary_cache_metadata,
)
from output_provenance import dominant_model

from .llm_client import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    SOURCE_CHAR_BUDGET,
    SYSTEM_PROMPT,
    build_user_prompt,
    get_client,
    summarize_chunked,
    summarize_text,
)
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
MANIFEST_PATH = PAPERS_DIR / "manifest.json"

# Cache identity is code-derived: changing either prompt text or the Pydantic
# schema automatically makes previous sidecars stale.  Metadata itself lives
# under summaries/.cache/ so it cannot be mistaken for a paper checklist.
PROMPT_CHECKSUM = sha256_text(
    SYSTEM_PROMPT
    + "\n"
    + build_user_prompt("<<SOURCE>>", title="<<TITLE>>", year="<<YEAR>>")
)
SCHEMA_CHECKSUM = sha256_text(json.dumps(
    ManuscriptChecklist.model_json_schema(), sort_keys=True, separators=(",", ":"),
))
PROCESSING_CHECKSUM = sha256_text(json.dumps({
    "source_char_budget": SOURCE_CHAR_BUDGET,
    "chunk_size": CHUNK_SIZE,
    "chunk_overlap": CHUNK_OVERLAP,
    # Hash the implementation modules so extractor/merge changes invalidate
    # caches even when the prompt and Pydantic schema are unchanged.
    "extract_module": sha256_file(Path(__file__).with_name("extract.py")),
    "llm_client_module": sha256_file(Path(__file__).with_name("llm_client.py")),
}, sort_keys=True, separators=(",", ":")))


def _summarization_mode(*, chunked: bool, recover: bool) -> str:
    if chunked:
        return "chunked"
    return "single-with-chunked-recovery" if recover else "single"


def _source_identity_path(path: Path) -> str:
    try:
        return canonical_manifest_path(path, papers_dir=PAPERS_DIR)
    except ValueError:
        # Unit-test/external callers may use a source outside this repository;
        # keep a stable absolute identity without weakening production paths.
        return path.resolve().as_posix()


def load_metadata() -> dict[str, dict]:
    """pmcid -> {title, year, journal, authors} from the download log."""
    if not LOG_PATH.exists():
        return {}
    log = json.loads(LOG_PATH.read_text())
    return {p["pmcid"]: p for p in log.get("papers", [])}


def discover_files() -> list[Path]:
    """Explicitly included full texts, in deterministic PMCID order."""
    return discover_included_papers(MANIFEST_PATH, PAPERS_DIR, validate=True)


def load_all_summaries(
    summary_dir: Path,
    *,
    allowed_pmcids: set[str] | None = None,
) -> list[ManuscriptChecklist]:
    """Valid per-paper checklists, optionally filtered to an authorized set.

    Extra summary files are deliberately retained on disk for audit/review but
    cannot enter the combined artifact when ``allowed_pmcids`` is supplied.
    """
    out: list[ManuscriptChecklist] = []
    for p in sorted(summary_dir.glob("*.json")):
        file_pmcid = p.stem.upper()
        if allowed_pmcids is not None and file_pmcid not in allowed_pmcids:
            continue
        try:
            checklist = ManuscriptChecklist.model_validate_json(p.read_text())
            if checklist.pmcid != file_pmcid:
                raise ValueError(
                    f"record PMCID {checklist.pmcid} does not match filename {file_pmcid}"
                )
            out.append(checklist)
        except Exception as e:
            print(f"  ⚠ unreadable summary {p.name}: {type(e).__name__}: {e}")
    return out


def _paper_metadata(path: Path, meta: dict[str, dict]) -> tuple[str, str]:
    pmcid = pmcid_from_filename(path)
    row = meta.get(pmcid, {})
    return str(row.get("title") or path.stem), str(row.get("year") or "")


def _cached_checklist(
    path: Path,
    *,
    model: str,
    meta: dict[str, dict] | None = None,
    summary_dir: Path = SUMMARY_DIR,
    summarization_mode: str | None = "",
) -> ManuscriptChecklist | None:
    """Return a checklist only when its content and cache identity are valid."""
    pmcid = pmcid_from_filename(path)
    summary_path = summary_dir / f"{pmcid}.json"
    title, year = _paper_metadata(path, meta or {})
    source_identity = _source_identity_path(path)
    try:
        checklist = ManuscriptChecklist.model_validate_json(summary_path.read_text())
        source_checksum = sha256_file(path)
    except Exception:
        return None
    if not summary_cache_matches(
        summary_path,
        pmcid=pmcid,
        source_path=path,
        source_identity_path=source_identity,
        source_checksum=source_checksum,
        model=model,
        prompt_checksum=PROMPT_CHECKSUM,
        schema_checksum=SCHEMA_CHECKSUM,
        title=title,
        year=year,
        processing_checksum=PROCESSING_CHECKSUM,
        summarization_mode=summarization_mode,
    ):
        return None
    if checklist.pmcid != pmcid or checklist.title != title or checklist.year != year:
        return None
    return checklist


def find_failed(
    *,
    model: str,
    files: list[Path] | None = None,
    meta: dict[str, dict] | None = None,
    summarization_mode: str = "",
) -> list[Path]:
    """Files with no current summary (missing, invalid, or stale cache)."""
    candidates = discover_files() if files is None else files
    return [
        path for path in candidates
        if _cached_checklist(
            path, model=model, meta=meta,
            summarization_mode=summarization_mode,
        ) is None
    ]


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
    title, year = _paper_metadata(path, meta)
    source_identity = _source_identity_path(path)
    mode = _summarization_mode(chunked=chunked, recover=recover)

    try:
        source_checksum = sha256_file(path)
    except Exception as e:
        return PaperResult(pmcid=pmcid, status="failed", checklist=None,
                           error=f"{type(e).__name__}: {e}")

    # A summary JSON alone is not a valid cache.  The sidecar ties it to the
    # exact source bytes, prompt, schema, and model used to create it.
    if out_path.exists() and not force and summary_cache_matches(
        out_path,
        pmcid=pmcid,
        source_path=path,
        source_identity_path=source_identity,
        source_checksum=source_checksum,
        model=model,
        prompt_checksum=PROMPT_CHECKSUM,
        schema_checksum=SCHEMA_CHECKSUM,
        title=title,
        year=year,
        processing_checksum=PROCESSING_CHECKSUM,
        summarization_mode=mode,
    ):
        try:
            checklist = ManuscriptChecklist.model_validate_json(out_path.read_text())
            if (checklist.pmcid, checklist.title, checklist.year) == (pmcid, title, year):
                return PaperResult(pmcid=pmcid, status="skipped", checklist=checklist)
        except Exception:
            pass  # cached file invalid → re-summarize

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
        if (checklist.pmcid, checklist.title, checklist.year) != (pmcid, title, year):
            raise ValueError(
                "summarizer returned identity fields that do not match the source metadata"
            )
        atomic_write_text(out_path, checklist.model_dump_json(indent=2) + "\n")
        write_summary_cache_metadata(
            out_path,
            pmcid=pmcid,
            source_path=path,
            source_identity_path=source_identity,
            source_checksum=source_checksum,
            model=model,
            prompt_checksum=PROMPT_CHECKSUM,
            schema_checksum=SCHEMA_CHECKSUM,
            title=title,
            year=year,
            processing_checksum=PROCESSING_CHECKSUM,
            summarization_mode=mode,
        )
        return PaperResult(pmcid=pmcid, status="ok", checklist=checklist)
    except Exception as e:
        return PaperResult(pmcid=pmcid, status="failed", checklist=None,
                           error=f"{type(e).__name__}: {e}")


def _sync_manifest_summary_statuses(
    included: dict[str, Path],
    current_pmcids: set[str],
    *,
    manifest_path: Path = MANIFEST_PATH,
) -> None:
    """Make manifest ``summarized`` status reflect current cache-valid output."""
    manifest = PaperManifest(manifest_path)
    changed = False
    for pmcid in sorted(included):
        record = manifest.records.get(pmcid)
        if record is None:
            continue
        desired = "summarized" if pmcid in current_pmcids else "downloaded"
        if record.get("status") == desired:
            continue
        manifest.upsert(pmcid, status=desired)
        changed = True
    if changed:
        manifest.save()


def _write_combined(checklists: list[ManuscriptChecklist]) -> None:
    checklists.sort(key=lambda checklist: (checklist.year, checklist.pmcid))
    batch_model = dominant_model([checklist.model_dump() for checklist in checklists])
    batch = SummaryBatch(n=len(checklists), model=batch_model, summaries=checklists)
    atomic_write_text(COMBINED_PATH, batch.model_dump_json(indent=2) + "\n")


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

    try:
        all_files = discover_files()
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    included = {pmcid_from_filename(path): path for path in all_files}
    files = list(all_files)
    if args.pmcid:
        wanted = {
            p if p.startswith("PMC") else f"PMC{p}"
            for raw in args.pmcid for part in raw.split(",")
            if (p := part.strip().upper())
        }
        files = [f for f in files if pmcid_from_filename(f) in wanted]
        missing = wanted - {pmcid_from_filename(f) for f in files}
        if missing:
            print(f"No downloaded file for: {', '.join(sorted(missing))}", file=sys.stderr)
        if not files:
            return 1

    if not all_files:
        _sync_manifest_summary_statuses(included, set())
        _write_combined([])
        print("No explicitly included full-text papers; wrote an empty combined artifact.")
        return 0

    try:
        client, model = get_client()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    mode = _summarization_mode(chunked=args.chunked, recover=args.recover)
    if args.recover:
        files = find_failed(
            model=model,
            files=files,
            meta=meta,
            summarization_mode=mode,
        )
    if args.limit:
        files = files[: args.limit]
    if not files:
        print("All selected papers have current cached summaries; rebuilding combined output.")

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

    # Re-evaluate every included paper after the run.  This filters retained
    # stale/excluded JSON files without deleting them and prevents a failed
    # refresh from publishing an old cache against changed source bytes.
    all_checklists: list[ManuscriptChecklist] = []
    for path in all_files:
        checklist = _cached_checklist(
            path,
            model=model,
            meta=meta,
            summarization_mode=None,  # any current, recorded execution mode
        )
        if checklist is not None:
            all_checklists.append(checklist)
    current_pmcids = {checklist.pmcid for checklist in all_checklists}
    _sync_manifest_summary_statuses(included, current_pmcids)
    publication_complete = current_pmcids == set(included)
    if failed == 0 and publication_complete:
        _write_combined(all_checklists)
    else:
        reason = (
            "one or more refreshes failed"
            if failed
            else "the selected pilot did not produce the complete included set"
        )
        print(f"  Combined output left unchanged because {reason}.")
    n_ehr = sum(1 for c in all_checklists if c.ehr_used)
    checklists = all_checklists

    print("=" * 60)
    print(f"  Summarized : {ok}")
    print(f"  Skipped    : {skipped}  (cached)")
    print(f"  Failed     : {failed}")
    label = (
        "Total in combined file"
        if failed == 0 and publication_complete
        else "Current valid summaries"
    )
    print(f"  {label}: {len(checklists)}")
    print(f"  EHR-based  : {n_ehr} / {len(checklists)}")
    print(f"  Per-paper  : {SUMMARY_DIR}/")
    unchanged = failed > 0 or not publication_complete
    print(f"  Combined   : {COMBINED_PATH}" + (" (unchanged)" if unchanged else ""))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
