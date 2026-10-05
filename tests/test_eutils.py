"""EutilsClient paging, driven by a stub HTTP client (no network)."""

from exposome_ehr.eutils import EutilsClient


class PagingStub:
    """Serves a multi-page esearch result set from the history server."""

    def __init__(self):
        self.calls = []
        # count=5, page_size will be 2 in the test -> 3 pages
        self._pages = [
            {"esearchresult": {"count": "5", "idlist": ["1", "2"],
                               "webenv": "WE1", "querykey": "1"}},
            {"esearchresult": {"idlist": ["3", "4"]}},
            {"esearchresult": {"idlist": ["5", "2"]}},  # dup "2" across pages
        ]

    def post_json(self, url, data=None, headers=None):
        params = data
        self.calls.append(dict(params or {}))
        return self._pages[len(self.calls) - 1]


def test_search_all_pmids_pages_via_history_server():
    stub = PagingStub()
    client = EutilsClient(client=stub)
    result = client.search_all_pmids("some term", page_size=2)

    # Deduplicated across pages, order preserved.
    assert result.pmids == ["1", "2", "3", "4", "5"]
    assert result.count == 5

    # First call is the initial search with the term and history requested.
    assert stub.calls[0]["term"] == "some term"
    assert stub.calls[0]["usehistory"] == "y"

    # Subsequent pages read the cached set: WebEnv + query_key, no re-sent term.
    for page_params in stub.calls[1:]:
        assert page_params.get("WebEnv") == "WE1"
        assert page_params.get("query_key") == "1"
        assert "term" not in page_params
        assert page_params["retstart"] in (2, 4)


def test_search_all_pmids_falls_back_to_term_without_a_handle():
    """If the server returns no WebEnv, paging still works by re-querying term."""

    class NoHistoryStub:
        def __init__(self):
            self.calls = []
            self._pages = [
                {"esearchresult": {"count": "4", "idlist": ["1", "2"]}},
                {"esearchresult": {"idlist": ["3", "4"]}},
            ]

        def post_json(self, url, data=None, headers=None):
            self.calls.append(dict(data or {}))
            return self._pages[len(self.calls) - 1]

    stub = NoHistoryStub()
    result = EutilsClient(client=stub).search_all_pmids("q", page_size=2)
    assert result.pmids == ["1", "2", "3", "4"]
    assert stub.calls[1].get("term") == "q"
    assert "WebEnv" not in stub.calls[1]


class YearStub:
    """A fake PubMed holding one PMID per (year, i); answers PDAT-sliced queries."""

    def __init__(self, per_year: dict[int, int]):
        import re

        self.re = re
        self.records = {f"{y}-{i}": y for y, n in per_year.items() for i in range(n)}
        self.terms = []

    def post_json(self, url, data=None, headers=None):
        params = data
        term = params["term"]
        self.terms.append(term)
        m = self.re.search(r'"(\d+)"\[PDAT\] : "(\d+)"\[PDAT\]', term)
        lo, hi = (int(m.group(1)), int(m.group(2))) if m else (0, 10**6)
        hits = [p for p, y in self.records.items() if lo <= y <= hi]
        return {"esearchresult": {"count": str(len(hits)), "idlist": hits[: int(params["retmax"])],
                                  "webenv": "W", "querykey": "1"}}


def test_search_over_the_esearch_window_is_read_in_date_slices(monkeypatch):
    """PubMed serves at most 9,999 PMIDs per query; larger sets are sliced by year."""
    import exposome_ehr.eutils as eutils

    monkeypatch.setattr(eutils, "ESEARCH_WINDOW", 10)
    stub = YearStub({2001: 4, 2010: 7, 2020: 6})   # 17 records, window 10
    result = EutilsClient(client=stub).search_all_pmids("exposome[tiab]")
    assert result.count == 17
    assert sorted(result.pmids) == sorted(stub.records)
    assert any("[PDAT]" in t for t in stub.terms)
