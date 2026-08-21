"""Thin operations layer for the Dagster asset pipeline.

Each op wraps an EXISTING script so the pipeline is orchestrated (lineage,
caching/resume, UI) without rewriting the fetcher, summarizer, or results
builder. ``rebuild_combined`` is the one pure-Python step: it derives the
combined ``SummaryBatch`` from the per-paper summary files (no LLM, no TinyDB
dependency), mirroring how the TinyDB store exports.

Everything runs with ``cwd`` at the repo root so the scripts' relative
``papers/`` paths resolve.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

from summarizer.schema import ManuscriptChecklist, SummaryBatch
from database import Store
from paper_manifest import (
    PaperManifest,
    ReconciliationResult,
    publication_ready_paper_files,
    reconcile_manifest,
    summary_cache_matches,
    summary_cache_path,
)

REPO_ROOT = Path(__file__).resolve().parent
PAPERS_DIR = REPO_ROOT / "papers"
SUMMARY_DIR = PAPERS_DIR / "summaries"
COMBINED_PATH = PAPERS_DIR / "manuscript_summaries.json"
DOWNLOAD_LOG = PAPERS_DIR / "download_log.json"
MANIFEST_PATH = PAPERS_DIR / "manifest.json"
DB_PATH = PAPERS_DIR / "db.json"

# Default summarizer mode: resume-only (skip cached papers, chunked recovery on
# failure) + concurrent workers (the bottleneck is network-bound LLM calls).
# Override worker counts via SUMMARIZE_WORKERS / SCAN_WORKERS (e.g. from CI
# workflow_dispatch inputs) without touching the Dagster asset graph.
_summarize_args = ["--recover", "--workers", os.environ.get("SUMMARIZE_WORKERS", "4")]
if os.environ.get("SUMMARIZE_FORCE", "").strip().lower() in ("1", "true", "yes"):
    _summarize_args = ["--force", "--workers", os.environ.get("SUMMARIZE_WORKERS", "4")]
SUMMARIZE_ARGS = _summarize_args

# Focused data-availability pass. This updates the already-created per-paper
# JSONs with data_availability/accession fields before the combined artifact is
# rebuilt. Keeping it as a separate Dagster asset makes the lineage explicit.
DATA_AVAILABILITY_ARGS = ["--workers", os.environ.get("SCAN_WORKERS", "4")]


def _run(cmd: list[str], *, label: str) -> int:
    """Run ``cmd`` in the repo root; raise RuntimeError on non-zero exit."""
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), text=True, capture_output=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        raise RuntimeError(f"{label} failed (exit {proc.returncode})\n" + "\n".join(tail))
    return proc.returncode


def fetch_papers() -> int:
    """Run the PMC fetch/download stage -> papers/ + download_log.json."""
    return _run([sys.executable, "-m", "fetch_pmc_papers"], label="fetch_pmc_papers")


def reconcile_paper_state() -> ReconciliationResult:
    """Project the download log and filesystem onto the canonical manifest."""
    return reconcile_manifest(
        manifest_path=PAPERS_DIR / "manifest.json",
        papers_dir=PAPERS_DIR,
        download_log_path=DOWNLOAD_LOG,
    )


def _limit_args() -> list[str]:
    """``--limit N`` for a pilot run, sourced from the PIPELINE_LIMIT env var."""
    limit = os.environ.get("PIPELINE_LIMIT", "").strip()
    return ["--limit", limit] if limit else []


def summarize_papers() -> int:
    """Run the summarizer over missing papers (resume-only, chunked recovery)."""
    return _run([sys.executable, "-m", "summarizer.run"] + SUMMARIZE_ARGS + _limit_args(),
                label="summarizer.run")


def scan_data_availability() -> int:
    """Run the focused data-availability scan over downloaded papers."""
    return _run([sys.executable, str(REPO_ROOT / "scan_data_availability.py")] + DATA_AVAILABILITY_ARGS + _limit_args(),
                label="scan_data_availability")


def build_results() -> int:
    """Run build_results.py -> results/ (SUMMARY.md, checklist.md, combined copy)."""
    return _run([sys.executable, str(REPO_ROOT / "build_results.py")],
                label="build_results")


def _summary_cache_is_current(
    summary_path: Path,
    *,
    source_path: Path,
    manifest_record: dict,
    checklist: ManuscriptChecklist,
) -> bool:
    """Validate the complete cache identity before a manifest-driven publish."""
    from summarizer.run import PROCESSING_CHECKSUM, PROMPT_CHECKSUM, SCHEMA_CHECKSUM

    try:
        cache = json.loads(summary_cache_path(summary_path).read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(cache, dict) or cache.get("summarization_mode") not in {
        "single", "chunked", "single-with-chunked-recovery",
    }:
        return False
    expected_title = str(manifest_record.get("title") or checklist.title)
    expected_year = str(manifest_record.get("year") or checklist.year)
    if checklist.title != expected_title or checklist.year != expected_year:
        return False
    return summary_cache_matches(
        summary_path,
        pmcid=checklist.pmcid,
        source_path=source_path,
        source_identity_path=str(manifest_record.get("path") or ""),
        source_checksum=str(manifest_record.get("checksum") or ""),
        model=checklist.model,
        prompt_checksum=PROMPT_CHECKSUM,
        schema_checksum=SCHEMA_CHECKSUM,
        title=expected_title,
        year=expected_year,
        processing_checksum=PROCESSING_CHECKSUM,
        summarization_mode=str(cache["summarization_mode"]),
    )


def rebuild_combined(
    *,
    summary_dir: Path = SUMMARY_DIR,
    out_path: Path = COMBINED_PATH,
    db_path: Path = DB_PATH,
    manifest_path: Path = MANIFEST_PATH,
    papers_dir: Path = PAPERS_DIR,
    included_pmcids: set[str] | None = None,
) -> dict:
    """Rebuild TinyDB and ``SummaryBatch`` from current summaries.

    Every ``<pmcid>.json`` is validated first.  A fresh temporary Store is then
    populated with exactly those records and atomically moved into place, so a
    deleted per-paper summary cannot survive as a stale TinyDB row.  The
    combined artifact is likewise built at a temporary path.  Each final file
    is published atomically with ``os.replace``; the final consistency gate
    detects any cross-file drift if the process is interrupted between them.
    Invalid files are skipped with a warning.
    """
    authoritative_sources: dict[str, Path] | None = None
    authoritative_records: dict[str, dict] = {}
    if included_pmcids is None:
        if Path(manifest_path).is_file():
            authoritative_sources = publication_ready_paper_files(
                manifest_path, papers_dir, validate=True,
            )
            authoritative_records = PaperManifest(manifest_path).records
            included_pmcids = set(authoritative_sources)
        else:
            # Never infer publication authority from whatever JSON happens to
            # be in a directory.  Isolated callers can pass included_pmcids
            # explicitly; production must first create/reconcile the manifest.
            raise FileNotFoundError(
                f"paper manifest is missing: {manifest_path}; run download/reconciliation first"
            )

    records: list[dict] = []
    for f in sorted(summary_dir.glob("*.json")):
        file_pmcid = f.stem.upper()
        if included_pmcids is not None and file_pmcid not in included_pmcids:
            continue
        try:
            rec = json.loads(f.read_text())
            checklist = ManuscriptChecklist(**rec)
            if checklist.pmcid != file_pmcid:
                raise ValueError(
                    f"record PMCID {checklist.pmcid} does not match filename {file_pmcid}"
                )
        except Exception as e:
            if authoritative_sources is not None:
                raise ValueError(
                    f"authoritative summary is invalid for {file_pmcid}: "
                    f"{type(e).__name__}"
                ) from e
            warnings.warn(f"skipping {f.name}: {type(e).__name__}", UserWarning,
                          stacklevel=2)
            continue
        if authoritative_sources is not None and not _summary_cache_is_current(
            f,
            source_path=authoritative_sources[file_pmcid],
            manifest_record=authoritative_records[file_pmcid],
            checklist=checklist,
        ):
            raise ValueError(
                f"summary cache is stale or missing for {file_pmcid}; run make summarize"
            )
        records.append(checklist.model_dump())

    if authoritative_sources is not None:
        built_ids = {str(record["pmcid"]) for record in records}
        missing = sorted(set(authoritative_sources) - built_ids)
        if missing:
            raise ValueError(
                "authoritative summaries are missing for " + ", ".join(missing)
            )

    db_target = Path(db_path)
    out_target = Path(out_path)
    db_target.parent.mkdir(parents=True, exist_ok=True)
    out_target.parent.mkdir(parents=True, exist_ok=True)

    db_fd, db_tmp_name = tempfile.mkstemp(
        prefix=f".{db_target.name}.", suffix=".tmp", dir=str(db_target.parent),
    )
    out_fd, out_tmp_name = tempfile.mkstemp(
        prefix=f".{out_target.name}.", suffix=".tmp", dir=str(out_target.parent),
    )
    os.close(db_fd)
    os.close(out_fd)
    db_tmp = Path(db_tmp_name)
    out_tmp = Path(out_tmp_name)
    # TinyDB expects either no file or valid JSON; mkstemp creates an empty file.
    db_tmp.unlink()
    out_tmp.unlink()
    try:
        with Store(db_tmp) as store:
            store.replace_all(records)
            data = store.export_combined(out_tmp)
        os.replace(db_tmp, db_target)
        os.replace(out_tmp, out_target)
        return data
    finally:
        db_tmp.unlink(missing_ok=True)
        out_tmp.unlink(missing_ok=True)
