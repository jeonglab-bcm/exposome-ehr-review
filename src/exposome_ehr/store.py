"""Storage layer: raw XML cache, JSONL record layer, normalized SQLite.

Three layers, deliberately:

  data/raw/    every efetch response verbatim, gzipped. Re-parsing never needs
               the network, so parser changes are cheap.
  data/articles/pmid_NN.jsonl
               one JSON object per article, the record layer that other tools
               can stream without SQLite. Written by appending to
               data/articles.jsonl during a run and compacted at the end of it:
               deduplicated by PMID, newest record winning, sorted by PMID and
               split into shards of one million PMIDs (pmid_42.jsonl holds
               42,000,000-42,999,999). The shards are version-controlled, so they
               have to be deterministic and produce a readable diff rather than
               growing by a full copy on every re-run; sharding keeps every file
               under GitHub's 100 MB limit (the whole corpus is over it) and a
               record never moves between shards.
  data/db/     normalized SQLite, rebuildable from the JSONL at any time.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------- provenance
CREATE TABLE IF NOT EXISTS harvest_run (
    run_id            TEXT PRIMARY KEY,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    query_profile     TEXT NOT NULL,
    corpus_query      TEXT NOT NULL,
    esearch_count     INTEGER,
    pmids_retrieved   INTEGER,
    articles_stored   INTEGER,
    facets_run        INTEGER,
    eutils_requests   INTEGER,
    tool_version      TEXT,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS query_block (
    run_id        TEXT NOT NULL REFERENCES harvest_run(run_id),
    block_id      TEXT NOT NULL,
    query         TEXT NOT NULL,
    rationale     TEXT,
    esearch_count INTEGER,
    translation   TEXT,
    PRIMARY KEY (run_id, block_id)
);

-- Which clause of the corpus query retrieved each record. Lets a reviewer see
-- what the guarded-acronym clause is actually contributing.
CREATE TABLE IF NOT EXISTS article_query_block (
    pmid     TEXT NOT NULL,
    run_id   TEXT NOT NULL,
    block_id TEXT NOT NULL,
    PRIMARY KEY (pmid, run_id, block_id)
);

-- ------------------------------------------------------------------ articles
CREATE TABLE IF NOT EXISTS article (
    pmid                  TEXT PRIMARY KEY,
    doi                   TEXT,
    pmc                   TEXT,
    pii                   TEXT,
    record_type           TEXT,
    title                 TEXT,
    vernacular_title      TEXT,
    abstract              TEXT,
    has_abstract          INTEGER NOT NULL DEFAULT 0,
    abstract_section_count INTEGER,
    journal_title         TEXT,
    journal_iso           TEXT,
    journal_nlm_id        TEXT,
    journal_country       TEXT,
    issn                  TEXT,
    volume                TEXT,
    issue                 TEXT,
    pagination            TEXT,
    pub_year              INTEGER,
    pub_month             INTEGER,
    pub_day               INTEGER,
    medline_date          TEXT,
    article_date          TEXT,
    entrez_date           TEXT,
    medline_status        TEXT,
    owner                 TEXT,
    indexing_method       TEXT,
    publication_status    TEXT,
    author_count          INTEGER,
    mesh_descriptor_count INTEGER,
    is_mesh_indexed       INTEGER NOT NULL DEFAULT 0,
    is_retracted          INTEGER NOT NULL DEFAULT 0,
    is_human_tagged       INTEGER NOT NULL DEFAULT 0,
    is_animal_tagged      INTEGER NOT NULL DEFAULT 0,
    integrity_flags       TEXT,
    first_seen_run        TEXT,
    fetched_at            TEXT
);
CREATE INDEX IF NOT EXISTS ix_article_year ON article(pub_year);
CREATE INDEX IF NOT EXISTS ix_article_journal ON article(journal_iso);

CREATE TABLE IF NOT EXISTS article_language (
    pmid     TEXT NOT NULL REFERENCES article(pmid) ON DELETE CASCADE,
    language TEXT NOT NULL,
    PRIMARY KEY (pmid, language)
);

CREATE TABLE IF NOT EXISTS article_publication_type (
    pmid    TEXT NOT NULL REFERENCES article(pmid) ON DELETE CASCADE,
    type_ui TEXT,
    type_name TEXT NOT NULL,
    PRIMARY KEY (pmid, type_name)
);
CREATE INDEX IF NOT EXISTS ix_pubtype_name ON article_publication_type(type_name);

-- qualifier_ui is '' rather than NULL when the heading carries no qualifier,
-- because SQLite does not treat NULLs as equal inside a primary key and the
-- pair really is the natural key here.
CREATE TABLE IF NOT EXISTS article_mesh (
    pmid             TEXT NOT NULL REFERENCES article(pmid) ON DELETE CASCADE,
    descriptor_ui    TEXT NOT NULL,
    descriptor_name  TEXT NOT NULL,
    descriptor_major INTEGER NOT NULL DEFAULT 0,
    qualifier_ui     TEXT NOT NULL DEFAULT '',
    qualifier_name   TEXT,
    qualifier_major  INTEGER NOT NULL DEFAULT 0,
    has_qualifier    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (pmid, descriptor_ui, qualifier_ui)
);
CREATE INDEX IF NOT EXISTS ix_mesh_descriptor ON article_mesh(descriptor_name);

CREATE TABLE IF NOT EXISTS article_author (
    pmid             TEXT NOT NULL REFERENCES article(pmid) ON DELETE CASCADE,
    position         INTEGER NOT NULL,
    last_name        TEXT,
    fore_name        TEXT,
    initials         TEXT,
    collective_name  TEXT,
    orcid            TEXT,
    affiliation      TEXT,
    affiliation_count INTEGER,
    PRIMARY KEY (pmid, position)
);
CREATE INDEX IF NOT EXISTS ix_author_last ON article_author(last_name);

CREATE TABLE IF NOT EXISTS article_chemical (
    pmid TEXT NOT NULL REFERENCES article(pmid) ON DELETE CASCADE,
    substance_ui TEXT,
    substance_name TEXT NOT NULL,
    PRIMARY KEY (pmid, substance_name)
);

CREATE TABLE IF NOT EXISTS article_keyword (
    pmid    TEXT NOT NULL REFERENCES article(pmid) ON DELETE CASCADE,
    keyword TEXT NOT NULL,
    is_major INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (pmid, keyword)
);

CREATE TABLE IF NOT EXISTS article_grant (
    pmid     TEXT NOT NULL REFERENCES article(pmid) ON DELETE CASCADE,
    grant_id TEXT,
    agency   TEXT,
    country  TEXT
);

CREATE TABLE IF NOT EXISTS article_abstract_section (
    pmid          TEXT NOT NULL REFERENCES article(pmid) ON DELETE CASCADE,
    section_order INTEGER NOT NULL,
    label         TEXT,
    nlm_category  TEXT,
    text          TEXT NOT NULL,
    PRIMARY KEY (pmid, section_order)
);

CREATE TABLE IF NOT EXISTS article_integrity_link (
    pmid        TEXT NOT NULL REFERENCES article(pmid) ON DELETE CASCADE,
    ref_type    TEXT NOT NULL,
    ref_source  TEXT,
    target_pmid TEXT
);

CREATE TABLE IF NOT EXISTS article_reference (
    pmid            TEXT NOT NULL REFERENCES article(pmid) ON DELETE CASCADE,
    referenced_pmid TEXT NOT NULL,
    PRIMARY KEY (pmid, referenced_pmid)
);

-- -------------------------------------------------------------------- facets
CREATE TABLE IF NOT EXISTS facet_group (
    group_id   TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    definition TEXT
);

CREATE TABLE IF NOT EXISTS facet (
    facet_id     TEXT PRIMARY KEY,
    group_id     TEXT NOT NULL REFERENCES facet_group(group_id),
    label        TEXT NOT NULL,
    definition   TEXT,
    pubmed_query TEXT NOT NULL,
    mesh_terms   TEXT,
    source_refs  TEXT
);

CREATE TABLE IF NOT EXISTS facet_query_run (
    run_id        TEXT NOT NULL,
    facet_id      TEXT NOT NULL REFERENCES facet(facet_id),
    query         TEXT NOT NULL,
    esearch_count INTEGER,
    pmids_in_corpus INTEGER,
    translation   TEXT,
    ran_at        TEXT,
    PRIMARY KEY (run_id, facet_id)
);

-- The tag join. evidence_source distinguishes a title/abstract query hit from
-- an NLM MeSH indexing hit; both are kept because they disagree often enough
-- to matter, and disagreement is itself a signal for note-NLP work.
CREATE TABLE IF NOT EXISTS article_facet (
    pmid            TEXT NOT NULL REFERENCES article(pmid) ON DELETE CASCADE,
    facet_id        TEXT NOT NULL REFERENCES facet(facet_id),
    evidence_source TEXT NOT NULL,
    detail          TEXT,
    PRIMARY KEY (pmid, facet_id, evidence_source)
);
CREATE INDEX IF NOT EXISTS ix_article_facet_facet ON article_facet(facet_id);

CREATE TABLE IF NOT EXISTS seed_reference (
    ref_id          TEXT PRIMARY KEY,
    citation        TEXT NOT NULL,
    pubmed_lookup   TEXT,
    expected_title  TEXT,
    expected_pmid   TEXT,
    note            TEXT,
    resolved_pmid   TEXT,
    resolved_title  TEXT,
    match_method    TEXT,
    lookup_hits     INTEGER,
    found_in_corpus INTEGER,
    expected_pmid_in_corpus INTEGER
);

-- ------------------------------------------------------------------ ontology
CREATE TABLE IF NOT EXISTS ontology_node (
    node_id    TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    group_id   TEXT,
    definition TEXT
);

CREATE TABLE IF NOT EXISTS ontology_predicate (
    predicate   TEXT PRIMARY KEY,
    transitive  INTEGER NOT NULL DEFAULT 0,
    subsumption INTEGER NOT NULL DEFAULT 0,
    symmetric   INTEGER NOT NULL DEFAULT 0,
    inverse     TEXT
);

CREATE TABLE IF NOT EXISTS ontology_edge (
    subject_id TEXT NOT NULL REFERENCES ontology_node(node_id),
    predicate  TEXT NOT NULL REFERENCES ontology_predicate(predicate),
    object_id  TEXT NOT NULL REFERENCES ontology_node(node_id),
    provenance TEXT,
    source     TEXT NOT NULL DEFAULT 'curated',
    PRIMARY KEY (subject_id, predicate, object_id, source)
);
CREATE INDEX IF NOT EXISTS ix_edge_predicate ON ontology_edge(predicate);

-- Transitive closure of the subsumption-like predicates, materialised so that
-- "every descendant of X" is one join rather than a recursive CTE per query.
CREATE TABLE IF NOT EXISTS ontology_closure (
    ancestor_id   TEXT NOT NULL,
    descendant_id TEXT NOT NULL,
    predicate     TEXT NOT NULL,
    min_levels    INTEGER NOT NULL,
    PRIMARY KEY (ancestor_id, descendant_id, predicate)
);

-- --------------------------------------------------------------- OMOP layer
CREATE TABLE IF NOT EXISTS omop_concept (
    concept_id       INTEGER,
    concept_code     TEXT NOT NULL,
    vocabulary_id    TEXT NOT NULL,
    concept_name     TEXT,
    domain_id        TEXT,
    concept_class_id TEXT,
    standard_concept TEXT,
    invalid_reason   TEXT,
    source           TEXT NOT NULL,
    resolved_at      TEXT,
    PRIMARY KEY (vocabulary_id, concept_code, source)
);

CREATE TABLE IF NOT EXISTS omop_concept_relationship (
    vocabulary_id_1 TEXT NOT NULL,
    concept_code_1  TEXT NOT NULL,
    relationship_id TEXT NOT NULL,
    vocabulary_id_2 TEXT NOT NULL,
    concept_code_2  TEXT NOT NULL,
    concept_name_2  TEXT,
    source          TEXT NOT NULL,
    resolved_at     TEXT,
    PRIMARY KEY (vocabulary_id_1, concept_code_1, relationship_id,
                 vocabulary_id_2, concept_code_2, source)
);
CREATE INDEX IF NOT EXISTS ix_ocr_rel ON omop_concept_relationship(relationship_id);

-- Facet to concept mapping. mapping_status starts 'unreviewed' for everything
-- a resolver produced: a machine string match is a candidate, not a mapping.
CREATE TABLE IF NOT EXISTS facet_concept (
    facet_id       TEXT NOT NULL REFERENCES facet(facet_id),
    vocabulary_id  TEXT NOT NULL,
    search_term    TEXT NOT NULL,
    concept_code   TEXT NOT NULL,
    concept_name   TEXT,
    concept_id     INTEGER,
    domain_id      TEXT,
    match_rank     INTEGER,
    label_match    TEXT,
    resolver       TEXT NOT NULL,
    mapping_status TEXT NOT NULL DEFAULT 'unreviewed',
    resolved_at    TEXT,
    PRIMARY KEY (facet_id, vocabulary_id, search_term, concept_code, resolver)
);

-- Whether each MeSH descriptor name declared in facets.yaml is a real NLM
-- descriptor. A term can be real yet unused in this corpus; only a term PubMed
-- does not recognise at all is a vocabulary error.
CREATE TABLE IF NOT EXISTS mesh_term_check (
    facet_id        TEXT NOT NULL,
    descriptor_name TEXT NOT NULL,
    is_valid        INTEGER,
    pubmed_count    INTEGER,
    corpus_count    INTEGER,
    translation     TEXT,
    checked_at      TEXT,
    PRIMARY KEY (facet_id, descriptor_name)
);

CREATE TABLE IF NOT EXISTS resolver_attempt (
    resolver    TEXT NOT NULL,
    target      TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT,
    attempted_at TEXT NOT NULL,
    PRIMARY KEY (resolver, target, attempted_at)
);
"""

VIEWS = """
DROP VIEW IF EXISTS v_facet_summary;
CREATE VIEW v_facet_summary AS
SELECT f.facet_id,
       f.group_id,
       f.label,
       COUNT(DISTINCT CASE WHEN af.evidence_source = 'pubmed_query' THEN af.pmid END) AS n_query_hits,
       COUNT(DISTINCT CASE WHEN af.evidence_source = 'mesh_term'    THEN af.pmid END) AS n_mesh_hits,
       COUNT(DISTINCT af.pmid) AS n_articles_any
FROM facet f
LEFT JOIN article_facet af ON af.facet_id = f.facet_id
GROUP BY f.facet_id, f.group_id, f.label;

-- Exposure x outcome co-tagging: how many articles carry each pair of tags.
-- A co-tag is not a finding; the quoted links in relation_excerpts.yaml are.
-- It is the denominator for them: where the literature is, before reading it.
DROP VIEW IF EXISTS v_exposure_outcome_cooccurrence;
CREATE VIEW v_exposure_outcome_cooccurrence AS
SELECT e.facet_id AS exposure_id,
       o.facet_id AS outcome_id,
       COUNT(DISTINCT e.pmid) AS n_articles
FROM article_facet e
JOIN facet fe ON fe.facet_id = e.facet_id AND fe.group_id = 'exposure'
JOIN article_facet o ON o.pmid = e.pmid
JOIN facet fo ON fo.facet_id = o.facet_id AND fo.group_id = 'outcome'
GROUP BY e.facet_id, o.facet_id;

-- Age strata per article, from either tag path. Pediatric is a WHERE clause on
-- this view, not a retrieval filter.
DROP VIEW IF EXISTS v_article_age_strata;
CREATE VIEW v_article_age_strata AS
SELECT a.pmid,
       a.pub_year,
       MAX(CASE WHEN af.facet_id = 'prenatal_window' THEN 1 ELSE 0 END) AS prenatal,
       MAX(CASE WHEN af.facet_id = 'infant'          THEN 1 ELSE 0 END) AS infant,
       MAX(CASE WHEN af.facet_id = 'child'           THEN 1 ELSE 0 END) AS child,
       MAX(CASE WHEN af.facet_id = 'adolescent'      THEN 1 ELSE 0 END) AS adolescent,
       MAX(CASE WHEN af.facet_id = 'adult'           THEN 1 ELSE 0 END) AS adult,
       MAX(CASE WHEN af.facet_id IN ('prenatal_window', 'infant', 'child', 'adolescent')
                THEN 1 ELSE 0 END) AS pediatric
FROM article a
LEFT JOIN article_facet af ON af.pmid = a.pmid
GROUP BY a.pmid, a.pub_year;

DROP VIEW IF EXISTS v_facet_evidence_disagreement;
CREATE VIEW v_facet_evidence_disagreement AS
SELECT f.facet_id,
       SUM(CASE WHEN q.pmid IS NOT NULL AND m.pmid IS NULL THEN 1 ELSE 0 END) AS query_only,
       SUM(CASE WHEN q.pmid IS NULL AND m.pmid IS NOT NULL THEN 1 ELSE 0 END) AS mesh_only,
       SUM(CASE WHEN q.pmid IS NOT NULL AND m.pmid IS NOT NULL THEN 1 ELSE 0 END) AS both
FROM facet f
LEFT JOIN article a ON 1 = 1
LEFT JOIN article_facet q ON q.pmid = a.pmid AND q.facet_id = f.facet_id AND q.evidence_source = 'pubmed_query'
LEFT JOIN article_facet m ON m.pmid = a.pmid AND m.facet_id = f.facet_id AND m.evidence_source = 'mesh_term'
WHERE q.pmid IS NOT NULL OR m.pmid IS NOT NULL
GROUP BY f.facet_id;

DROP VIEW IF EXISTS v_ontology_is_a_paths;
CREATE VIEW v_ontology_is_a_paths AS
SELECT c.descendant_id,
       nd.label AS descendant_label,
       c.ancestor_id,
       na.label AS ancestor_label,
       c.predicate,
       c.min_levels
FROM ontology_closure c
JOIN ontology_node nd ON nd.node_id = c.descendant_id
JOIN ontology_node na ON na.node_id = c.ancestor_id;

DROP VIEW IF EXISTS v_mapping_review_queue;
CREATE VIEW v_mapping_review_queue AS
SELECT fc.facet_id, fc.vocabulary_id, fc.search_term, fc.concept_code,
       fc.concept_name, fc.concept_id, fc.match_rank, fc.label_match,
       fc.resolver, fc.mapping_status
FROM facet_concept fc
WHERE fc.mapping_status = 'unreviewed'
ORDER BY CASE fc.label_match WHEN 'loose' THEN 0 WHEN 'contains' THEN 1 ELSE 2 END,
         fc.match_rank, fc.facet_id;

-- The rows most likely to be wrong: the best candidate for a term, whose
-- concept name does not actually contain the term. A human should look at
-- these before anything else.
DROP VIEW IF EXISTS v_loose_top_matches;
CREATE VIEW v_loose_top_matches AS
SELECT facet_id, vocabulary_id, search_term, concept_code, concept_name, resolver
FROM facet_concept
WHERE match_rank = 1 AND label_match = 'loose'
ORDER BY facet_id, vocabulary_id;
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Owns the SQLite connection and the on-disk data layout."""

    def __init__(self, data_dir: Path, db_name: str = "exposome_pubmed.sqlite"):
        self.data_dir = Path(data_dir)
        self.raw_dir = self.data_dir / "raw"
        self.db_dir = self.data_dir / "db"
        self.derived_dir = self.data_dir / "derived"
        for directory in (self.raw_dir, self.db_dir, self.derived_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.db_path = self.db_dir / db_name
        self.jsonl_path = self.data_dir / "articles.jsonl"   # append-only staging, not tracked
        self.shard_dir = self.data_dir / "articles"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.executescript(VIEWS)
        self.conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    # -- plumbing ---------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row else None

    def rows(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    # -- raw + jsonl layers ----------------------------------------------
    def write_raw(self, run_id: str, batch_index: int, raw_xml: bytes) -> Path:
        path = self.raw_dir / f"{run_id}_efetch_{batch_index:05d}.xml.gz"
        with gzip.open(path, "wb") as fh:
            fh.write(raw_xml)
        return path

    def append_jsonl(self, records: list[dict[str, Any]]) -> None:
        with self.jsonl_path.open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    SHARD_SIZE = 1_000_000

    def shard_paths(self) -> list[Path]:
        return sorted(self.shard_dir.glob("pmid_*.jsonl")) if self.shard_dir.exists() else []

    def iter_jsonl(self) -> Iterator[dict[str, Any]]:
        """Every stored record: the compacted shards, then anything staged since.

        Staged records come last, so a deduplicating reader that keeps the last
        record per PMID keeps the newest.
        """
        for path in self.shard_paths() + ([self.jsonl_path] if self.jsonl_path.exists() else []):
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        yield json.loads(line)

    @staticmethod
    def _pmid_sort_key(record: dict[str, Any]) -> tuple[int, str]:
        """Numeric PMID order, with a lexical fallback for anything unparseable."""
        pmid = str(record.get("pmid", ""))
        return (int(pmid), "") if pmid.isdigit() else (1 << 62, pmid)

    def _shard_name(self, record: dict[str, Any]) -> str:
        pmid = str(record.get("pmid", ""))
        return f"pmid_{int(pmid) // self.SHARD_SIZE:02d}.jsonl" if pmid.isdigit() else "pmid_other.jsonl"

    def compact_jsonl(self) -> tuple[int, int]:
        """Deduplicate the record layer by PMID, sort it and shard it. Returns (kept, dropped).

        Appending is cheap during a harvest but leaves duplicates behind when a
        run is repeated, and an unsorted file makes a version-controlled diff
        unreadable. Compaction is therefore a separate pass at the end of a run:
        last record for a PMID wins, output is sorted, every shard is written
        through a temporary file, and the staging file is removed only once all
        shards are in place, so an interrupted compaction loses nothing.
        """
        by_pmid: dict[str, dict[str, Any]] = {}
        total = 0
        for record in self.iter_jsonl():
            total += 1
            by_pmid[str(record.get("pmid"))] = record
        if not total:
            return (0, 0)

        shards: dict[str, list[dict[str, Any]]] = {}
        for record in sorted(by_pmid.values(), key=self._pmid_sort_key):
            shards.setdefault(self._shard_name(record), []).append(record)
        self.shard_dir.mkdir(parents=True, exist_ok=True)
        for name, records in shards.items():
            tmp_path = self.shard_dir / (name + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as fh:
                for record in records:
                    fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            tmp_path.replace(self.shard_dir / name)
        for stale in self.shard_paths():
            if stale.name not in shards:
                stale.unlink()
        if self.jsonl_path.exists():
            self.jsonl_path.unlink()
        return (len(by_pmid), total - len(by_pmid))

    def corpus_text(self) -> str:
        """The compacted record layer as one string, for determinism checks."""
        return "".join(p.read_text(encoding="utf-8") for p in self.shard_paths())

    # -- vocabulary snapshot ---------------------------------------------
    def upsert_vocabulary(self, vocab) -> None:
        """Snapshot the curated vocabulary into the database."""
        with self.transaction() as conn:
            for group_id, group in vocab.groups.items():
                conn.execute(
                    "INSERT OR REPLACE INTO facet_group(group_id, label, definition) "
                    "VALUES (?,?,?)",
                    (group_id, group["label"], " ".join(group["definition"].split())),
                )
            for facet in vocab.facet_list:
                conn.execute(
                    "INSERT OR REPLACE INTO facet"
                    "(facet_id, group_id, label, definition, pubmed_query, mesh_terms, source_refs)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        facet["id"],
                        facet["group"],
                        facet["label"],
                        " ".join(facet["definition"].split()),
                        facet["pubmed_query"],
                        json.dumps(facet["mesh_terms"]),
                        json.dumps(facet["source_refs"]),
                    ),
                )
            for ref_id, ref in vocab.seed_references.items():
                # Declared fields are refreshed from the vocabulary; resolution
                # results already in the table are preserved.
                conn.execute(
                    "INSERT INTO seed_reference"
                    "(ref_id, citation, pubmed_lookup, expected_title, expected_pmid, note)"
                    " VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(ref_id) DO UPDATE SET"
                    " citation=excluded.citation, pubmed_lookup=excluded.pubmed_lookup,"
                    " expected_title=excluded.expected_title,"
                    " expected_pmid=excluded.expected_pmid, note=excluded.note",
                    (
                        ref_id,
                        ref["citation"],
                        ref.get("pubmed_lookup"),
                        ref.get("expected_title"),
                        ref.get("expected_pmid"),
                        " ".join(ref.get("note", "").split()) or None,
                    ),
                )
            for predicate, spec in vocab.predicates.items():
                conn.execute(
                    "INSERT OR REPLACE INTO ontology_predicate"
                    "(predicate, transitive, subsumption, symmetric, inverse) VALUES (?,?,?,?,?)",
                    (
                        predicate,
                        int(bool(spec.get("transitive"))),
                        int(bool(spec.get("subsumption"))),
                        int(bool(spec.get("symmetric"))),
                        spec.get("inverse"),
                    ),
                )
            for node in vocab.nodes.values():
                conn.execute(
                    "INSERT OR REPLACE INTO ontology_node"
                    "(node_id, label, kind, group_id, definition) VALUES (?,?,?,?,?)",
                    (node["id"], node["label"], node["kind"], node["group"], node["definition"]),
                )
            # The config is the whole truth for curated edges: drop the old set first,
            # so an edge removed from relations.yaml does not linger in the database.
            conn.execute("DELETE FROM ontology_edge WHERE source = 'curated'")
            for subject, predicate, obj, provenance in vocab.edges:
                conn.execute(
                    "INSERT OR REPLACE INTO ontology_edge"
                    "(subject_id, predicate, object_id, provenance, source) VALUES (?,?,?,?, 'curated')",
                    (subject, predicate, obj, provenance),
                )
        self.rebuild_closure()

    def rebuild_closure(self) -> None:
        """Recompute the transitive closure of the transitive predicates."""
        with self.transaction() as conn:
            conn.execute("DELETE FROM ontology_closure")
            predicates = [
                r["predicate"]
                for r in conn.execute(
                    "SELECT predicate FROM ontology_predicate WHERE transitive = 1"
                )
            ]
            for predicate in predicates:
                adjacency: dict[str, set[str]] = {}
                for row in conn.execute(
                    "SELECT subject_id, object_id FROM ontology_edge WHERE predicate = ?",
                    (predicate,),
                ):
                    adjacency.setdefault(row["subject_id"], set()).add(row["object_id"])
                for start in adjacency:
                    depth = 0
                    frontier = {start}
                    best: dict[str, int] = {}
                    while frontier and depth < 32:
                        depth += 1
                        nxt: set[str] = set()
                        for node in frontier:
                            for parent in adjacency.get(node, ()):
                                if parent == start:
                                    continue
                                if parent not in best:
                                    best[parent] = depth
                                    nxt.add(parent)
                        frontier = nxt
                    for ancestor, levels in best.items():
                        conn.execute(
                            "INSERT OR REPLACE INTO ontology_closure"
                            "(ancestor_id, descendant_id, predicate, min_levels) VALUES (?,?,?,?)",
                            (ancestor, start, predicate, levels),
                        )

    # -- articles ---------------------------------------------------------
    def upsert_articles(self, records: list[dict[str, Any]], run_id: str) -> int:
        stored = 0
        now = utcnow()
        with self.transaction() as conn:
            for record in records:
                pmid = record["pmid"]
                conn.execute(
                    """
                    INSERT INTO article (
                        pmid, doi, pmc, pii, record_type, title, vernacular_title, abstract,
                        has_abstract, abstract_section_count, journal_title, journal_iso,
                        journal_nlm_id, journal_country, issn, volume, issue, pagination,
                        pub_year, pub_month, pub_day, medline_date, article_date, entrez_date,
                        medline_status, owner, indexing_method, publication_status, author_count,
                        mesh_descriptor_count, is_mesh_indexed, is_retracted, is_human_tagged,
                        is_animal_tagged, integrity_flags, first_seen_run, fetched_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(pmid) DO UPDATE SET
                        -- Re-harvesting refreshes every field to match the latest
                        -- fetch, consistent with the child tables (which are fully
                        -- rebuilt below). PubMed corrects metadata and promotes
                        -- ahead-of-print records to a real pub_year, so a subset
                        -- refresh would leave scalar columns disagreeing with the
                        -- rebuilt children. first_seen_run is the one provenance
                        -- exception: it records the first run that saw this PMID
                        -- and must never be overwritten (fetched_at carries the
                        -- latest-fetch timestamp).
                        doi=excluded.doi, pmc=excluded.pmc, pii=excluded.pii,
                        record_type=excluded.record_type, title=excluded.title,
                        vernacular_title=excluded.vernacular_title,
                        abstract=excluded.abstract, has_abstract=excluded.has_abstract,
                        abstract_section_count=excluded.abstract_section_count,
                        journal_title=excluded.journal_title, journal_iso=excluded.journal_iso,
                        journal_nlm_id=excluded.journal_nlm_id,
                        journal_country=excluded.journal_country, issn=excluded.issn,
                        volume=excluded.volume, issue=excluded.issue,
                        pagination=excluded.pagination, pub_year=excluded.pub_year,
                        pub_month=excluded.pub_month, pub_day=excluded.pub_day,
                        medline_date=excluded.medline_date, article_date=excluded.article_date,
                        entrez_date=excluded.entrez_date, medline_status=excluded.medline_status,
                        owner=excluded.owner, indexing_method=excluded.indexing_method,
                        publication_status=excluded.publication_status,
                        author_count=excluded.author_count,
                        mesh_descriptor_count=excluded.mesh_descriptor_count,
                        is_mesh_indexed=excluded.is_mesh_indexed,
                        is_retracted=excluded.is_retracted,
                        is_human_tagged=excluded.is_human_tagged,
                        is_animal_tagged=excluded.is_animal_tagged,
                        integrity_flags=excluded.integrity_flags,
                        fetched_at=excluded.fetched_at
                    """,
                    (
                        pmid, record.get("doi"), record.get("pmc"), record.get("pii"),
                        record.get("record_type"), record.get("title"),
                        record.get("vernacular_title"), record.get("abstract"),
                        int(bool(record.get("has_abstract"))),
                        record.get("abstract_section_count"), record.get("journal_title"),
                        record.get("journal_iso"), record.get("journal_nlm_id"),
                        record.get("journal_country"), record.get("issn"), record.get("volume"),
                        record.get("issue"), record.get("pagination"), record.get("pub_year"),
                        record.get("pub_month"), record.get("pub_day"), record.get("medline_date"),
                        record.get("article_date"), record.get("entrez_date"),
                        record.get("medline_status"), record.get("owner"),
                        record.get("indexing_method"), record.get("publication_status"),
                        record.get("author_count"), record.get("mesh_descriptor_count"),
                        int(bool(record.get("is_mesh_indexed"))),
                        int(bool(record.get("is_retracted"))),
                        int(bool(record.get("is_human_tagged"))),
                        int(bool(record.get("is_animal_tagged"))),
                        json.dumps(record.get("integrity_flags") or []),
                        run_id, now,
                    ),
                )

                for table in (
                    "article_language", "article_publication_type", "article_mesh",
                    "article_author", "article_chemical", "article_keyword", "article_grant",
                    "article_abstract_section", "article_integrity_link", "article_reference",
                ):
                    conn.execute(f"DELETE FROM {table} WHERE pmid = ?", (pmid,))

                conn.executemany(
                    "INSERT OR IGNORE INTO article_language(pmid, language) VALUES (?,?)",
                    [(pmid, lang) for lang in record.get("languages", [])],
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO article_publication_type(pmid, type_ui, type_name) VALUES (?,?,?)",
                    [(pmid, pt.get("ui"), pt["name"]) for pt in record.get("publication_types", [])],
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO article_mesh"
                    "(pmid, descriptor_ui, descriptor_name, descriptor_major, qualifier_ui,"
                    " qualifier_name, qualifier_major, has_qualifier) VALUES (?,?,?,?,?,?,?,?)",
                    [
                        (
                            pmid, m["descriptor_ui"], m["descriptor_name"],
                            int(bool(m["descriptor_major"])), m.get("qualifier_ui") or "",
                            m.get("qualifier_name"), int(bool(m.get("qualifier_major"))),
                            int(bool(m.get("qualifier_ui"))),
                        )
                        for m in record.get("mesh_headings", [])
                        if m.get("descriptor_ui")
                    ],
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO article_author"
                    "(pmid, position, last_name, fore_name, initials, collective_name, orcid,"
                    " affiliation, affiliation_count) VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            pmid, a["position"], a.get("last_name"), a.get("fore_name"),
                            a.get("initials"), a.get("collective_name"), a.get("orcid"),
                            a.get("affiliation"), a.get("affiliation_count"),
                        )
                        for a in record.get("authors", [])
                    ],
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO article_chemical(pmid, substance_ui, substance_name) VALUES (?,?,?)",
                    [(pmid, c.get("ui"), c["name"]) for c in record.get("chemicals", [])],
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO article_keyword(pmid, keyword, is_major) VALUES (?,?,?)",
                    [(pmid, k["term"], int(bool(k.get("major")))) for k in record.get("keywords", [])],
                )
                conn.executemany(
                    "INSERT INTO article_grant(pmid, grant_id, agency, country) VALUES (?,?,?,?)",
                    [
                        (pmid, g.get("grant_id"), g.get("agency"), g.get("country"))
                        for g in record.get("grants", [])
                    ],
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO article_abstract_section"
                    "(pmid, section_order, label, nlm_category, text) VALUES (?,?,?,?,?)",
                    [
                        (pmid, s["order"], s.get("label"), s.get("nlm_category"), s["text"])
                        for s in record.get("abstract_sections", [])
                    ],
                )
                conn.executemany(
                    "INSERT INTO article_integrity_link(pmid, ref_type, ref_source, target_pmid) VALUES (?,?,?,?)",
                    [
                        (pmid, l["ref_type"], l.get("ref_source"), l.get("target_pmid"))
                        for l in record.get("integrity_links", [])
                    ],
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO article_reference(pmid, referenced_pmid) VALUES (?,?)",
                    [(pmid, ref) for ref in record.get("reference_pmids", [])],
                )
                stored += 1
        return stored

    # -- runs -------------------------------------------------------------
    def start_run(self, run_id: str, profile: str, corpus_query: str, tool_version: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO harvest_run"
                "(run_id, started_at, query_profile, corpus_query, tool_version)"
                " VALUES (?,?,?,?,?)",
                (run_id, utcnow(), profile, corpus_query, tool_version),
            )

    def finish_run(self, run_id: str, **fields: Any) -> None:
        allowed = {
            "esearch_count", "pmids_retrieved", "articles_stored",
            "facets_run", "eutils_requests", "notes",
        }
        sets = ["finished_at = ?"]
        params: list[Any] = [utcnow()]
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key} = ?")
                params.append(value)
        params.append(run_id)
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE harvest_run SET {', '.join(sets)} WHERE run_id = ?", params
            )
