"""Dagster asset pipeline for the all-age exposome / EHR literature review.

A generated-artifact lineage that orchestrates the existing scripts via thin wrappers
(see ``pipeline_ops``):

    download_log  ->  paper_state  ->  per_paper_summaries  ->  data_availability_scan  ->
    manuscript_summaries  ->  results  ->  site
                         |                              |
                         +-> paper_summary ------------+-> artifact_consistency

Run it:

    make dagster                       # `dagster dev -f pipeline.py` (UI + lineage)
    # or materialize the whole graph headlessly:
    make materialize

Each asset materializes by running the corresponding stage; the summarizer runs
in ``--recover`` (resume-only, chunked recovery) mode so re-materializing only
fills in the gaps. No rewrite of the fetcher, summarizer, or build_results.
"""
from __future__ import annotations

import json
from pathlib import Path

from dagster import asset, Definitions, MetadataValue

import artifact_consistency as consistency_checker
import build_site as site_builder
import build_summary as summary_builder
import pipeline_ops

PAPERS_DIR = pipeline_ops.PAPERS_DIR
SUMMARY_DIR = pipeline_ops.SUMMARY_DIR
DOWNLOAD_LOG = pipeline_ops.DOWNLOAD_LOG


def summarize_data_availability_counts(
    summary_dir: Path = SUMMARY_DIR,
    *,
    allowed_pmcids: set[str] | None = None,
) -> dict:
    """Count data-availability categories for explicitly included summaries."""
    counts: dict[str, int] = {}
    parse_failures = 0
    if allowed_pmcids is None and summary_dir.resolve() == SUMMARY_DIR.resolve():
        allowed_pmcids = set(pipeline_ops.publication_ready_paper_files(
            pipeline_ops.MANIFEST_PATH,
            pipeline_ops.PAPERS_DIR,
            validate=True,
        ))
    for f in summary_dir.glob("*.json"):
        if allowed_pmcids is not None and f.stem.upper() not in allowed_pmcids:
            continue
        try:
            rec = json.loads(f.read_text())
        except Exception:
            parse_failures += 1
            continue
        cat = rec.get("data_availability", "not-stated") or "not-stated"
        counts[cat] = counts.get(cat, 0) + 1
    return {"n": sum(counts.values()), "counts": counts, "parse_failures": parse_failures}


@asset
def download_log(context) -> dict:
    """Fetch PMC papers + the download log (fetch_pmc_papers.py)."""
    pipeline_ops.fetch_papers()
    if not DOWNLOAD_LOG.exists():
        data = {"n": 0, "candidates": 0, "excluded": 0, "pending": 0}
    else:
        log = json.loads(DOWNLOAD_LOG.read_text())
        data = {
            "n": len(log.get("papers", [])),
            "candidates": len(log.get("candidates", [])),
            "excluded": len(log.get("excluded", [])),
            "pending": len(log.get("pending", [])),
        }
    context.add_output_metadata({
        "included_papers": data["n"],
        "candidates": data["candidates"],
        "excluded": data["excluded"],
        "pending": data["pending"],
        "path": MetadataValue.path(DOWNLOAD_LOG),
    })
    return data


@asset
def paper_state(context, download_log: dict) -> dict:
    """Reconcile the log, manifest, and validated local full texts."""
    result = pipeline_ops.reconcile_paper_state()
    data = {
        "n": len(result.included_files),
        "physical": len(result.selected_files),
        "candidates": download_log.get("candidates", 0),
        "redownload": len(result.redownload),
        "registered": len(result.registered),
        "duplicates": len(result.duplicates),
        "invalid_files": len(result.invalid_files),
    }
    context.add_output_metadata({
        "local_full_texts": data["n"],
        "validated_physical_files": data["physical"],
        "redownload": data["redownload"],
        "registered": data["registered"],
        "duplicate_representations": data["duplicates"],
        "invalid_files": data["invalid_files"],
        "manifest": MetadataValue.path(pipeline_ops.PAPERS_DIR / "manifest.json"),
    })
    return data


@asset
def paper_summary(context, paper_state: dict) -> dict:
    """Regenerate paper_summary.md from the reconciled download log."""
    data = summary_builder.main()
    data["local_full_texts"] = paper_state.get("n", 0)
    context.add_output_metadata({
        "papers": data["n"],
        "local_full_texts": data["local_full_texts"],
        "path": MetadataValue.path(summary_builder.OUT),
    })
    return data


@asset
def per_paper_summaries(context, paper_state: dict) -> dict:
    """Summarize every missing paper (summarizer.run --recover) -> papers/summaries/."""
    pipeline_ops.summarize_papers()
    included = set(pipeline_ops.publication_ready_paper_files(
        pipeline_ops.MANIFEST_PATH,
        pipeline_ops.PAPERS_DIR,
        validate=True,
    ))
    n = sum((SUMMARY_DIR / f"{pmcid}.json").is_file() for pmcid in included)
    data = {"n": n, "downloaded": paper_state.get("n", 0)}
    context.add_output_metadata({
        "summaries": n,
        "downloaded_candidates": data["downloaded"],
        "path": MetadataValue.path(SUMMARY_DIR),
    })
    return data


@asset
def data_availability_scan(context, per_paper_summaries: dict) -> dict:
    """Update per-paper summaries with data-availability fields."""
    pipeline_ops.scan_data_availability()
    data = summarize_data_availability_counts()
    data["per_paper"] = per_paper_summaries.get("n", 0)
    parse_failures = data["parse_failures"]
    counts = data["counts"]
    context.add_output_metadata({
        "summaries_scanned": data["n"],
        "per_paper_summaries": data["per_paper"],
        "parse_failures": parse_failures,
        "data_availability_counts": MetadataValue.json(counts),
    })
    return data


@asset
def manuscript_summaries(context, data_availability_scan: dict) -> dict:
    """Rebuild the combined SummaryBatch from enriched per-paper files."""
    data = pipeline_ops.rebuild_combined()
    out = {"n": data["n"], "model": data.get("model", ""),
           "per_paper": data_availability_scan.get("per_paper", data_availability_scan.get("n", 0))}
    context.add_output_metadata({
        "summaries": out["n"],
        "model": out["model"],
        "path": MetadataValue.path(pipeline_ops.COMBINED_PATH),
    })
    return out


@asset
def results(context, manuscript_summaries: dict) -> dict:
    """Export readable results/ (SUMMARY.md, checklist.md, combined copy)."""
    pipeline_ops.build_results()
    data = {"n": manuscript_summaries.get("n", 0)}
    context.add_output_metadata({
        "summaries": data["n"],
        "summary_md": MetadataValue.path(pipeline_ops.REPO_ROOT / "results" / "SUMMARY.md"),
        "checklist_md": MetadataValue.path(pipeline_ops.REPO_ROOT / "results" / "checklist.md"),
    })
    return data


@asset
def site(context, results: dict) -> dict:
    """Rebuild the static documentation site from the published combined JSON."""
    data = site_builder.main()
    data["results"] = results.get("n", 0)
    context.add_output_metadata({
        "papers": data["n"],
        "ehr_papers": data["ehr"],
        "model": data["model"],
        "path": MetadataValue.path(site_builder.OUT),
    })
    return data


@asset
def artifact_consistency(context, paper_summary: dict, site: dict) -> dict:
    """Fail materialization if persisted and generated artifact sets drift."""
    counts = consistency_checker.check_artifacts(pipeline_ops.REPO_ROOT)
    data = {
        "n": counts.get("manifest", 0),
        "paper_summary": paper_summary.get("n", 0),
        "site": site.get("n", 0),
        "counts": counts,
    }
    context.add_output_metadata({
        "papers": data["n"],
        "artifact_counts": MetadataValue.json(counts),
    })
    return data


defs = Definitions(assets=[download_log, paper_state, paper_summary, per_paper_summaries,
                           data_availability_scan, manuscript_summaries,
                           results, site, artifact_consistency])
