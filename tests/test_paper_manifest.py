"""Focused state-integrity tests for the PMCID manifest and summary cache."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import Store
from paper_manifest import (
    PaperManifest,
    PayloadCandidate,
    deduplicate_paper_files,
    discover_included_papers,
    included_paper_files,
    publication_ready_paper_files,
    reconcile_manifest,
    select_first_valid_payload,
    sha256_file,
    summarized_paper_files,
    summary_cache_matches,
    summary_cache_path,
    write_summary_cache_metadata,
)
from summarizer.schema import ManuscriptChecklist


NOW = "2026-08-21T12:34:56Z"


def _pdf() -> bytes:
    return b"%PDF-1.7\n" + (b"full text " * 2_500)


def _xml() -> bytes:
    return (
        "<article article-type='research-article'><front/>"
        f"<body><p>{'full article text ' * 180}</p></body></article>"
    ).encode()


def _summary(pmcid: str) -> ManuscriptChecklist:
    return ManuscriptChecklist(
        pmcid=pmcid,
        title=f"{pmcid} title",
        year="2020",
        ehr_used=False,
        ehr_evidence="research cohort",
        summary="summary",
    )


def _screening(decision: str = "included") -> dict:
    return {
        "decision": decision,
        "scope_classification": "core-exposomics" if decision == "included" else "out-of-scope",
        "eligibility_evidence": ["human primary study"] if decision == "included" else [],
        "exclusion_reasons": [] if decision == "included" else ["not eligible"],
    }


def test_manifest_has_canonical_shape_and_atomic_checksum(tmp_path: Path):
    source = tmp_path / "2020_PMC123_paper.pdf"
    source.write_bytes(_pdf())
    manifest_path = tmp_path / "manifest.json"

    manifest = PaperManifest(manifest_path)
    record = manifest.upsert(
        "123",
        status="downloaded",
        path=source,
        checksum=sha256_file(source),
        query_provenance=["exposom*[Title/Abstract]", "exposom*[Title/Abstract]"],
        timestamp=NOW,
    )
    manifest.save(timestamp=NOW)

    raw = json.loads(manifest_path.read_text())
    assert set(raw) == {"schema_version", "updated_at", "records"}
    assert raw["schema_version"] == 1
    assert set(raw["records"]) == {"PMC123"}
    assert record["checksum"].startswith("sha256:")
    assert record["query_provenance"] == ["exposom*[Title/Abstract]"]
    assert record["timestamps"] == {
        "first_seen_at": NOW,
        "updated_at": NOW,
        "downloaded_at": NOW,
        "validated_at": NOW,
    }


def test_reconcile_log_manifest_and_filesystem(tmp_path: Path):
    papers = tmp_path / "papers"
    papers.mkdir()
    # PMC1 has two valid representations; PDF wins but XML remains untouched.
    pmc1_pdf = papers / "2020_PMC1_primary.pdf"
    pmc1_xml = papers / "2020_PMC1_primary.xml"
    pmc1_pdf.write_bytes(_pdf())
    pmc1_xml.write_bytes(_xml())
    # PMC2 has a fake direct-PDF response and a valid XML fallback.
    pmc2_bad = papers / "2021_PMC2_primary.pdf"
    pmc2_xml = papers / "2021_PMC2_primary.xml"
    pmc2_bad.write_bytes(b"<html>rate limited</html>")
    pmc2_xml.write_bytes(_xml())
    # PMC4 was never logged but is valid and should be registered.
    pmc4_pdf = papers / "2022_PMC4_primary.pdf"
    pmc4_pdf.write_bytes(_pdf())

    query_one = "exposom*[Title/Abstract]"
    query_two = '"exposure-wide association"[Title/Abstract]'
    log = {
        "downloaded": ["1", "2", "3"],
        "search_runs": [{"query_index": 0, "query": query_one, "ids": ["1"]}],
        "papers": [
            {"pmcid": "PMC1", "title": "one", "query_membership": [0],
             "screening": _screening()},
            {"pmcid": "PMC2", "title": "two", "queries": [query_two],
             "screening": _screening()},
            {"pmcid": "PMC3", "title": "three", "queries": [query_one],
             "screening": _screening()},
        ],
        "excluded": [{
            "pmcid": "PMC5",
            "title": "review",
            "reason": "not primary research",
            "query_membership": [{"name": "operational", "query": query_two}],
        }],
    }
    log_path = papers / "download_log.json"
    log_path.write_text(json.dumps(log))

    result = reconcile_manifest(
        manifest_path=papers / "manifest.json",
        papers_dir=papers,
        download_log_path=log_path,
        timestamp=NOW,
    )

    records = json.loads((papers / "manifest.json").read_text())["records"]
    reconciled_log = json.loads(log_path.read_text())
    assert records["PMC1"]["path"] == "papers/2020_PMC1_primary.pdf"
    assert records["PMC1"]["query_provenance"] == [query_one]
    assert records["PMC2"]["path"] == "papers/2021_PMC2_primary.xml"
    assert records["PMC2"]["query_provenance"] == [query_two]
    assert records["PMC3"]["status"] == "missing"
    assert records["PMC3"]["path"] is None
    assert records["PMC3"]["checksum"] is None
    assert records["PMC4"]["status"] == "downloaded"
    assert records["PMC4"]["screening"]["decision"] == "pending"
    assert records["PMC5"]["status"] == "excluded"
    assert records["PMC5"]["query_provenance"] == [query_two]
    assert result.redownload == ("PMC3",)
    assert result.registered == ("PMC4",)
    assert set(result.included_files) == {"PMC1", "PMC2"}
    assert result.duplicates == {"PMC1": (pmc1_xml,)}
    assert pmc2_bad in result.invalid_files
    assert pmc1_xml.exists() and pmc2_bad.exists()  # reconciliation is non-destructive
    assert reconciled_log["downloaded"] == ["PMC1", "PMC2"]
    assert reconciled_log["xml_only"] == ["PMC2"]
    assert reconciled_log["local_files"] == ["PMC1", "PMC2", "PMC4"]
    assert reconciled_log["reconciled_at"] == NOW

    # Removing stale IDs from the legacy list must not forget the outstanding
    # retry on a later resume pass; the manifest now carries that state.
    again = reconcile_manifest(
        manifest_path=papers / "manifest.json",
        papers_dir=papers,
        download_log_path=log_path,
        timestamp="2026-08-21T13:00:00Z",
    )
    assert again.redownload == ("PMC3",)


def test_reconcile_marks_manifest_download_missing(tmp_path: Path):
    papers = tmp_path / "papers"
    papers.mkdir()
    missing = papers / "2020_PMC9_gone.pdf"
    manifest = PaperManifest(papers / "manifest.json")
    manifest.upsert(
        "PMC9", status="downloaded", path=missing,
        checksum="sha256:" + "0" * 64, timestamp=NOW,
        screening=_screening(),
    )
    manifest.save(timestamp=NOW)

    result = reconcile_manifest(
        manifest_path=manifest.path,
        papers_dir=papers,
        download_log_path=papers / "download_log.json",
        timestamp="2026-08-21T13:00:00Z",
    )

    record = PaperManifest(manifest.path).get("PMC9")
    assert record["status"] == "missing"
    assert record["path"] is None and record["checksum"] is None
    assert result.redownload == ("PMC9",)


def test_summarizer_discovery_refuses_included_record_without_full_text(tmp_path: Path):
    import pytest

    papers = tmp_path / "papers"
    papers.mkdir()
    manifest = PaperManifest(papers / "manifest.json")
    manifest.upsert(
        "PMC12",
        status="failed",
        path=None,
        checksum=None,
        screening=_screening(),
        timestamp=NOW,
    )
    manifest.save(timestamp=NOW)

    with pytest.raises(ValueError, match="PMC12=failed"):
        discover_included_papers(manifest.path, papers)


def test_invalid_pdf_falls_through_to_xml_payload():
    chosen = select_first_valid_payload([
        PayloadCandidate("direct-pdf", "pdf", b"<html>not a PDF</html>"),
        PayloadCandidate("europe-pmc-xml", "xml", _xml()),
    ])
    assert chosen is not None
    assert chosen.source == "europe-pmc-xml"


def test_deduplicate_is_one_path_per_pmcid(tmp_path: Path):
    pdf = tmp_path / "2020_PMC5_a.pdf"
    xml = tmp_path / "2020_PMC5_a.xml"
    other = tmp_path / "2020_PMC6_b.xml"
    pdf.write_bytes(_pdf())
    xml.write_bytes(_xml())
    other.write_bytes(_xml())

    result = deduplicate_paper_files([xml, other, pdf], validate=True)
    assert result.selected == {"PMC5": pdf, "PMC6": other}
    assert result.duplicates == {"PMC5": (xml,)}


def test_retained_excluded_and_pending_files_are_not_publishable(tmp_path: Path):
    papers = tmp_path / "papers"
    papers.mkdir()
    for pmcid in ("PMC20", "PMC21", "PMC22"):
        (papers / f"2020_{pmcid}_paper.pdf").write_bytes(_pdf())
    log_path = papers / "download_log.json"
    records = [
        {"pmcid": "PMC20", "title": "included", "screening": _screening("included")},
        {"pmcid": "PMC21", "title": "excluded", "screening": _screening("excluded")},
        {"pmcid": "PMC22", "title": "pending", "screening": _screening("pending")},
    ]
    log_path.write_text(json.dumps({
        "candidates": records,
        "papers": [records[0]],
        "excluded": [records[1]],
        "pending": [records[2]],
    }))

    result = reconcile_manifest(
        manifest_path=papers / "manifest.json",
        papers_dir=papers,
        download_log_path=log_path,
        timestamp=NOW,
    )

    assert set(result.selected_files) == {"PMC20", "PMC21", "PMC22"}
    assert set(result.included_files) == {"PMC20"}
    assert set(included_paper_files(papers / "manifest.json", papers)) == {"PMC20"}
    reconciled = json.loads(log_path.read_text())
    assert reconciled["downloaded"] == ["PMC20"]
    assert reconciled["local_files"] == ["PMC20", "PMC21", "PMC22"]
    assert all((papers / f"2020_{pmcid}_paper.pdf").exists()
               for pmcid in ("PMC20", "PMC21", "PMC22"))


def test_summarized_projection_excludes_downloaded_and_screened_out_sources(
    tmp_path: Path,
):
    papers = tmp_path / "papers"
    papers.mkdir()
    manifest = PaperManifest(papers / "manifest.json")
    for pmcid, status, decision in (
        ("PMC23", "summarized", "included"),
        ("PMC24", "downloaded", "included"),
        ("PMC25", "summarized", "excluded"),
    ):
        source = papers / f"2020_{pmcid}_paper.pdf"
        source.write_bytes(_pdf())
        manifest.upsert(
            pmcid,
            status=status,
            path=source,
            checksum=sha256_file(source),
            screening=_screening(decision),
            timestamp=NOW,
        )
    manifest.save(timestamp=NOW)

    assert set(included_paper_files(manifest.path, papers)) == {"PMC23", "PMC24"}
    assert set(summarized_paper_files(manifest.path, papers)) == {"PMC23"}
    import pytest
    with pytest.raises(ValueError, match="PMC24=downloaded"):
        publication_ready_paper_files(manifest.path, papers)


def test_summary_cache_invalidates_every_identity_component(tmp_path: Path):
    source = tmp_path / "2020_PMC7_source.pdf"
    source.write_bytes(_pdf())
    summary = tmp_path / "summaries" / "PMC7.json"
    summary.parent.mkdir()
    summary.write_text(_summary("PMC7").model_dump_json())
    write_summary_cache_metadata(
        summary,
        pmcid="PMC7",
        source_path=source,
        model="model-a",
        prompt_checksum="sha256:" + "1" * 64,
        schema_checksum="sha256:" + "2" * 64,
        source_identity_path="papers/2020_PMC7_source.pdf",
        title="Canonical title",
        year="2020",
        processing_checksum="sha256:" + "5" * 64,
        summarization_mode="single-with-chunked-recovery",
        timestamp=NOW,
    )

    kwargs = {
        "pmcid": "PMC7",
        "source_path": source,
        "model": "model-a",
        "prompt_checksum": "sha256:" + "1" * 64,
        "schema_checksum": "sha256:" + "2" * 64,
        "source_identity_path": "papers/2020_PMC7_source.pdf",
        "title": "Canonical title",
        "year": "2020",
        "processing_checksum": "sha256:" + "5" * 64,
        "summarization_mode": "single-with-chunked-recovery",
    }
    assert summary_cache_matches(summary, **kwargs)
    assert not summary_cache_matches(summary, **{**kwargs, "model": "model-b"})
    assert not summary_cache_matches(
        summary, **{**kwargs, "prompt_checksum": "sha256:" + "3" * 64}
    )
    assert not summary_cache_matches(
        summary, **{**kwargs, "schema_checksum": "sha256:" + "4" * 64}
    )
    assert not summary_cache_matches(
        summary, **{**kwargs, "source_identity_path": "papers/renamed_PMC7_source.pdf"}
    )
    assert not summary_cache_matches(summary, **{**kwargs, "title": "Corrected title"})
    assert not summary_cache_matches(summary, **{**kwargs, "year": "2021"})
    assert not summary_cache_matches(
        summary, **{**kwargs, "processing_checksum": "sha256:" + "6" * 64}
    )
    assert not summary_cache_matches(
        summary, **{**kwargs, "summarization_mode": "chunked"}
    )
    source.write_bytes(_pdf() + b"changed")
    assert not summary_cache_matches(summary, **kwargs)

    # Syntactically valid, non-object JSON is corrupt cache state, not an
    # exception that should abort the summarization worker.
    summary_cache_path(summary).write_text("[]")
    assert not summary_cache_matches(summary, **kwargs)


def test_process_one_writes_cache_and_source_change_invalidates(tmp_path: Path):
    from summarizer.run import _process_one

    source = tmp_path / "2020_PMC8_source.pdf"
    source.write_bytes(_pdf())
    checklist = _summary("PMC8")
    client = MagicMock()
    with patch("summarizer.run.extract", return_value=("text", "pdf")), \
         patch("summarizer.run.summarize_text", return_value=checklist) as summarize:
        first = _process_one(
            path=source, meta={"PMC8": {"title": "PMC8 title", "year": "2020"}},
            client=client, model="model-a",
            chunked=False, recover=False, summary_dir=tmp_path,
        )
        second = _process_one(
            path=source, meta={"PMC8": {"title": "PMC8 title", "year": "2020"}},
            client=client, model="model-a",
            chunked=False, recover=False, summary_dir=tmp_path,
        )
        source.write_bytes(_pdf() + b"new revision")
        third = _process_one(
            path=source, meta={"PMC8": {"title": "PMC8 title", "year": "2020"}},
            client=client, model="model-a",
            chunked=False, recover=False, summary_dir=tmp_path,
        )

    assert [first.status, second.status, third.status] == ["ok", "skipped", "ok"]
    assert summarize.call_count == 2


def test_rebuild_combined_removes_stale_database_rows(tmp_path: Path):
    import pipeline_ops

    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / "PMC10.json").write_text(_summary("PMC10").model_dump_json())
    stale_path = summaries / "PMC11.json"
    stale_path.write_text(_summary("PMC11").model_dump_json())
    db_path = tmp_path / "db.json"
    combined = tmp_path / "combined.json"

    pipeline_ops.rebuild_combined(
        summary_dir=summaries, out_path=combined, db_path=db_path,
        included_pmcids={"PMC10", "PMC11"},
    )
    stale_path.unlink()
    data = pipeline_ops.rebuild_combined(
        summary_dir=summaries, out_path=combined, db_path=db_path,
        included_pmcids={"PMC10"},
    )

    assert data["n"] == 1
    with Store(db_path) as store:
        assert store.get("PMC10") is not None
        assert store.get("PMC11") is None


def test_rebuild_combined_ignores_retained_stale_summary_without_deleting_it(tmp_path: Path):
    import pipeline_ops

    summaries = tmp_path / "summaries"
    summaries.mkdir()
    included = summaries / "PMC30.json"
    stale = summaries / "PMC31.json"
    included.write_text(_summary("PMC30").model_dump_json())
    stale.write_text(_summary("PMC31").model_dump_json())

    data = pipeline_ops.rebuild_combined(
        summary_dir=summaries,
        out_path=tmp_path / "combined.json",
        db_path=tmp_path / "db.json",
        included_pmcids={"PMC30"},
    )

    assert data["n"] == 1
    assert [row["pmcid"] for row in data["summaries"]] == ["PMC30"]
    assert stale.exists()


def test_rebuild_combined_refuses_incomplete_downloaded_projection(
    tmp_path: Path,
):
    import pipeline_ops
    from summarizer.run import PROCESSING_CHECKSUM, PROMPT_CHECKSUM, SCHEMA_CHECKSUM

    papers = tmp_path / "papers"
    summaries = papers / "summaries"
    summaries.mkdir(parents=True)
    manifest = PaperManifest(papers / "manifest.json")
    for pmcid, status in (("PMC32", "summarized"), ("PMC33", "downloaded")):
        source = papers / f"2020_{pmcid}_paper.pdf"
        source.write_bytes(_pdf())
        manifest.upsert(
            pmcid,
            status=status,
            path=source,
            checksum=sha256_file(source),
            screening=_screening(),
            timestamp=NOW,
        )
        summary_path = summaries / f"{pmcid}.json"
        checklist = _summary(pmcid)
        summary_path.write_text(checklist.model_dump_json())
        if status == "summarized":
            write_summary_cache_metadata(
                summary_path,
                pmcid=pmcid,
                source_path=source,
                source_identity_path=source,
                source_checksum=sha256_file(source),
                model=checklist.model,
                prompt_checksum=PROMPT_CHECKSUM,
                schema_checksum=SCHEMA_CHECKSUM,
                title=checklist.title,
                year=checklist.year,
                processing_checksum=PROCESSING_CHECKSUM,
                summarization_mode="single-with-chunked-recovery",
                timestamp=NOW,
            )
    manifest.save(timestamp=NOW)

    import pytest
    db_path = papers / "db.json"
    combined = papers / "manuscript_summaries.json"
    db_path.write_text('{"sentinel": "database"}\n')
    combined.write_text('{"sentinel": "combined"}\n')
    with pytest.raises(ValueError, match="PMC33=downloaded"):
        pipeline_ops.rebuild_combined(
            summary_dir=summaries,
            out_path=combined,
            db_path=db_path,
            manifest_path=manifest.path,
            papers_dir=papers,
        )

    assert json.loads(combined.read_text()) == {"sentinel": "combined"}
    assert json.loads(db_path.read_text()) == {"sentinel": "database"}
    assert (summaries / "PMC33.json").exists()


def test_manifest_driven_rebuild_refuses_summarized_status_without_current_cache(
    tmp_path: Path,
):
    import pipeline_ops
    import pytest

    papers = tmp_path / "papers"
    summaries = papers / "summaries"
    summaries.mkdir(parents=True)
    source = papers / "2020_PMC34_paper.pdf"
    source.write_bytes(_pdf())
    manifest = PaperManifest(papers / "manifest.json")
    manifest.upsert(
        "PMC34",
        status="summarized",
        path=source,
        checksum=sha256_file(source),
        screening=_screening(),
        timestamp=NOW,
    )
    manifest.save(timestamp=NOW)
    (summaries / "PMC34.json").write_text(_summary("PMC34").model_dump_json())
    combined = papers / "manuscript_summaries.json"
    database = papers / "db.json"
    combined.write_text('{"sentinel": "combined"}\n')
    database.write_text('{"sentinel": "database"}\n')

    with pytest.raises(ValueError, match="cache is stale or missing"):
        pipeline_ops.rebuild_combined(
            summary_dir=summaries,
            out_path=combined,
            db_path=database,
            manifest_path=manifest.path,
            papers_dir=papers,
        )

    assert json.loads(combined.read_text()) == {"sentinel": "combined"}
    assert json.loads(database.read_text()) == {"sentinel": "database"}


def test_manifest_driven_rebuild_refuses_missing_authoritative_summary(tmp_path: Path):
    import pipeline_ops
    import pytest

    papers = tmp_path / "papers"
    summaries = papers / "summaries"
    summaries.mkdir(parents=True)
    source = papers / "2020_PMC35_paper.pdf"
    source.write_bytes(_pdf())
    manifest = PaperManifest(papers / "manifest.json")
    manifest.upsert(
        "PMC35",
        status="summarized",
        path=source,
        checksum=sha256_file(source),
        screening=_screening(),
        timestamp=NOW,
    )
    manifest.save(timestamp=NOW)

    with pytest.raises(ValueError, match="summaries are missing for PMC35"):
        pipeline_ops.rebuild_combined(
            summary_dir=summaries,
            out_path=papers / "manuscript_summaries.json",
            db_path=papers / "db.json",
            manifest_path=manifest.path,
            papers_dir=papers,
        )


def test_manifest_driven_rebuild_rejects_edited_summary_identity(tmp_path: Path):
    import pipeline_ops
    import pytest
    from summarizer.run import PROCESSING_CHECKSUM, PROMPT_CHECKSUM, SCHEMA_CHECKSUM

    papers = tmp_path / "papers"
    summaries = papers / "summaries"
    summaries.mkdir(parents=True)
    source = papers / "2020_PMC36_paper.pdf"
    source.write_bytes(_pdf())
    manifest = PaperManifest(papers / "manifest.json")
    manifest.upsert(
        "PMC36",
        status="summarized",
        path=source,
        checksum=sha256_file(source),
        screening=_screening(),
        timestamp=NOW,
        metadata={"title": "Manifest title", "year": "2020"},
    )
    manifest.save(timestamp=NOW)
    checklist = _summary("PMC36").model_copy(update={"title": "Edited title"})
    summary_path = summaries / "PMC36.json"
    summary_path.write_text(checklist.model_dump_json())
    write_summary_cache_metadata(
        summary_path,
        pmcid="PMC36",
        source_path=source,
        source_identity_path=source,
        source_checksum=sha256_file(source),
        model=checklist.model,
        prompt_checksum=PROMPT_CHECKSUM,
        schema_checksum=SCHEMA_CHECKSUM,
        title="Manifest title",
        year="2020",
        processing_checksum=PROCESSING_CHECKSUM,
        summarization_mode="single-with-chunked-recovery",
        timestamp=NOW,
    )

    with pytest.raises(ValueError, match="cache is stale or missing"):
        pipeline_ops.rebuild_combined(
            summary_dir=summaries,
            out_path=papers / "manuscript_summaries.json",
            db_path=papers / "db.json",
            manifest_path=manifest.path,
            papers_dir=papers,
        )


def test_manifest_summarized_status_tracks_current_cache_set(tmp_path: Path):
    from summarizer.run import _sync_manifest_summary_statuses

    source = tmp_path / "2020_PMC40_source.pdf"
    source.write_bytes(_pdf())
    manifest_path = tmp_path / "manifest.json"
    manifest = PaperManifest(manifest_path)
    manifest.upsert(
        "PMC40",
        status="downloaded",
        path=source,
        checksum=sha256_file(source),
        screening=_screening(),
        timestamp=NOW,
    )
    manifest.save(timestamp=NOW)

    _sync_manifest_summary_statuses(
        {"PMC40": source}, {"PMC40"}, manifest_path=manifest_path,
    )
    assert PaperManifest(manifest_path).get("PMC40")["status"] == "summarized"

    _sync_manifest_summary_statuses(
        {"PMC40": source}, set(), manifest_path=manifest_path,
    )
    assert PaperManifest(manifest_path).get("PMC40")["status"] == "downloaded"
