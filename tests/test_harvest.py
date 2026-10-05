"""Harvest orchestration, driven by a stub E-utilities client (no network)."""

import pytest

from exposome_ehr.harvest import Harvester, check_mesh_terms, rebuild_from_jsonl
from exposome_ehr.eutils import SearchResult
from exposome_ehr.parse import parse_efetch_response


class StubClient:
    """Answers esearch from query maps and efetch from a fixture.

    ``exact`` is consulted first and keyed on the whole query string, because a
    facet query embeds the entire corpus query: substring matching alone would
    make every facet query look like whichever clause it happens to contain.
    """

    def __init__(
        self,
        sample_xml: bytes,
        corpus: list[str],
        hits: dict[str, list[str]] | None = None,
        exact: dict[str, list[str]] | None = None,
    ):
        self.sample_xml = sample_xml
        self.corpus = corpus
        self.hits = hits or {}
        self.exact = exact or {}
        self.request_count = 0
        self.searches: list[str] = []
        self.fetched: list[list[str]] = []

    def _lookup(self, term: str) -> list[str]:
        if term in self.exact:
            return self.exact[term]
        # Longest key first, so a specific term wins over a generic one.
        for needle in sorted(self.hits, key=len, reverse=True):
            if needle in term:
                return self.hits[needle]
        return self.corpus

    def esearch(self, term, retmax=0, use_history=True, db="pubmed"):
        self.request_count += 1
        self.searches.append(term)
        pmids = self._lookup(term)
        return SearchResult(
            term=term,
            count=len(pmids),
            webenv="W1",
            query_key="1",
            pmids=pmids[:retmax] if retmax else pmids,
            translation=term,
        )

    def search_all_pmids(self, term, page_size=10000):
        result = self.esearch(term)
        result.pmids = self._lookup(term)
        return result

    def efetch_pubmed_xml(self, pmids):
        self.request_count += 1
        self.fetched.append(list(pmids))
        return self.sample_xml

    def iter_article_batches(self, pmids, batch_size=200):
        for index, start in enumerate(range(0, len(pmids), batch_size)):
            chunk = pmids[start : start + batch_size]
            yield index, chunk, self.efetch_pubmed_xml(chunk)


@pytest.fixture()
def stub(sample_xml, vocab):
    vaccine = next(
        b["query"] for b in vocab.profile_blocks("core") if b["id"] == "vaccine_as_exposure"
    )
    return StubClient(
        sample_xml,
        corpus=["99900001", "99900002"],
        hits={
            # A facet query that matches only the first record. "black carbon"
            # appears in the particulate matter facet's query and nowhere else
            # (not in the corpus query, which every facet query embeds).
            '"black carbon"[tiab]': ["99900001"],
            # A seed lookup that returns the wrong paper.
            "Understanding racial disparities in childhood asthma": ["99900001"],
        },
        exact={
            # This clause matches a record that is not in the corpus, and one
            # that PubMed lists but never returns a record for.
            vaccine: ["88800001"],
        },
    )


def test_run_stores_articles_and_tags(store, vocab, stub):
    harvester = Harvester(store, vocab, client=stub, profile="core")
    summary = harvester.run()

    assert summary.pmids_retrieved == 2
    assert summary.articles_stored == 2
    assert summary.facets_run == len(vocab.facet_ids)
    assert store.scalar("SELECT COUNT(*) FROM article") == 2


def test_facet_tags_are_intersected_with_the_corpus(store, vocab, stub):
    Harvester(store, vocab, client=stub, profile="core").run()
    hits = {
        row["pmid"]
        for row in store.rows(
            "SELECT pmid FROM article_facet"
            " WHERE facet_id = 'particulate_matter' AND evidence_source = 'pubmed_query'"
        )
    }
    assert hits == {"99900001"}


def test_facet_query_run_records_out_of_corpus_hits(store, vocab, stub):
    Harvester(store, vocab, client=stub, profile="core").run()
    row = store.rows(
        "SELECT esearch_count, pmids_in_corpus FROM facet_query_run"
        " WHERE facet_id = 'particulate_matter'"
    )[0]
    assert row["esearch_count"] == 1
    assert row["pmids_in_corpus"] == 1


def test_mesh_tag_path_is_independent_of_the_query_path(store, vocab, stub):
    Harvester(store, vocab, client=stub, profile="core").run()
    # The fixture's first record is MeSH-indexed with 'Particulate Matter', which
    # the particulate matter facet declares; no facet query was told to return it.
    mesh_hits = store.rows(
        "SELECT pmid, detail FROM article_facet"
        " WHERE facet_id = 'particulate_matter' AND evidence_source = 'mesh_term'"
    )
    assert [row["pmid"] for row in mesh_hits] == ["99900001"]
    assert "Particulate Matter" in mesh_hits[0]["detail"]


def test_query_block_attribution_excludes_out_of_corpus_pmids(store, vocab, stub):
    Harvester(store, vocab, client=stub, profile="core").run()
    row = store.rows(
        "SELECT esearch_count FROM query_block WHERE block_id = 'vaccine_as_exposure'"
    )[0]
    assert row["esearch_count"] == 1
    assert not store.rows(
        "SELECT 1 FROM article_query_block WHERE block_id = 'vaccine_as_exposure'"
    )


def test_pmids_with_no_record_are_excluded_from_tagging(store, vocab, sample_xml):
    """A PMID esearch lists but efetch never returns must not be tagged.

    Tagging it would violate the article_facet foreign key and abort the whole
    harvest, so the corpus used for tagging is what was actually stored.
    """
    stub = StubClient(sample_xml, corpus=["99900001", "99900002", "77700001"])
    summary = Harvester(store, vocab, client=stub, profile="core").run()

    assert summary.pmids_retrieved == 3
    assert summary.articles_stored == 2
    assert summary.corpus_size == 2
    tagged = {row["pmid"] for row in store.rows("SELECT DISTINCT pmid FROM article_facet")}
    assert "77700001" not in tagged
    assert tagged == {"99900001", "99900002"}


def test_limit_truncates_and_is_recorded(store, vocab, stub):
    summary = Harvester(store, vocab, client=stub, profile="core").run(limit=1)
    assert summary.truncated_to == 1
    assert summary.pmids_retrieved == 1


def test_skip_facets_still_applies_mesh_tags(store, vocab, stub):
    summary = Harvester(store, vocab, client=stub, profile="core").run(skip_facets=True)
    assert summary.facets_run == 0
    assert not store.rows(
        "SELECT 1 FROM article_facet WHERE evidence_source = 'pubmed_query'"
    )
    assert store.rows("SELECT 1 FROM article_facet WHERE evidence_source = 'mesh_term'")


def test_seed_resolution_requires_the_expected_title(store, vocab, stub):
    Harvester(store, vocab, client=stub, profile="core").run()
    row = store.rows("SELECT * FROM seed_reference WHERE ref_id = 'correa2022'")[0]
    # The stub returns the fixture record, whose title is not Correa's, so the
    # resolver must refuse it rather than accept the first hit.
    assert row["match_method"] == "ambiguous"
    assert row["resolved_pmid"] is None


def test_seed_resolution_raises_without_a_vocabulary_snapshot(store, vocab, stub):
    harvester = Harvester(store, vocab, client=stub, profile="core")
    with pytest.raises(RuntimeError, match="upsert_vocabulary"):
        harvester.resolve_seed_references()


def test_summary_reports_seed_resolution_counts(store, vocab, stub):
    """Seed resolution outcomes surface in the summary, not only in the table."""
    summary = Harvester(store, vocab, client=stub, profile="core").run()
    # Every seed with a lookup is tallied by its match method.
    assert summary.seed_resolution
    assert sum(summary.seed_resolution.values()) == sum(
        1 for ref in vocab.seed_references.values() if ref.get("pubmed_lookup")
    )
    assert summary.as_dict()["seed_resolution"] == summary.seed_resolution


def test_rebuild_from_jsonl_needs_no_network(store, vocab, sample_xml):
    store.append_jsonl(parse_efetch_response(sample_xml))
    assert rebuild_from_jsonl(store, vocab) == 2
    assert store.scalar("SELECT COUNT(*) FROM article") == 2
    assert store.scalar("SELECT COUNT(*) FROM article_mesh") == 4


def test_check_mesh_terms_flags_an_untranslated_descriptor(store, vocab, sample_xml):
    class MeshStub(StubClient):
        def esearch(self, term, retmax=0, use_history=True, db="pubmed"):
            self.request_count += 1
            # PubMed falls back to free text when a descriptor does not exist;
            # the translation then no longer names the MeSH Terms field.
            good = "Particulate Matter" in term
            return SearchResult(
                term=term,
                count=5,
                webenv=None,
                query_key=None,
                pmids=[],
                translation="x[MeSH Terms]" if good else "x[All Fields]",
            )

    store.upsert_vocabulary(vocab)
    stats = check_mesh_terms(store, vocab, MeshStub(sample_xml, [], {}))
    assert stats["checked"] > 0
    assert stats["invalid"] == stats["checked"] - store.scalar(
        "SELECT COUNT(*) FROM mesh_term_check WHERE is_valid = 1"
    )
    assert store.scalar(
        "SELECT is_valid FROM mesh_term_check WHERE descriptor_name = 'Particulate Matter'"
    ) == 1


def test_run_compacts_the_jsonl_and_reports_the_count(store, vocab, stub):
    summary = Harvester(store, vocab, client=stub, profile="core").run()
    assert summary.jsonl_records == 2
    assert [r["pmid"] for r in store.iter_jsonl()] == ["99900001", "99900002"]


def test_repeated_runs_do_not_grow_the_jsonl(store, vocab, stub):
    harvester = Harvester(store, vocab, client=stub, profile="core")
    harvester.run()
    after_one = store.corpus_text()
    harvester.run()
    assert store.corpus_text() == after_one
