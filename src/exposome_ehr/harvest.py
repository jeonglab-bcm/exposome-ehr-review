"""Harvest orchestration.

The corpus is defined by ONE broad query. Facets are applied afterwards as
tags, never as retrieval filters, so a paper is never dropped for failing
to mention an exposure or an age group. Two independent tag paths are recorded per facet:

  pubmed_query - the facet's title/abstract query, intersected with the corpus.
  mesh_term    - NLM's own MeSH indexing of the record.

They disagree often, and the disagreement is kept rather than reconciled: it is
the closest thing this corpus has to a measure of how much a phenotype
definition depends on free text versus on curated indexing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import Vocabulary
from .eutils import EutilsClient
from .parse import parse_efetch_response
from .store import Store, utcnow

log = logging.getLogger(__name__)


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("run_%Y%m%dT%H%M%SZ")


@dataclass
class HarvestSummary:
    run_id: str
    profile: str
    esearch_count: int = 0
    pmids_retrieved: int = 0
    articles_stored: int = 0
    jsonl_records: int = 0
    corpus_size: int = 0
    facets_run: int = 0
    facet_tag_rows: int = 0
    mesh_tag_rows: int = 0
    eutils_requests: int = 0
    block_counts: dict[str, int] = field(default_factory=dict)
    facet_counts: dict[str, int] = field(default_factory=dict)
    seed_resolution: dict[str, int] = field(default_factory=dict)
    truncated_to: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "profile": self.profile,
            "esearch_count": self.esearch_count,
            "pmids_retrieved": self.pmids_retrieved,
            "articles_stored": self.articles_stored,
            "jsonl_records": self.jsonl_records,
            "corpus_size": self.corpus_size,
            "facets_run": self.facets_run,
            "facet_tag_rows": self.facet_tag_rows,
            "mesh_tag_rows": self.mesh_tag_rows,
            "eutils_requests": self.eutils_requests,
            "truncated_to": self.truncated_to,
            "block_counts": self.block_counts,
            "facet_counts": self.facet_counts,
            "seed_resolution": self.seed_resolution,
        }


class Harvester:
    def __init__(
        self,
        store: Store,
        vocab: Vocabulary,
        client: EutilsClient | None = None,
        profile: str = "core",
    ):
        self.store = store
        self.vocab = vocab
        self.client = client or EutilsClient()
        self.profile = profile

    # ------------------------------------------------------------------ run
    def run(
        self,
        limit: int | None = None,
        skip_facets: bool = False,
        batch_size: int = 200,
    ) -> HarvestSummary:
        run_id = new_run_id()
        corpus_query = self.vocab.corpus_query(self.profile)
        summary = HarvestSummary(run_id=run_id, profile=self.profile)

        self.store.upsert_vocabulary(self.vocab)
        self.store.start_run(run_id, self.profile, corpus_query, __version__)

        log.info("run %s: corpus esearch (profile=%s)", run_id, self.profile)
        search = self.client.search_all_pmids(corpus_query)
        summary.esearch_count = search.count
        pmids = search.pmids
        if search.warnings:
            log.warning("esearch warnings: %s", search.warnings)
        if limit is not None and len(pmids) > limit:
            summary.truncated_to = limit
            pmids = pmids[:limit]
            log.warning("corpus truncated to %d PMIDs by --limit", limit)
        summary.pmids_retrieved = len(pmids)
        log.info("corpus: %d records reported, %d PMIDs to fetch", search.count, len(pmids))

        summary.articles_stored = self._fetch_articles(run_id, pmids, batch_size)

        # Tag against what is actually in the article table, not against the
        # PMID list esearch handed back. PubMed can return no record for a PMID
        # it just listed (deleted or embargoed citations), and tagging such a
        # PMID would fail the article_facet foreign key and abort the run.
        corpus = self._stored_subset(pmids)
        summary.corpus_size = len(corpus)
        missing = len(pmids) - len(corpus)
        if missing:
            log.warning(
                "%d of %d PMIDs have no stored record and are excluded from tagging",
                missing, len(pmids),
            )

        self._attribute_query_blocks(run_id, corpus)

        if not skip_facets:
            summary.facets_run, summary.facet_tag_rows, summary.facet_counts = (
                self._tag_facets_by_query(run_id, corpus)
            )
        summary.mesh_tag_rows = self._tag_facets_by_mesh()
        summary.seed_resolution = self.resolve_seed_references()

        summary.block_counts = {
            row["block_id"]: row["esearch_count"]
            for row in self.store.rows(
                "SELECT block_id, esearch_count FROM query_block WHERE run_id = ?", (run_id,)
            )
        }
        kept, dropped = self.store.compact_jsonl()
        summary.jsonl_records = kept
        if dropped:
            log.info("compacted %s: %d records kept, %d duplicates dropped",
                     self.store.jsonl_path.name, kept, dropped)

        summary.eutils_requests = self.client.request_count
        self.store.finish_run(
            run_id,
            esearch_count=summary.esearch_count,
            pmids_retrieved=summary.pmids_retrieved,
            articles_stored=summary.articles_stored,
            facets_run=summary.facets_run,
            eutils_requests=summary.eutils_requests,
            notes=json.dumps({"truncated_to": summary.truncated_to}),
        )
        return summary

    def _stored_subset(self, pmids: list[str]) -> set[str]:
        """Those of ``pmids`` that exist in the article table."""
        found: set[str] = set()
        for start in range(0, len(pmids), 500):
            chunk = pmids[start : start + 500]
            placeholders = ",".join("?" * len(chunk))
            found.update(
                row["pmid"]
                for row in self.store.rows(
                    f"SELECT pmid FROM article WHERE pmid IN ({placeholders})", tuple(chunk)
                )
            )
        return found

    # -------------------------------------------------------------- fetching
    def _fetch_articles(self, run_id: str, pmids: list[str], batch_size: int) -> int:
        stored = 0
        total_batches = (len(pmids) + batch_size - 1) // batch_size or 1
        for index, chunk, raw in self.client.iter_article_batches(pmids, batch_size):
            self.store.write_raw(run_id, index, raw)
            records = parse_efetch_response(raw)
            self.store.append_jsonl(records)
            stored += self.store.upsert_articles(records, run_id)
            log.info(
                "efetch batch %d/%d: %d requested, %d parsed, %d stored so far",
                index + 1, total_batches, len(chunk), len(records), stored,
            )
            missing = set(chunk) - {r["pmid"] for r in records}
            if missing:
                log.warning("batch %d: %d PMIDs returned no record", index + 1, len(missing))
        return stored

    # ------------------------------------------------------------ block prov
    def _attribute_query_blocks(self, run_id: str, corpus: set[str]) -> None:
        """Record which corpus-query clause retrieved each PMID."""
        for block in self.vocab.profile_blocks(self.profile):
            result = self.client.search_all_pmids(block["query"])
            hits = set(result.pmids) & corpus
            with self.store.transaction() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO query_block"
                    "(run_id, block_id, query, rationale, esearch_count, translation)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        run_id, block["id"], block["query"],
                        " ".join((block.get("rationale") or "").split()),
                        result.count, result.translation,
                    ),
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO article_query_block(pmid, run_id, block_id) VALUES (?,?,?)",
                    [(pmid, run_id, block["id"]) for pmid in sorted(hits)],
                )
            log.info(
                "block %s: %d in PubMed, %d inside corpus", block["id"], result.count, len(hits)
            )

    # ----------------------------------------------------------- facet tags
    def _tag_facets_by_query(
        self, run_id: str, corpus: set[str]
    ) -> tuple[int, int, dict[str, int]]:
        facets_run = 0
        rows_written = 0
        counts: dict[str, int] = {}
        for facet in self.vocab.facet_list:
            facet_id = facet["id"]
            query = self.vocab.facet_query(facet_id, self.profile)
            result = self.client.search_all_pmids(query)
            hits = sorted(set(result.pmids) & corpus)
            counts[facet_id] = len(hits)
            with self.store.transaction() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO facet_query_run"
                    "(run_id, facet_id, query, esearch_count, pmids_in_corpus, translation, ran_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (run_id, facet_id, query, result.count, len(hits), result.translation, utcnow()),
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO article_facet(pmid, facet_id, evidence_source, detail)"
                    " VALUES (?,?, 'pubmed_query', ?)",
                    [(pmid, facet_id, run_id) for pmid in hits],
                )
            rows_written += len(hits)
            facets_run += 1
            log.info(
                "facet %-34s %5d in PubMed, %5d in corpus",
                facet_id, result.count, len(hits),
            )
        return facets_run, rows_written, counts

    def _tag_facets_by_mesh(self) -> int:
        """Second tag path: NLM MeSH indexing, computed locally with no API calls."""
        written = 0
        with self.store.transaction() as conn:
            for facet in self.vocab.facet_list:
                terms = facet.get("mesh_terms") or []
                if not terms:
                    continue
                placeholders = ",".join("?" * len(terms))
                rows = conn.execute(
                    "SELECT DISTINCT pmid, descriptor_name FROM article_mesh"
                    f" WHERE descriptor_name IN ({placeholders})",
                    tuple(terms),
                ).fetchall()
                by_pmid: dict[str, list[str]] = {}
                for row in rows:
                    by_pmid.setdefault(row["pmid"], []).append(row["descriptor_name"])
                conn.executemany(
                    "INSERT OR REPLACE INTO article_facet(pmid, facet_id, evidence_source, detail)"
                    " VALUES (?,?, 'mesh_term', ?)",
                    [
                        (pmid, facet["id"], "; ".join(sorted(names)))
                        for pmid, names in by_pmid.items()
                    ],
                )
                written += len(by_pmid)
        return written

    # ------------------------------------------------------------ seed check
    @staticmethod
    def _normalize_title(title: str | None) -> str:
        if not title:
            return ""
        return "".join(ch for ch in title.lower() if ch.isalnum() or ch == " ").strip()

    def resolve_seed_references(self) -> dict[str, int]:
        """Look up each seed paper by title match and check corpus membership.

        PubMed treats an unquoted [Title] search as a relevance-ordered bag of
        words, so the first hit for a title query is often a different paper.
        Resolution therefore requires the returned record's title to match the
        declared expected_title; anything else is recorded as ambiguous rather
        than accepted.

        Returns a tally of resolution methods (e.g. title_exact, ambiguous,
        not_found) so the run summary can report how many seeds resolved
        without having to query the table.
        """
        outcomes: dict[str, int] = {}
        for ref_id, ref in self.vocab.seed_references.items():
            lookup = ref.get("pubmed_lookup")
            if not lookup:
                continue
            try:
                result = self.client.esearch(lookup, retmax=20)
            except Exception as exc:  # pragma: no cover - network dependent
                log.warning("seed lookup failed for %s: %s", ref_id, exc)
                outcomes["lookup_failed"] = outcomes.get("lookup_failed", 0) + 1
                continue

            expected_title = self._normalize_title(ref.get("expected_title"))
            pmid: str | None = None
            resolved_title: str | None = None
            method = "not_found"

            if result.pmids:
                titles: dict[str, str | None] = {}
                for candidate in result.pmids:
                    row = self.store.rows(
                        "SELECT title FROM article WHERE pmid = ?", (candidate,)
                    )
                    titles[candidate] = row[0]["title"] if row else None
                unknown = [p for p, t in titles.items() if t is None]
                if unknown:
                    # Candidates outside the corpus still need titles to match on.
                    try:
                        fetched = parse_efetch_response(
                            self.client.efetch_pubmed_xml(unknown)
                        )
                        for record in fetched:
                            titles[record["pmid"]] = record["title"]
                    except Exception as exc:  # pragma: no cover
                        log.warning("seed title fetch failed for %s: %s", ref_id, exc)

                if expected_title:
                    for candidate, title in titles.items():
                        if self._normalize_title(title) == expected_title:
                            pmid, resolved_title, method = candidate, title, "title_exact"
                            break
                    if pmid is None:
                        method = "ambiguous"
                elif len(result.pmids) == 1:
                    pmid = result.pmids[0]
                    resolved_title = titles.get(pmid)
                    method = "unique_hit"
                else:
                    method = "ambiguous"

            in_corpus = int(
                bool(pmid)
                and bool(self.store.scalar("SELECT 1 FROM article WHERE pmid = ?", (pmid,)))
            )
            expected_pmid = ref.get("expected_pmid")
            expected_in_corpus = int(
                bool(expected_pmid)
                and bool(
                    self.store.scalar("SELECT 1 FROM article WHERE pmid = ?", (expected_pmid,))
                )
            )
            with self.store.transaction() as conn:
                cursor = conn.execute(
                    "UPDATE seed_reference SET resolved_pmid = ?, resolved_title = ?,"
                    " match_method = ?, lookup_hits = ?, found_in_corpus = ?,"
                    " expected_pmid_in_corpus = ? WHERE ref_id = ?",
                    (
                        pmid, resolved_title, method, result.count,
                        in_corpus, expected_in_corpus, ref_id,
                    ),
                )
                if cursor.rowcount == 0:
                    # The vocabulary snapshot must exist before resolution runs.
                    # A silent zero-row update here is how a green report can
                    # hide an unresolved seed, so fail loudly instead.
                    raise RuntimeError(
                        f"seed_reference row for {ref_id!r} is missing; "
                        "call Store.upsert_vocabulary before resolving seeds"
                    )
            outcomes[method] = outcomes.get(method, 0) + 1
            log.info(
                "seed %-12s pmid=%s method=%s in_corpus=%s expected_in_corpus=%s (%d hits)",
                ref_id, pmid, method, bool(in_corpus), bool(expected_in_corpus), result.count,
            )
        return outcomes


def check_mesh_terms(store: Store, vocab: Vocabulary, client: EutilsClient) -> dict[str, int]:
    """Validate every declared MeSH descriptor name against PubMed's MeSH index.

    A descriptor that PubMed does not recognise is a typo in the curated
    vocabulary. A descriptor PubMed recognises but that no corpus record carries
    is fine: it just means the corpus has no paper indexed that way.
    """
    stats = {"checked": 0, "invalid": 0, "unused_in_corpus": 0}
    seen: set[tuple[str, str]] = set()
    for facet in vocab.facet_list:
        for term in facet.get("mesh_terms") or []:
            key = (facet["id"], term)
            if key in seen:
                continue
            seen.add(key)
            escaped = term.replace('"', '\\"')
            result = client.esearch(f'"{escaped}"[MeSH Terms]', retmax=0)
            translation = result.translation or ""
            # PubMed silently falls back to a free-text search when a descriptor
            # does not exist, so a non-zero count is not enough on its own: the
            # query translation has to still name the MeSH Terms field.
            is_valid = "[MeSH Terms]" in translation and result.count > 0
            corpus_count = store.scalar(
                "SELECT COUNT(DISTINCT pmid) FROM article_mesh WHERE descriptor_name = ?",
                (term,),
            ) or 0
            with store.transaction() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO mesh_term_check"
                    "(facet_id, descriptor_name, is_valid, pubmed_count, corpus_count,"
                    " translation, checked_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        facet["id"], term, int(is_valid), result.count,
                        corpus_count, translation[:400], utcnow(),
                    ),
                )
            stats["checked"] += 1
            if not is_valid:
                stats["invalid"] += 1
                log.warning(
                    "MeSH descriptor not recognised: %r (facet %s) -> %s",
                    term, facet["id"], translation[:120],
                )
            elif corpus_count == 0:
                stats["unused_in_corpus"] += 1
    return stats


def rebuild_from_jsonl(store: Store, vocab: Vocabulary, run_id: str = "rebuild") -> int:
    """Rebuild the article tables from the JSONL layer without touching the network."""
    store.upsert_vocabulary(vocab)
    batch: list[dict[str, Any]] = []
    total = 0
    for record in store.iter_jsonl():
        batch.append(record)
        if len(batch) >= 500:
            total += store.upsert_articles(batch, run_id)
            batch = []
    if batch:
        total += store.upsert_articles(batch, run_id)
    return total
