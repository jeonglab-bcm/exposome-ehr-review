"""NCBI E-utilities client (PubMed only).

Rate limits are NCBI's published ones: 3 requests per second anonymously, 10
with an API key. Set ``NCBI_API_KEY`` (and ideally ``NCBI_TOOL_EMAIL``) to get
the higher ceiling.
"""

from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterator

from .httpclient import RateLimitedClient

log = logging.getLogger(__name__)

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EFETCH_BATCH = 200
# PubMed esearch returns at most this many PMIDs for one query, however paged.
ESEARCH_WINDOW = 9999


@dataclass
class SearchResult:
    """Outcome of an esearch call using the Entrez history server."""

    term: str
    count: int
    webenv: str | None
    query_key: str | None
    pmids: list[str]
    translation: str | None = None
    warnings: dict | None = None


class EutilsClient:
    def __init__(
        self,
        api_key: str | None = None,
        email: str | None = None,
        tool: str = "exposome-ehr",
        client: RateLimitedClient | None = None,
    ):
        self.api_key = api_key if api_key is not None else os.environ.get("NCBI_API_KEY")
        self.email = email if email is not None else os.environ.get("NCBI_TOOL_EMAIL")
        self.tool = tool
        # NCBI allows 10 req/s with a key and 3 without. 0.34s tripped occasional
        # 429s in practice, so the anonymous floor sits above the nominal rate.
        interval = 0.12 if self.api_key else 0.40
        self.client = client or RateLimitedClient(min_interval=interval)

    def _common(self) -> dict[str, str]:
        params = {"tool": self.tool}
        if self.api_key:
            params["api_key"] = self.api_key
        if self.email:
            params["email"] = self.email
        return params

    @property
    def request_count(self) -> int:
        return self.client.request_count

    # -- search -----------------------------------------------------------
    def esearch(
        self,
        term: str,
        retmax: int = 0,
        use_history: bool = True,
        db: str = "pubmed",
    ) -> SearchResult:
        params = {
            **self._common(),
            "db": db,
            "term": term,
            "retmax": retmax,
            "retmode": "json",
            "usehistory": "y" if use_history else "n",
        }
        payload = self.client.post_json(f"{BASE}/esearch.fcgi", data=params)
        result = payload["esearchresult"]
        if "ERROR" in result:
            raise RuntimeError(f"esearch error for {term!r}: {result['ERROR']}")
        return SearchResult(
            term=term,
            count=int(result["count"]),
            webenv=result.get("webenv"),
            query_key=result.get("querykey"),
            pmids=list(result.get("idlist", [])),
            translation=result.get("querytranslation"),
            warnings=result.get("warninglist"),
        )

    def search_all_pmids(self, term: str, page_size: int = 10000) -> SearchResult:
        """esearch, then page through the full PMID list via the history server.

        The first esearch stores the result set on the Entrez history server and
        returns a WebEnv/query_key handle. Subsequent pages are read from that
        cached set with retstart, rather than re-executing the query per page:
        re-running the search would page an independently-recomputed, possibly
        index-shifted result set.
        """
        page_size = min(page_size, ESEARCH_WINDOW)
        head = self.esearch(term, retmax=page_size)
        if head.count > ESEARCH_WINDOW:
            # PubMed no longer serves esearch results past the 9,999th (any
            # retstart beyond it returns an error), so a larger set is read in
            # publication-date slices that each fit inside the window.
            head.pmids = self._search_by_date_slices(term, head.count)
            return head
        pmids = list(head.pmids)
        while len(pmids) < head.count:
            params = {
                **self._common(),
                "db": "pubmed",
                "retstart": len(pmids),
                "retmax": page_size,
                "retmode": "json",
            }
            # Page the cached history set when we have a handle; fall back to
            # re-querying by term only if the server returned no WebEnv.
            if head.webenv and head.query_key:
                params["WebEnv"] = head.webenv
                params["query_key"] = head.query_key
            else:
                params["term"] = term
            payload = self.client.post_json(f"{BASE}/esearch.fcgi", data=params)
            page = payload["esearchresult"].get("idlist", [])
            if not page:
                log.warning(
                    "esearch stopped early at %d of %d for %r",
                    len(pmids),
                    head.count,
                    term[:80],
                )
                break
            pmids.extend(page)
        # A shifting index can still repeat a PMID across pages; keep the dedup.
        head.pmids = list(dict.fromkeys(pmids))
        return head

    def _search_by_date_slices(self, term: str, total: int, first: int = 1800, last: int | None = None) -> list[str]:
        """All PMIDs for ``term``, read in publication-year slices under the window.

        A slice still over the window is halved; a single year over the window
        cannot be split by year and is reported rather than silently truncated.
        The slices partition the years, so their counts must add up to
        ``total``; a shortfall (records with no usable date) is logged.
        """
        import datetime

        last = last or datetime.date.today().year + 1
        pmids: list[str] = []
        stack = [(first, last)]
        while stack:
            lo, hi = stack.pop()
            sliced = f'({term}) AND ("{lo}"[PDAT] : "{hi}"[PDAT])'
            part = self.esearch(sliced, retmax=ESEARCH_WINDOW)
            if part.count <= ESEARCH_WINDOW:
                pmids.extend(part.pmids)
            elif lo < hi:
                mid = (lo + hi) // 2
                stack += [(lo, mid), (mid + 1, hi)]
            else:
                log.warning("year %d alone has %d records; only %d retrieved", lo, part.count, len(part.pmids))
                pmids.extend(part.pmids)
        pmids = list(dict.fromkeys(pmids))
        if len(pmids) != total:
            log.warning("date slices retrieved %d of %d PMIDs for %r", len(pmids), total, term[:80])
        return pmids

    # -- fetch ------------------------------------------------------------
    def efetch_pubmed_xml(self, pmids: list[str]) -> bytes:
        params = {
            **self._common(),
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "xml",
        }
        # efetch id lists go in the body: a 200-PMID GET is near the URL limit.
        return self.client.request(
            "POST", f"{BASE}/efetch.fcgi", data=params
        )

    def efetch_pmc_xml(self, pmcid: str) -> bytes:
        """One PMC article as JATS XML. Only open-access articles carry a <body>."""
        params = {**self._common(), "db": "pmc", "id": pmcid.removeprefix("PMC")}
        return self.client.request("POST", f"{BASE}/efetch.fcgi", data=params)

    def iter_article_batches(
        self, pmids: list[str], batch_size: int = EFETCH_BATCH
    ) -> Iterator[tuple[int, list[str], bytes]]:
        """Yield ``(batch_index, pmids, raw_xml)`` for each efetch batch."""
        for index, start in enumerate(range(0, len(pmids), batch_size)):
            chunk = pmids[start : start + batch_size]
            yield index, chunk, self.efetch_pubmed_xml(chunk)

    # -- link -------------------------------------------------------------
    def elink_citedin(self, pmid: str) -> list[str]:
        """PMIDs of PubMed records citing ``pmid`` (coverage is partial)."""
        params = {
            **self._common(),
            "dbfrom": "pubmed",
            "db": "pubmed",
            "id": pmid,
            "linkname": "pubmed_pubmed_citedin",
            "retmode": "json",
        }
        payload = self.client.get_json(f"{BASE}/elink.fcgi", params=params)
        out: list[str] = []
        for linkset in payload.get("linksets", []):
            for db in linkset.get("linksetdbs", []):
                if db.get("linkname") == "pubmed_pubmed_citedin":
                    out.extend(str(x) for x in db.get("links", []))
        return out


def split_pubmed_articles(raw_xml: bytes) -> list[ET.Element]:
    """Split an efetch response into PubmedArticle / PubmedBookArticle elements."""
    root = ET.fromstring(raw_xml)
    return list(root.findall("PubmedArticle")) + list(root.findall("PubmedBookArticle"))
