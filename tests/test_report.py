"""Report generation and derived exports, against a small in-memory corpus."""

import csv

from exposome_ehr.parse import parse_efetch_response
from exposome_ehr.report import build_report, export_derived, run_checks


def _seed_store(store, vocab, sample_xml):
    store.upsert_vocabulary(vocab)
    store.start_run("run_test", "core", "the query", "0.1.0")
    store.upsert_articles(parse_efetch_response(sample_xml), "run_test")
    store.finish_run("run_test", esearch_count=2, pmids_retrieved=2, articles_stored=2)
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO article_facet(pmid, facet_id, evidence_source) "
            "VALUES ('99900001','particulate_matter','pubmed_query')"
        )
        conn.execute(
            "INSERT INTO facet_concept(facet_id, vocabulary_id, search_term, concept_code,"
            " concept_name, match_rank, label_match, resolver) VALUES"
            " ('particulate_matter','SNOMED','exposure to particulate matter','000000001',"
            " 'Exposure to particulate matter (event)', 1, 'exact', 'ols4')"
        )
    return store


def test_report_renders_the_expected_sections(store, vocab, sample_xml):
    _seed_store(store, vocab, sample_xml)
    text = build_report(store, vocab)
    for heading in (
        "# Exposome / EHR PubMed harvest report",
        "## Checks",
        "## Corpus query clause contribution",
        "## Corpus composition",
        "## Facet coverage",
        "## Age strata",
        "## Most co-tagged exposure-outcome pairs",
        "## Ontology graph",
        "## OMOP concept resolution",
    ):
        assert heading in text


def test_report_states_corpus_size_and_composition(store, vocab, sample_xml):
    _seed_store(store, vocab, sample_xml)
    text = build_report(store, vocab)
    assert "| articles | 2 | 100.0% |" in text
    assert "run_test" in text


def test_checks_flag_a_missing_seed_and_an_unrun_mesh_check(store, vocab, sample_xml):
    _seed_store(store, vocab, sample_xml)
    checks = {c.name: c for c in run_checks(store, vocab)}
    assert checks["corpus is non-empty"].passed
    assert not checks["seed correa2022 is in the corpus"].passed
    assert not checks["every declared MeSH descriptor is a real NLM descriptor"].passed
    assert "check-mesh" in checks[
        "every declared MeSH descriptor is a real NLM descriptor"
    ].detail


def test_checks_pass_on_the_graph_and_the_vocabulary(store, vocab, sample_xml):
    _seed_store(store, vocab, sample_xml)
    checks = {c.name: c for c in run_checks(store, vocab)}
    assert checks["ontology graph has no dangling edges"].passed
    assert checks["no node is its own ancestor"].passed
    assert checks["curated vocabulary is internally consistent"].passed


def test_truncated_run_does_not_fail_the_completeness_check(store, vocab, sample_xml):
    """A --limit run records truncated_to in notes; the check must not FAIL on it."""
    import json

    _seed_store(store, vocab, sample_xml)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE harvest_run SET esearch_count = 5000, pmids_retrieved = 2, notes = ?"
            " WHERE run_id = 'run_test'",
            (json.dumps({"truncated_to": 2}),),
        )
    checks = {c.name: c for c in run_checks(store, vocab)}
    check = checks["every PMID esearch reported was retrieved"]
    assert check.passed
    assert "truncated" in check.detail


def test_unexpectedly_short_run_still_fails(store, vocab, sample_xml):
    """A non-truncated run that retrieved fewer than reported must still FAIL."""
    _seed_store(store, vocab, sample_xml)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE harvest_run SET esearch_count = 5000, pmids_retrieved = 2, notes = '{}'"
            " WHERE run_id = 'run_test'"
        )
    checks = {c.name: c for c in run_checks(store, vocab)}
    assert not checks["every PMID esearch reported was retrieved"].passed


def test_preprint_seed_absence_is_a_pass_not_a_failure(store, vocab, sample_xml):
    """A seed PubMed cannot index (a medRxiv preprint) is expected to be absent."""
    import copy

    from exposome_ehr.config import Vocabulary

    facets = copy.deepcopy(vocab.facets)
    facets["seed_references"]["preprint2025"] = {
        "citation": "A preprint.", "pubmed_lookup": "a preprint[Title]",
        "note": "medRxiv preprint; PubMed does not index medRxiv.",
    }
    with_preprint = Vocabulary(query=vocab.query, facets=facets, relations=vocab.relations)
    _seed_store(store, with_preprint, sample_xml)
    checks = {c.name: c for c in run_checks(store, with_preprint)}
    check = checks["seed preprint2025 (preprint, expected absent)"]
    assert check.passed
    assert "expected" in check.detail


def test_exports_are_written_with_headers(store, vocab, sample_xml):
    _seed_store(store, vocab, sample_xml)
    paths = export_derived(store, vocab)
    names = {p.name for p in paths}
    assert {
        "facet_summary.tsv",
        "ontology_nodes.tsv",
        "ontology_edges.tsv",
        "ontology_closure.tsv",
        "facet_concepts.tsv",
        "exposure_outcome_cooccurrence.tsv",
        "age_strata.tsv",
        "corpus_by_year.tsv",
    } <= names
    for path in paths:
        with path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh, delimiter="\t"))
        assert rows and rows[0], f"{path.name} has no header"


def test_ontology_edge_export_carries_predicates_and_provenance(store, vocab, sample_xml):
    _seed_store(store, vocab, sample_xml)
    export_derived(store, vocab)
    path = store.derived_dir / "ontology_edges.tsv"
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    predicates = {row["predicate"] for row in rows}
    assert predicates == {"is_a"}  # literature links arrive with the first judged run
    assert all(row["source"] == "curated" for row in rows)
    assert any(row["provenance"] for row in rows)


def test_closure_export_walks_the_taxonomy(store, vocab, sample_xml):
    _seed_store(store, vocab, sample_xml)
    export_derived(store, vocab)
    path = store.derived_dir / "ontology_closure.tsv"
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    is_a = {(r["descendant_id"], r["ancestor_id"]) for r in rows if r["predicate"] == "is_a"}
    assert ("ehr", "routinely_collected_data") in is_a
    assert ("ehr", "exposome_axis") in is_a
    assert ("research_cohort", "routinely_collected_data") not in is_a
