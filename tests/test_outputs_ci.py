"""Focused regression tests for outputs and the fail-closed CI publication gate."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_results
import build_site
import build_summary
from artifact_consistency import ArtifactConsistencyError, check_artifacts
from fetch_pmc_papers import SEARCH_QUERY_SPECS
from paper_manifest import write_summary_cache_metadata
from summarizer.run import PROCESSING_CHECKSUM, PROMPT_CHECKSUM, SCHEMA_CHECKSUM


REPO_ROOT = Path(__file__).resolve().parent.parent
TIMESTAMP = "2026-08-21T00:00:00Z"
SNAPSHOT_DATE = "2026-08-20"


def _record(pmcid: str, *, model: str = "model-a", year: str = "2020") -> dict:
    return {
        "pmcid": pmcid,
        "title": f"{pmcid} exposome study",
        "year": year,
        "ehr_used": pmcid == "PMC1",
        "ehr_evidence": "linked records" if pmcid == "PMC1" else "not used",
        "summary": f"Summary for {pmcid}",
        "key_findings": [],
        "captured_features": [],
        "pathologies_diseases": [],
        "study_design": "cohort",
        "data_source_type": "linked cohort",
        "population": "all ages",
        "exposure_domain": "mixed exposures",
        "limitations": [],
        "data_availability": "not-stated",
        "data_accession_links": [],
        "data_availability_statement": "",
        "confidence": "medium",
        "source_format": "pdf",
        "model": model,
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2))


def _query_entries() -> list[dict]:
    return [
        {
            **asdict(spec),
            "snapshot_date": SNAPSHOT_DATE,
            "effective_query": _effective_query(spec),
        }
        for spec in SEARCH_QUERY_SPECS
    ]


def _effective_query(spec) -> str:
    return (
        f'({spec.query}) AND ("1900/01/01"[PMC Live Date] : '
        f'"2026/08/20"[PMC Live Date])'
    )


def _screening(pmcid: str, *, decision: str = "included") -> dict:
    included = decision == "included"
    return {
        "pmcid": pmcid,
        "title": f"{pmcid} exposome study",
        "decision": decision,
        "scope_classification": "core-exposomics" if included else "out-of-scope",
        "population_facet": "mixed",
        "human_study": True,
        "primary_study": True,
        "ehr_facet": pmcid == "PMC1",
        "query_provenance": sorted(spec.name for spec in SEARCH_QUERY_SPECS),
        "eligibility_evidence": ["Human primary-study exposure and health-outcome evidence"],
        "exclusion_reasons": [] if included else ["No eligible exposure-outcome design"],
        "screening_method": "automated-metadata-v1",
        "reviewer": "",
        "reviewed_at": "",
    }


def _candidate(record: dict, screening: dict) -> dict:
    return {
        "pmcid": record["pmcid"],
        "title": record["title"],
        "journal": f"Journal {record['pmcid']}",
        "year": record["year"],
        "authors": ["A. Author"],
        "query_membership": _query_entries(),
        "pmid": record["pmcid"].removeprefix("PMC"),
        "metadata_source": "pubmed-efetch",
        "metadata_complete": True,
        "metadata_missing": [],
        "screening": screening,
    }


def _fixture_tree(root: Path) -> None:
    papers = root / "papers"
    summaries = papers / "summaries"
    results = root / "results"
    docs = root / "docs"
    summaries.mkdir(parents=True)
    results.mkdir()
    docs.mkdir()

    records = [_record("PMC1", year="2020"), _record("PMC2", year="2021")]
    screening = {record["pmcid"]: _screening(record["pmcid"]) for record in records}
    candidates = [_candidate(record, screening[record["pmcid"]]) for record in records]
    manifest_records = {}
    for record in records:
        pmcid = record["pmcid"]
        source = papers / f"{record['year']}_{pmcid}_study.pdf"
        # A minimally plausible PDF according to the shared state validator.
        source.write_bytes(b"%PDF-1.7\n" + f"full text for {pmcid}".encode() * 2000)
        checksum = f"sha256:{hashlib.sha256(source.read_bytes()).hexdigest()}"
        manifest_records[pmcid] = {
            "title": record["title"],
            "journal": f"Journal {pmcid}",
            "year": record["year"],
            "authors": ["A. Author"],
            "status": "summarized",
            "publication_eligible": True,
            "path": str(source.relative_to(root)),
            "checksum": checksum,
            "query_provenance": [spec.query for spec in SEARCH_QUERY_SPECS],
            "screening": screening[pmcid],
            "timestamps": {
                "first_seen_at": TIMESTAMP,
                "updated_at": TIMESTAMP,
                "downloaded_at": TIMESTAMP,
                "validated_at": TIMESTAMP,
            },
        }
        summary_path = summaries / f"{pmcid}.json"
        _write_json(summary_path, record)
        write_summary_cache_metadata(
            summary_path,
            pmcid=pmcid,
            source_path=source,
            source_identity_path=source.resolve(),
            source_checksum=checksum,
            model=record["model"],
            prompt_checksum=PROMPT_CHECKSUM,
            schema_checksum=SCHEMA_CHECKSUM,
            title=record["title"],
            year=record["year"],
            processing_checksum=PROCESSING_CHECKSUM,
            summarization_mode="single-with-chunked-recovery",
            timestamp=TIMESTAMP,
        )

    _write_json(papers / "manifest.json", {
        "schema_version": 1,
        "updated_at": TIMESTAMP,
        "records": manifest_records,
    })
    _write_json(papers / "download_log.json", {
        "search_database": "pmc",
        "search_runs": [
            {
                "query_name": spec.name,
                "arm": spec.arm,
                "database": "pmc",
                "query": spec.query,
                "effective_query": _effective_query(spec),
                "snapshot_date": SNAPSHOT_DATE,
                "facets": dict(spec.facets),
                "ncbi_count": 2,
                "retrieved_count": 2,
                "retrieved_at": TIMESTAMP,
                "pages": 1,
                "page_starts": [0],
                "page_counts": [2],
                "query_translation": _effective_query(spec),
                "shards": [{
                    "query": _effective_query(spec),
                    "date_from": "1900-01-01",
                    "date_to": SNAPSHOT_DATE,
                    "count": 2,
                    "retrieved_at": TIMESTAMP,
                    "is_leaf": True,
                    "pages": 1,
                    "page_starts": [0],
                    "page_counts": [2],
                    "query_translation": _effective_query(spec),
                }],
            }
            for spec in SEARCH_QUERY_SPECS
        ],
        "query_membership": {
            record["pmcid"]: _query_entries() for record in records
        },
        "candidates": candidates,
        "screening": {
            "included": [screening[record["pmcid"]] for record in records],
            "excluded": [],
            "pending": [],
        },
        "downloaded": ["1", "2"],
        "xml_only": [],
        "excluded": [],
        "pending": [],
        "failed": [],
        "abstract_only": [],
        "papers": candidates,
        "screening_overrides_file": "papers/screening_overrides.json",
        "screening_override_count": 0,
    })
    _write_json(papers / "db.json", {"_default": {"1": records[0], "2": records[1]}})
    combined = {"n": 2, "model": "model-a", "summaries": records}
    _write_json(papers / "manuscript_summaries.json", combined)

    build_summary.main(
        log_path=papers / "download_log.json", out_path=root / "paper_summary.md"
    )
    build_results.main(combined=papers / "manuscript_summaries.json", out_dir=results)
    build_site.main(combined=results / "manuscript_summaries.json", out=docs / "index.html")


def test_consistency_checker_accepts_reconciled_artifacts(tmp_path: Path):
    _fixture_tree(tmp_path)

    counts = check_artifacts(tmp_path)

    assert counts["manifest"] == 2
    assert set(counts.values()) == {2}


def test_consistency_checker_ignores_retained_invalid_download_response(tmp_path: Path):
    _fixture_tree(tmp_path)
    # Reconciliation preserves bad responses for inspection but they cannot
    # enter the explicit included-study publication projection.
    (tmp_path / "papers" / "2022_PMC999_bad.pdf").write_text("upstream HTML error")

    counts = check_artifacts(tmp_path)

    assert counts["source_files"] == 2


def test_consistency_checker_excludes_retained_screened_out_full_text(tmp_path: Path):
    _fixture_tree(tmp_path)
    papers = tmp_path / "papers"
    source = papers / "2022_PMC9_retained.pdf"
    source.write_bytes(b"%PDF-1.7\n" + b"retained excluded full text" * 2000)
    checksum = f"sha256:{hashlib.sha256(source.read_bytes()).hexdigest()}"
    excluded_screening = _screening("PMC9", decision="excluded")
    excluded_record = _record("PMC9", year="2022")
    excluded_candidate = _candidate(excluded_record, excluded_screening)

    manifest_path = papers / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["records"]["PMC9"] = {
        "title": excluded_candidate["title"],
        "journal": excluded_candidate["journal"],
        "year": excluded_candidate["year"],
        "authors": excluded_candidate["authors"],
        "status": "downloaded",
        "publication_eligible": False,
        "path": str(source.relative_to(tmp_path)),
        "checksum": checksum,
        "query_provenance": [spec.query for spec in SEARCH_QUERY_SPECS],
        "screening": excluded_screening,
        "timestamps": {"downloaded_at": TIMESTAMP, "updated_at": TIMESTAMP},
    }
    _write_json(manifest_path, manifest)
    _write_json(papers / "summaries" / "PMC9.json", excluded_record)

    log_path = papers / "download_log.json"
    log = json.loads(log_path.read_text())
    log["query_membership"]["PMC9"] = _query_entries()
    log["candidates"].append(excluded_candidate)
    log["screening"]["excluded"].append(excluded_screening)
    log["excluded"].append(excluded_screening)
    for run in log["search_runs"]:
        run["ncbi_count"] = run["retrieved_count"] = 3
        run["page_counts"] = [3]
        run["shards"][0]["count"] = 3
        run["shards"][0]["page_counts"] = [3]
    _write_json(log_path, log)
    build_summary.main(log_path=log_path, out_path=tmp_path / "paper_summary.md")

    counts = check_artifacts(tmp_path)

    assert set(counts.values()) == {2}
    db_rows = json.loads((papers / "db.json").read_text())["_default"].values()
    combined_rows = json.loads((papers / "manuscript_summaries.json").read_text())["summaries"]
    assert "PMC9" not in {row["pmcid"] for row in db_rows}
    assert "PMC9" not in {row["pmcid"] for row in combined_rows}


@pytest.mark.parametrize(
    ("artifact", "expected"),
    [
        ("manifest", "manifest: missing candidate record PMC2"),
        ("files", "recorded path does not exist"),
        ("summaries", "per_paper_summaries: 1 PMCIDs != manifest 2"),
        ("db", "db: 1 PMCIDs != manifest 2"),
        ("combined", "combined: 1 PMCIDs != manifest 2"),
        ("markdown", "paper_summary.md: declares 1, contains 2"),
        ("site", "site: declares 1, embeds 2"),
    ],
)
def test_consistency_checker_fails_each_drift_class(
    tmp_path: Path, artifact: str, expected: str
):
    _fixture_tree(tmp_path)
    if artifact == "manifest":
        path = tmp_path / "papers" / "manifest.json"
        data = json.loads(path.read_text())
        data["records"].pop("PMC2")
        _write_json(path, data)
    elif artifact == "files":
        (tmp_path / "papers" / "2021_PMC2_study.pdf").unlink()
    elif artifact == "summaries":
        (tmp_path / "papers" / "summaries" / "PMC2.json").unlink()
    elif artifact == "db":
        path = tmp_path / "papers" / "db.json"
        data = json.loads(path.read_text())
        data["_default"].pop("2")
        _write_json(path, data)
    elif artifact == "combined":
        path = tmp_path / "papers" / "manuscript_summaries.json"
        data = json.loads(path.read_text())
        data["summaries"].pop()
        data["n"] = 1
        _write_json(path, data)
    elif artifact == "markdown":
        path = tmp_path / "paper_summary.md"
        path.write_text(path.read_text().replace("artifact-count: 2", "artifact-count: 1"))
    elif artifact == "site":
        path = tmp_path / "docs" / "index.html"
        path.write_text(path.read_text().replace(
            'name="exposome-record-count" content="2"',
            'name="exposome-record-count" content="1"',
        ))

    with pytest.raises(ArtifactConsistencyError, match=expected):
        check_artifacts(tmp_path)


def test_consistency_checker_audits_complete_query_pages_and_membership(tmp_path: Path):
    _fixture_tree(tmp_path)
    path = tmp_path / "papers" / "download_log.json"
    log = json.loads(path.read_text())
    log["search_runs"][0]["page_counts"] = [1]
    _write_json(path, log)

    with pytest.raises(ArtifactConsistencyError, match="page totals do not match"):
        check_artifacts(tmp_path)


def test_consistency_checker_rejects_unsupported_inclusion_evidence(tmp_path: Path):
    _fixture_tree(tmp_path)
    log_path = tmp_path / "papers" / "download_log.json"
    log = json.loads(log_path.read_text())
    log["candidates"][0]["screening"]["eligibility_evidence"] = []
    log["screening"]["included"][0]["eligibility_evidence"] = []
    _write_json(log_path, log)
    manifest_path = tmp_path / "papers" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["records"]["PMC1"]["screening"]["eligibility_evidence"] = []
    _write_json(manifest_path, manifest)

    with pytest.raises(ArtifactConsistencyError, match="included study needs eligibility evidence"):
        check_artifacts(tmp_path)


def test_consistency_checker_requires_current_cache_identity(tmp_path: Path):
    _fixture_tree(tmp_path)
    cache = tmp_path / "papers" / "summaries" / ".cache" / "PMC1.json"
    data = json.loads(cache.read_text())
    data["processing_checksum"] = "sha256:" + "0" * 64
    _write_json(cache, data)

    with pytest.raises(ArtifactConsistencyError, match="processing_checksum"):
        check_artifacts(tmp_path)


def test_consistency_checker_audits_redundant_publication_eligibility(tmp_path: Path):
    _fixture_tree(tmp_path)
    manifest_path = tmp_path / "papers" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["records"]["PMC1"]["publication_eligible"] = False
    _write_json(manifest_path, manifest)

    with pytest.raises(ArtifactConsistencyError, match="publication_eligible"):
        check_artifacts(tmp_path)


def test_consistency_checker_validates_record_payloads_and_exact_output_content(tmp_path: Path):
    _fixture_tree(tmp_path)
    summary = tmp_path / "papers" / "summaries" / "PMC1.json"
    data = json.loads(summary.read_text())
    data["ehr_used"] = "definitely"
    _write_json(summary, data)

    with pytest.raises(ArtifactConsistencyError, match="ManuscriptChecklist validation"):
        check_artifacts(tmp_path)

    _fixture_tree(tmp_path / "content")
    report = tmp_path / "content" / "results" / "SUMMARY.md"
    report.write_text(report.read_text().replace("model-a", "wrong-model", 1))
    with pytest.raises(ArtifactConsistencyError, match="content differs from a deterministic rebuild"):
        check_artifacts(tmp_path / "content")


def test_generated_outputs_derive_mixed_model_provenance_from_records(tmp_path: Path):
    records = [
        _record("PMC1", model="model-new"),
        _record("PMC2", model="model-new"),
        _record("PMC3", model="model-legacy"),
    ]
    combined = tmp_path / "combined.json"
    _write_json(combined, {"n": 3, "model": "stale-batch-label", "summaries": records})

    result_stats = build_results.main(combined=combined, out_dir=tmp_path / "results")
    site_stats = build_site.main(
        combined=tmp_path / "results" / "manuscript_summaries.json",
        out=tmp_path / "docs" / "index.html",
    )

    expected = "model-new (2), model-legacy (1)"
    assert result_stats["model"] == expected
    assert site_stats["model"] == expected
    assert expected in (tmp_path / "results" / "SUMMARY.md").read_text()
    assert expected in (tmp_path / "docs" / "index.html").read_text()
    assert "Gemma 4" not in (tmp_path / "results" / "SUMMARY.md").read_text()


def test_make_targets_are_resumable_and_rebuild_exact_publication_state():
    download = subprocess.run(
        ["make", "-n", "download"], cwd=REPO_ROOT, text=True, capture_output=True, check=True
    ).stdout
    summarize = subprocess.run(
        ["make", "-n", "summarize"], cwd=REPO_ROOT, text=True, capture_output=True, check=True
    ).stdout
    results = subprocess.run(
        ["make", "-n", "results"], cwd=REPO_ROOT, text=True, capture_output=True, check=True
    ).stdout
    db_import = subprocess.run(
        ["make", "-n", "db-import"], cwd=REPO_ROOT, text=True, capture_output=True, check=True
    ).stdout
    db_export = subprocess.run(
        ["make", "-n", "db-export"], cwd=REPO_ROOT, text=True, capture_output=True, check=True
    ).stdout
    scan = subprocess.run(
        ["make", "-n", "scan"], cwd=REPO_ROOT, text=True, capture_output=True, check=True
    ).stdout

    assert "fetch_pmc_papers.py" in download
    assert "-m summarizer.run --recover" in summarize
    assert "rebuild_combined" in results
    assert results.index("rebuild_combined") < results.index("build_results.py")
    assert "rebuild_combined" in db_import and "db.py import" not in db_import
    assert "rebuild_combined" in db_export and "db.py export" in db_export
    assert "scan_data_availability.py" in scan


def test_workflow_keeps_automatic_ci_read_only_and_rejects_pilot_publication():
    workflow = (REPO_ROOT / ".github" / "workflows" / "pipeline.yml").read_text()

    # Push / pull-request testing lives in test.yml; pipeline.yml is dispatch-only
    # publication, read-only at the top level with write scoped to the publish job.
    assert "push:" not in workflow and "pull_request:" not in workflow
    assert "workflow_dispatch:" in workflow
    assert "contents: read" in workflow
    assert "contents: write" in workflow
    assert "needs: tests" in workflow
    assert "Refusing to publish a pilot subset" in workflow
    assert 'PIPELINE_LIMIT: ""' in workflow

    # The LLM endpoint is tailnet-internal: the publish job must join the tailnet,
    # and must not gate on the retired GEMMA_* secret (the key is optional now).
    assert "tailscale/github-action" in workflow
    assert "EXPOSOME_LLM_BASE_URL" in workflow
    assert "GEMMA" not in workflow

    ci = (REPO_ROOT / ".github" / "workflows" / "test.yml").read_text()
    assert "pull_request:" in ci and "tailscale/github-action" in ci
