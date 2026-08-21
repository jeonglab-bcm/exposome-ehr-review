"""Focused contract tests for complete, reproducible PMC discovery."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

import fetch_pmc_papers as fetcher


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.content = content
        self.text = content.decode("utf-8", errors="replace")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected request")
        response = self.responses.pop(0)
        if isinstance(response, requests.RequestException):
            raise response
        return response


TRANSLATION = (
    '"exposom*"[Title/Abstract] AND '
    '1900/01/01:2026/08/20[PMC Live Date]'
)


def history_search(count, *, key="1", webenv="WEBENV", translation=TRANSLATION,
                   warninglist=None):
    result = {
        "count": str(count),
        "querykey": key,
        "webenv": webenv,
        "idlist": [],
        "querytranslation": translation,
    }
    if warninglist is not None:
        result["warninglist"] = warninglist
    return FakeResponse(payload={
        "esearchresult": result,
    })


def history_page(ids):
    return FakeResponse(payload={
        "result": {
            "uids": list(ids),
            **{str(uid): {"uid": str(uid), "title": f"Paper {uid}"} for uid in ids},
        }
    })


def test_query_registry_uses_only_supported_pmc_fields_and_named_arms():
    assert len(fetcher.SEARCH_QUERY_SPECS) == len(fetcher.SEARCH_QUERIES)
    assert len({spec.name for spec in fetcher.SEARCH_QUERY_SPECS}) == len(
        fetcher.SEARCH_QUERY_SPECS
    )
    for spec in fetcher.SEARCH_QUERY_SPECS:
        fetcher.validate_pmc_query(spec.query)
        assert "[Publication Type]" not in spec.query

    core = [spec for spec in fetcher.SEARCH_QUERY_SPECS if spec.arm == "core"]
    assert core
    assert all(spec.facets["population_scope"] == "all_age" for spec in core)
    assert "Exposome\"[MeSH Terms]" in core[0].query
    assert "exposom*[Title/Abstract]" in core[0].query
    assert "exposure-wide association" in core[0].query

    vaccines = [
        spec for spec in fetcher.SEARCH_QUERY_SPECS
        if spec.arm == "operational_vaccine"
    ]
    assert vaccines
    assert all(spec.facets["population_scope"] == "all_age" for spec in vaccines)
    combined = " ".join(spec.query for spec in vaccines)
    assert "immunogenicity[Title/Abstract]" in combined
    assert "immune response" in combined
    assert "biomarker*[Title/Abstract]" in combined
    assert "infection[Title/Abstract]" in combined
    assert "NOT (uptake[Title/Abstract]" not in combined


def test_publication_type_field_is_rejected_before_network_access():
    with pytest.raises(ValueError, match="publication type"):
        fetcher.validate_pmc_query("NOT Review[Publication Type]")


def test_all_age_core_recall_regression_registry_is_explicit():
    assert fetcher.CORE_RECALL_REGRESSION_PMCS == {
        "PMC9678903",
        "PMC6144482",
        "PMC11117089",
        "PMC10099694",
        "PMC13099396",
    }


def test_search_pmc_retrieves_and_reconciles_every_page_with_identity_params():
    session = FakeSession([
        history_search(5),
        history_page(["1", "2"]),
        history_page(["3", "4"]),
        history_page(["5"]),
    ])

    result = fetcher.search_pmc(
        "exposom*[Title/Abstract]",
        page_size=2,
        tool="review_tool",
        email="owner@example.org",
        api_key="secret-key",
        session=session,
        sleep=lambda _: None,
        timestamp=lambda: "2026-08-21T12:34:56Z",
        snapshot_date="2026-08-20",
    )

    assert result.count == 5
    assert result.ids == ("1", "2", "3", "4", "5")
    assert result.pages == 3
    assert result.page_counts == (2, 2, 1)
    assert result.retrieved_at == "2026-08-21T12:34:56Z"
    assert result.query_translation == TRANSLATION
    assert result.snapshot_date == "2026-08-20"
    assert result.page_starts == (0, 2, 4)
    assert session.calls[0][1]["params"]["usehistory"] == "y"
    assert session.calls[0][1]["params"]["retmax"] == 0
    assert [call[1]["params"]["retstart"] for call in session.calls[1:]] == [0, 2, 4]
    for _, kwargs in session.calls:
        assert kwargs["params"]["tool"] == "review_tool"
        assert kwargs["params"]["email"] == "owner@example.org"
        assert kwargs["params"]["api_key"] == "secret-key"


def test_retry_honors_retry_after_then_uses_exponential_backoff():
    session = FakeSession([
        FakeResponse(429, headers={"Retry-After": "3"}),
        FakeResponse(503),
        FakeResponse(200, payload={"ok": True}),
    ])
    sleeps = []

    response = fetcher._request_with_retry(
        "https://example.test",
        params={},
        timeout=1,
        session=session,
        max_retries=2,
        backoff_seconds=0.5,
        sleep=sleeps.append,
    )

    assert response.status_code == 200
    assert sleeps == [3.0, 1.0]


def test_retry_after_http_date_is_supported():
    now = lambda: datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    assert fetcher._retry_after_seconds(
        "Fri, 21 Aug 2026 12:00:07 GMT", now=now,
    ) == 7.0


def test_permanent_page_failure_raises_instead_of_returning_partial_ids():
    session = FakeSession([
        history_search(3),
        FakeResponse(503),
        FakeResponse(503),
    ])

    with pytest.raises(fetcher.NCBIRetrievalError, match="after 2 attempts"):
        fetcher.search_pmc(
            "exposom*[Title/Abstract]",
            page_size=2,
            email="test@example.org",
            session=session,
            max_retries=1,
            backoff_seconds=0,
            sleep=lambda _: None,
            snapshot_date="2026-08-20",
        )
    assert len(session.calls) == 3


def test_short_history_page_and_shard_count_drift_both_fail_closed():
    short = FakeSession([history_search(3), history_page(["1"])])
    with pytest.raises(fetcher.NCBIRetrievalError, match="Incomplete"):
        fetcher.search_pmc(
            "exposom*[Title/Abstract]", page_size=2,
            email="test@example.org", session=short,
            sleep=lambda _: None,
            snapshot_date="2026-08-20",
        )

    drift = FakeSession([
        history_search(3, key="root", webenv="root"),
        history_search(1, key="left", webenv="left"),
        history_page(["1"]),
        history_search(1, key="right", webenv="right"),
        history_page(["2"]),
    ])
    with pytest.raises(fetcher.NCBIRetrievalError, match="do not reconcile"):
        fetcher.search_pmc(
            "exposom*[Title/Abstract]", page_size=2,
            email="test@example.org", session=drift,
            sleep=lambda _: None,
            snapshot_date="2026-08-20",
            max_history_ids=2,
        )


def test_more_than_9999_hits_are_date_sharded_and_history_paged_completely():
    left_ids = [str(value) for value in range(1, 5001)]
    right_ids = [str(value) for value in range(5001, 10002)]

    def pages(ids, size=500):
        return [history_page(ids[start:start + size]) for start in range(0, len(ids), size)]

    session = FakeSession([
        history_search(10001, key="root", webenv="root"),
        history_search(5000, key="left", webenv="left"),
        *pages(left_ids),
        history_search(5001, key="right", webenv="right"),
        *pages(right_ids),
    ])

    result = fetcher.search_pmc(
        "exposom*[Title/Abstract]",
        page_size=500,
        email="test@example.org",
        session=session,
        sleep=lambda _: None,
        snapshot_date="2026-08-20",
    )

    assert result.count == len(result.ids) == 10001
    assert len(set(result.ids)) == 10001
    assert len(result.shards) == 3
    assert result.shards[0].is_leaf is False
    assert [shard.count for shard in result.shards[1:]] == [5000, 5001]
    assert result.pages == 21
    history_calls = [
        kwargs["params"] for url, kwargs in session.calls if url.endswith("esummary.fcgi")
    ]
    assert history_calls
    assert max(params["retstart"] for params in history_calls) < 9999
    assert all("query_key" in params and "WebEnv" in params for params in history_calls)


def test_search_warnings_and_silent_all_fields_translation_fail_closed():
    warned = FakeSession([
        history_search(
            1,
            warninglist={"quotedphrasesnotfound": ["missing phrase"]},
        )
    ])
    with pytest.raises(fetcher.NCBIRetrievalError, match="warning"):
        fetcher.search_pmc(
            "exposom*[Title/Abstract]",
            email="test@example.org",
            session=warned,
            sleep=lambda _: None,
            snapshot_date="2026-08-20",
        )

    remapped = FakeSession([
        history_search(
            1,
            translation=(
                '"exposom*"[All Fields] AND '
                '1900/01/01:2026/08/20[PMC Live Date]'
            ),
        )
    ])
    with pytest.raises(fetcher.NCBIRetrievalError, match="dropped field|All Fields"):
        fetcher.search_pmc(
            "exposom*[Title/Abstract]",
            email="test@example.org",
            session=remapped,
            sleep=lambda _: None,
            snapshot_date="2026-08-20",
        )


def test_esummary_is_identified_and_fails_if_any_uid_is_missing():
    session = FakeSession([FakeResponse(payload={
        "result": {"uids": ["1"], "1": {"title": "paper"}}
    })])
    result = fetcher.fetch_summaries(
        ["1"], tool="tool", email="mail@example.org", api_key=None,
        session=session, sleep=lambda _: None,
    )
    assert result["1"]["title"] == "paper"
    params = session.calls[0][1]["params"]
    assert params["tool"] == "tool"
    assert params["email"] == "mail@example.org"
    assert "api_key" not in params

    incomplete = FakeSession([FakeResponse(payload={"result": {"uids": ["1"]}})])
    with pytest.raises(fetcher.NCBIRetrievalError, match="every requested"):
        fetcher.fetch_summaries(
            ["1", "2"], email="test@example.org", session=incomplete,
            sleep=lambda _: None,
        )


def test_pubmed_metadata_enrichment_supplies_abstract_types_and_mesh_strictly():
    xml = b"""<?xml version="1.0"?>
    <PubmedArticleSet>
      <PubmedArticle><MedlineCitation><PMID>11</PMID><Article>
        <Abstract><AbstractText Label="METHODS">Prospective adult cohort.</AbstractText></Abstract>
        <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
        <Language>eng</Language>
      </Article><MeshHeadingList>
        <MeshHeading><DescriptorName>Humans</DescriptorName></MeshHeading>
      </MeshHeadingList></MedlineCitation></PubmedArticle>
      <PubmedArticle><MedlineCitation><PMID>22</PMID><Article>
        <Abstract><AbstractText>Randomized pediatric vaccine trial.</AbstractText></Abstract>
        <PublicationTypeList><PublicationType>Randomized Controlled Trial</PublicationType></PublicationTypeList>
      </Article><MeshHeadingList>
        <MeshHeading><DescriptorName>Humans</DescriptorName></MeshHeading>
      </MeshHeadingList></MedlineCitation></PubmedArticle>
    </PubmedArticleSet>"""
    session = FakeSession([FakeResponse(content=xml)])
    records = fetcher.fetch_pubmed_metadata(
        ["11", "22"],
        tool="tool",
        email="mail@example.org",
        session=session,
        sleep=lambda _: None,
    )
    assert records["11"]["abstract"] == "METHODS: Prospective adult cohort."
    assert records["11"]["pubtype"] == ["Journal Article"]
    assert records["11"]["mesh_terms"] == ["Humans"]
    assert records["22"]["metadata_complete"] is True

    incomplete = FakeSession([FakeResponse(content=xml.replace(
        b"<PMID>22</PMID>", b"<PMID>23</PMID>",
    ))])
    with pytest.raises(fetcher.NCBIRetrievalError, match="every requested PMID"):
        fetcher.fetch_pubmed_metadata(
            ["11", "22"],
            email="mail@example.org",
            session=incomplete,
            sleep=lambda _: None,
        )


def test_pmc_to_pubmed_enrichment_marks_unresolved_pmid_pending_metadata():
    pmc = {
        "uids": ["1", "2"],
        "1": {"title": "Paper one", "articleids": [{"idtype": "pmid", "value": "11"}]},
        "2": {"title": "Paper two", "articleids": []},
    }
    xml = b"""<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>11</PMID>
      <Article><Abstract><AbstractText>Human cohort.</AbstractText></Abstract>
      <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
      </Article><MeshHeadingList><MeshHeading><DescriptorName>Humans</DescriptorName>
      </MeshHeading></MeshHeadingList></MedlineCitation></PubmedArticle></PubmedArticleSet>"""
    enriched = fetcher.enrich_pmc_summaries(
        pmc,
        email="mail@example.org",
        session=FakeSession([FakeResponse(content=xml)]),
        sleep=lambda _: None,
    )
    assert enriched["1"]["metadata_complete"] is True
    assert enriched["1"]["mesh_terms"] == ["Humans"]
    assert enriched["2"]["metadata_complete"] is False
    assert "pmid" in enriched["2"]["metadata_missing"]


def test_main_persists_exact_query_count_timestamp_and_per_paper_membership(
    tmp_path: Path, monkeypatch,
):
    spec = fetcher.SEARCH_QUERY_SPECS[0]
    seen_kwargs = {}

    def fake_search(query, **kwargs):
        seen_kwargs.update(kwargs)
        return fetcher.SearchResult(
            query=query,
            count=1,
            ids=("123",),
            retrieved_at="2026-08-21T12:34:56Z",
            pages=1,
            page_counts=(1,),
            query_translation="translated",
            page_starts=(0,),
            snapshot_date="2026-08-20",
            effective_query="effective snapshot query",
            shards=(fetcher.SearchShard(
                query="effective snapshot query",
                date_from="1900-01-01",
                date_to="2026-08-20",
                count=1,
                retrieved_at="2026-08-21T12:34:56Z",
                is_leaf=True,
                pages=1,
                page_starts=(0,),
                page_counts=(1,),
                query_translation="translated",
            ),),
        )

    monkeypatch.setattr(fetcher, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "search_pmc", fake_search)
    monkeypatch.setattr(fetcher, "fetch_summaries", lambda ids, **kwargs: {
        "uids": ["123"],
        "123": {
            "title": "A review",
            "source": "Journal",
            "pubdate": "2026",
            "authors": [],
            "pubtype": ["Review"],
        },
    })
    monkeypatch.setattr(fetcher.time, "sleep", lambda _: None)

    fetcher.main([
        "--query-name", spec.name,
        "--ncbi-tool", "named-tool",
        "--ncbi-email", "contact@example.org",
        "--ncbi-api-key", "key",
    ])

    log = json.loads((tmp_path / "download_log.json").read_text())
    run = log["search_runs"][0]
    assert run["query_name"] == spec.name
    assert run["query"] == spec.query
    assert run["ncbi_count"] == 1
    assert run["retrieved_count"] == 1
    assert run["retrieved_at"] == "2026-08-21T12:34:56Z"
    assert run["snapshot_date"] == "2026-08-20"
    assert run["effective_query"] == "effective snapshot query"
    assert run["page_starts"] == [0]
    assert run["page_counts"] == [1]
    assert run["shards"][0]["count"] == 1
    membership = log["query_membership"]["PMC123"][0]
    assert membership["name"] == spec.name
    assert membership["query"] == spec.query
    assert membership["snapshot_date"] == "2026-08-20"
    assert log["excluded"][0]["query_membership"] == [membership]
    assert seen_kwargs["tool"] == "named-tool"
    assert seen_kwargs["email"] == "contact@example.org"
    assert seen_kwargs["api_key"] == "key"


def test_durable_manual_override_survives_full_rerun_and_reaches_manifest(
    tmp_path: Path, monkeypatch,
):
    spec = fetcher.SEARCH_QUERY_SPECS[0]
    monkeypatch.setattr(fetcher, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "SEARCH_QUERY_SPECS", [spec])
    monkeypatch.setattr(fetcher, "CORE_RECALL_REGRESSION_PMCS", frozenset())
    monkeypatch.setattr(fetcher, "search_pmc", lambda query, **kwargs: fetcher.SearchResult(
        query=query,
        count=1,
        ids=("9",),
        retrieved_at="2026-08-21T12:34:56Z",
        pages=1,
        page_counts=(1,),
        query_translation="translated",
        page_starts=(0,),
        snapshot_date="2026-08-20",
        effective_query="effective",
    ))
    monkeypatch.setattr(fetcher, "fetch_summaries", lambda ids, **kwargs: {
        "uids": ["9"],
        "9": {
            "title": "Integrated exposome signatures",
            "source": "Journal",
            "pubdate": "2026",
            "authors": [],
            "articleids": [],
        },
    })
    monkeypatch.setattr(fetcher, "get_oa_links", lambda pmcid: (None, None))
    monkeypatch.setattr(fetcher, "europepmc_pdf_url", lambda pmcid: None)
    monkeypatch.setattr(fetcher, "europepmc_fulltext_xml", lambda pmcid: None)
    monkeypatch.setattr(fetcher.time, "sleep", lambda _: None)
    (tmp_path / "screening_overrides.json").write_text(json.dumps({
        "PMC9": {
            "decision": "included",
            "reviewer": "AB",
            "reviewed_at": "2026-08-21T12:00:00Z",
            "eligibility_evidence": ["Full text reports a prospective human cohort."],
            "human_study": True,
            "primary_study": True,
            "scope_classification": "core-exposomics",
        }
    }))

    args = ["--ncbi-email", "contact@example.org", "--snapshot-date", "2026-08-20"]
    assert fetcher.main(args) == 1
    assert fetcher.main(args) == 1

    log = json.loads((tmp_path / "download_log.json").read_text())
    assert log["papers"][0]["screening"]["decision"] == "included"
    assert log["papers"][0]["screening"]["screening_method"] == "manual-override"
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    record = manifest["records"]["PMC9"]
    assert record["screening"]["decision"] == "included"
    assert record["screening"]["reviewer"] == "AB"
    assert record["publication_eligible"] is True


def test_manifest_eligibility_retains_local_audit_file_but_excludes_nonlocal():
    assert fetcher._manifest_status_for_screening(
        {"status": "downloaded", "path": "papers/PMC1.pdf"}, "excluded",
    ) == "downloaded"
    assert fetcher._manifest_status_for_screening(
        {"status": "discovered", "path": None}, "excluded",
    ) == "excluded"


def test_excluded_existing_file_is_retained_but_not_publication_eligible(
    tmp_path: Path, monkeypatch,
):
    spec = fetcher.SEARCH_QUERY_SPECS[0]
    source = tmp_path / "2026_PMC77_review.pdf"
    source.write_bytes(b"%PDF-1.7\n" + b"x" * 21_000)
    monkeypatch.setattr(fetcher, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "SEARCH_QUERY_SPECS", [spec])
    monkeypatch.setattr(fetcher, "CORE_RECALL_REGRESSION_PMCS", frozenset())
    monkeypatch.setattr(fetcher, "search_pmc", lambda query, **kwargs: fetcher.SearchResult(
        query=query,
        count=1,
        ids=("77",),
        retrieved_at="2026-08-21T12:34:56Z",
        pages=1,
        page_counts=(1,),
        query_translation="translated",
        snapshot_date="2026-08-20",
        effective_query="effective",
    ))
    monkeypatch.setattr(fetcher, "fetch_summaries", lambda ids, **kwargs: {
        "uids": ["77"],
        "77": {
            "title": "A review of the human exposome",
            "source": "Journal",
            "pubdate": "2026",
            "authors": [],
            "pubtype": ["Review"],
            "articleids": [],
        },
    })
    monkeypatch.setattr(fetcher.time, "sleep", lambda _: None)

    fetcher.main([
        "--ncbi-email", "contact@example.org",
        "--snapshot-date", "2026-08-20",
    ])

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    record = manifest["records"]["PMC77"]
    assert record["status"] == "downloaded"
    assert source.exists()
    assert str(record["path"]).endswith(source.name)
    assert record["screening"]["decision"] == "excluded"
    assert record["publication_eligible"] is False
