"""Validation report and derived-table export.

The report exists to answer one question a reviewer will reasonably ask: is the
corpus query silently missing things? It checks the seed papers the vocabulary
was built from, shows what each query clause contributed, and shows where the
two facet tag paths disagree.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Vocabulary
from .store import Store


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    @property
    def mark(self) -> str:
        return "PASS" if self.passed else "FAIL"


# Clause exclusivity, scoped to the most recent harvest run. Both columns must
# use the same run: the exclusivity inner count is already run-scoped
# (a2.run_id = a.run_id), so scoping n_in_corpus to the same latest run keeps
# the two columns comparable. Without this, after a re-harvest n_in_corpus
# would span every run while n_only_this_block counts within a run, and the two
# could disagree.
_QUERY_BLOCK_EXCLUSIVITY_SQL = (
    "WITH latest AS ("
    "  SELECT run_id FROM article_query_block"
    "  WHERE run_id = (SELECT run_id FROM harvest_run ORDER BY started_at DESC LIMIT 1)"
    ")"
    " SELECT b.block_id,"
    " (SELECT COUNT(*) FROM article_query_block a"
    "  WHERE a.block_id = b.block_id AND a.run_id IN (SELECT run_id FROM latest)),"
    " (SELECT COUNT(*) FROM article_query_block a"
    "  WHERE a.block_id = b.block_id AND a.run_id IN (SELECT run_id FROM latest)"
    "  AND (SELECT COUNT(*) FROM article_query_block a2"
    "       WHERE a2.pmid = a.pmid AND a2.run_id = a.run_id) = 1)"
    " FROM (SELECT DISTINCT block_id FROM article_query_block) b"
    " ORDER BY 3 DESC"
)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_No rows._\n"
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    return "\n".join(lines) + "\n"


def run_checks(store: Store, vocab: Vocabulary) -> list[Check]:
    checks: list[Check] = []

    n_articles = store.scalar("SELECT COUNT(*) FROM article") or 0
    checks.append(
        Check("corpus is non-empty", n_articles > 0, f"{n_articles} articles stored")
    )

    last_run = store.rows(
        "SELECT esearch_count, pmids_retrieved, notes FROM harvest_run"
        " ORDER BY started_at DESC LIMIT 1"
    )
    if last_run and last_run[0]["esearch_count"]:
        reported = last_run[0]["esearch_count"]
        retrieved = last_run[0]["pmids_retrieved"]
        try:
            truncated_to = json.loads(last_run[0]["notes"] or "{}").get("truncated_to")
        except (ValueError, TypeError):
            truncated_to = None
        if truncated_to is not None:
            # A deliberately limited run (--limit) is not an incomplete harvest.
            # Failing here would be a known-reason failure, which trains people
            # to ignore failures.
            checks.append(
                Check(
                    "every PMID esearch reported was retrieved",
                    retrieved == truncated_to,
                    f"esearch reported {reported}, run intentionally truncated to "
                    f"{truncated_to} (--limit); retrieved {retrieved}",
                )
            )
        else:
            checks.append(
                Check(
                    "every PMID esearch reported was retrieved",
                    retrieved == reported,
                    f"esearch reported {reported}, retrieved {retrieved}",
                )
            )

    # Seed papers. Two independent checks per seed: the declared expected_pmid
    # must be in the corpus, and the lookup query must still resolve to it. The
    # medRxiv preprint is expected to be absent from PubMed entirely.
    for ref_id, ref in vocab.seed_references.items():
        row = store.rows("SELECT * FROM seed_reference WHERE ref_id = ?", (ref_id,))
        record = row[0] if row else None
        resolved = record["resolved_pmid"] if record else None
        method = record["match_method"] if record else None
        hits = record["lookup_hits"] if record else None
        expected_pmid = ref.get("expected_pmid")
        expected_in_corpus = bool(record["expected_pmid_in_corpus"]) if record else False

        if "medRxiv" in (ref.get("note") or ""):
            absent = not (record and record["found_in_corpus"])
            checks.append(
                Check(
                    f"seed {ref_id} (preprint, expected absent)",
                    absent,
                    "not indexed in PubMed; absence is expected, not a defect"
                    if absent
                    else f"unexpectedly present as PMID {resolved}",
                )
            )
            continue

        checks.append(
            Check(
                f"seed {ref_id} is in the corpus",
                expected_in_corpus,
                f"expected PMID {expected_pmid} "
                + ("found" if expected_in_corpus else "MISSING from corpus"),
            )
        )
        checks.append(
            Check(
                f"seed {ref_id} lookup query still resolves correctly",
                bool(resolved) and resolved == expected_pmid,
                f"lookup returned {hits} hits, resolved to {resolved or 'nothing'} "
                f"by {method}; expected {expected_pmid}",
            )
        )

    empty_facets = [
        row["facet_id"]
        for row in store.rows(
            "SELECT facet_id FROM v_facet_summary WHERE n_articles_any = 0"
        )
    ]
    checks.append(
        Check(
            "every facet tagged at least one article",
            not empty_facets,
            "all facets non-empty" if not empty_facets else f"empty: {', '.join(empty_facets)}",
        )
    )

    candidate_rows = store.scalar("SELECT COUNT(*) FROM facet_concept") or 0
    facets_with_concepts = store.scalar(
        "SELECT COUNT(DISTINCT facet_id) FROM facet_concept"
    ) or 0
    total_facets = store.scalar("SELECT COUNT(*) FROM facet") or 0
    checks.append(
        Check(
            "every facet has at least one candidate concept",
            facets_with_concepts == total_facets,
            f"{facets_with_concepts} of {total_facets} facets mapped, "
            f"{candidate_rows} candidate rows",
        )
    )

    # A term the vocabulary marks fallback_gap is expected to miss until an
    # ATHENA bundle is supplied; anything else that missed is a real problem.
    declared_gaps = {
        (facet["id"], concept["search_term"])
        for facet in vocab.facet_list
        for concept in facet["concepts"]
        if concept.get("fallback_gap")
    }
    resolved_terms = {
        (row["facet_id"], row["search_term"])
        for row in store.rows("SELECT DISTINCT facet_id, search_term FROM facet_concept")
    }
    all_terms = {
        (facet["id"], concept["search_term"])
        for facet in vocab.facet_list
        for concept in facet["concepts"]
    }
    unexpected_misses = sorted((all_terms - resolved_terms) - declared_gaps)
    checks.append(
        Check(
            "no undeclared concept-resolution misses",
            not unexpected_misses,
            f"{len(declared_gaps)} declared fallback gaps, "
            + (
                "no undeclared misses"
                if not unexpected_misses
                else f"undeclared: {unexpected_misses[:5]}"
            ),
        )
    )
    still_gapped = sorted(declared_gaps - resolved_terms)
    checks.append(
        Check(
            "declared fallback gaps are still gaps (informational)",
            True,
            f"{len(still_gapped)} of {len(declared_gaps)} declared gaps unresolved by the "
            "public fallback, as expected; supply --athena-dir to close them",
        )
    )

    dangling = store.scalar(
        "SELECT COUNT(*) FROM ontology_edge e"
        " LEFT JOIN ontology_node s ON s.node_id = e.subject_id"
        " LEFT JOIN ontology_node o ON o.node_id = e.object_id"
        " WHERE s.node_id IS NULL OR o.node_id IS NULL"
    ) or 0
    checks.append(
        Check("ontology graph has no dangling edges", dangling == 0, f"{dangling} dangling")
    )

    self_loops = store.scalar(
        "SELECT COUNT(*) FROM ontology_closure WHERE ancestor_id = descendant_id"
    ) or 0
    checks.append(
        Check("no node is its own ancestor", self_loops == 0, f"{self_loops} self-loops")
    )

    vocab_problems = vocab.validate()
    checks.append(
        Check(
            "curated vocabulary is internally consistent",
            not vocab_problems,
            "clean" if not vocab_problems else "; ".join(vocab_problems[:5]),
        )
    )

    n_checked = store.scalar("SELECT COUNT(*) FROM mesh_term_check") or 0
    if n_checked:
        invalid = store.rows(
            "SELECT facet_id, descriptor_name FROM mesh_term_check WHERE is_valid = 0"
            " ORDER BY facet_id LIMIT 6"
        )
        unused = store.scalar(
            "SELECT COUNT(*) FROM mesh_term_check WHERE is_valid = 1 AND corpus_count = 0"
        ) or 0
        checks.append(
            Check(
                "every declared MeSH descriptor is a real NLM descriptor",
                not invalid,
                f"{n_checked} descriptors checked against PubMed's MeSH index; "
                + (
                    f"{unused} are valid but unused in this corpus"
                    if not invalid
                    else "invalid: "
                    + ", ".join(f"{r['facet_id']}:{r['descriptor_name']}" for r in invalid)
                ),
            )
        )
    else:
        checks.append(
            Check(
                "every declared MeSH descriptor is a real NLM descriptor",
                False,
                "not checked; run `exposome-ehr check-mesh`",
            )
        )

    return checks


def export_derived(store: Store, vocab: Vocabulary) -> list[Path]:
    """Write the small, reviewable tables as TSV for version control."""
    out_dir = store.derived_dir
    exports: dict[str, tuple[list[str], str]] = {
        "facet_summary.tsv": (
            ["facet_id", "group_id", "label", "n_query_hits", "n_mesh_hits", "n_articles_any"],
            "SELECT facet_id, group_id, label, n_query_hits, n_mesh_hits, n_articles_any"
            " FROM v_facet_summary ORDER BY group_id, facet_id",
        ),
        "facet_evidence_disagreement.tsv": (
            ["facet_id", "query_only", "mesh_only", "both"],
            "SELECT facet_id, query_only, mesh_only, both"
            " FROM v_facet_evidence_disagreement ORDER BY facet_id",
        ),
        "exposure_outcome_cooccurrence.tsv": (
            ["exposure_id", "outcome_id", "n_articles"],
            "SELECT exposure_id, outcome_id, n_articles FROM v_exposure_outcome_cooccurrence"
            " ORDER BY n_articles DESC, exposure_id, outcome_id",
        ),
        "age_strata.tsv": (
            ["prenatal", "infant", "child", "adolescent", "adult", "n_articles"],
            "SELECT prenatal, infant, child, adolescent, adult, COUNT(*) FROM v_article_age_strata"
            " GROUP BY prenatal, infant, child, adolescent, adult ORDER BY COUNT(*) DESC",
        ),
        "ontology_nodes.tsv": (
            ["node_id", "label", "kind", "group_id", "definition"],
            "SELECT node_id, label, kind, group_id, definition FROM ontology_node ORDER BY kind, node_id",
        ),
        "ontology_edges.tsv": (
            ["subject_id", "predicate", "object_id", "provenance", "source"],
            "SELECT subject_id, predicate, object_id, provenance, source FROM ontology_edge"
            " ORDER BY predicate, subject_id",
        ),
        "ontology_closure.tsv": (
            ["descendant_id", "predicate", "ancestor_id", "min_levels"],
            "SELECT descendant_id, predicate, ancestor_id, min_levels FROM ontology_closure"
            " ORDER BY predicate, descendant_id, min_levels",
        ),
        "facet_concepts.tsv": (
            ["facet_id", "vocabulary_id", "search_term", "concept_code", "concept_name",
             "concept_id", "domain_id", "match_rank", "label_match", "resolver",
             "mapping_status"],
            "SELECT facet_id, vocabulary_id, search_term, concept_code, concept_name,"
            " concept_id, domain_id, match_rank, label_match, resolver, mapping_status"
            " FROM facet_concept ORDER BY facet_id, vocabulary_id, match_rank",
        ),
        "mapping_review_priority.tsv": (
            ["facet_id", "vocabulary_id", "search_term", "concept_code", "concept_name", "resolver"],
            "SELECT facet_id, vocabulary_id, search_term, concept_code, concept_name, resolver"
            " FROM v_loose_top_matches",
        ),
        "omop_concept_relationships.tsv": (
            ["vocabulary_id_1", "concept_code_1", "relationship_id", "vocabulary_id_2",
             "concept_code_2", "concept_name_2", "source"],
            "SELECT vocabulary_id_1, concept_code_1, relationship_id, vocabulary_id_2,"
            " concept_code_2, concept_name_2, source FROM omop_concept_relationship"
            " ORDER BY vocabulary_id_1, concept_code_1, relationship_id",
        ),
        "corpus_by_year.tsv": (
            ["pub_year", "n_articles"],
            "SELECT pub_year, COUNT(*) FROM article GROUP BY pub_year ORDER BY pub_year",
        ),
        "publication_types.tsv": (
            ["type_name", "n_articles"],
            "SELECT type_name, COUNT(DISTINCT pmid) FROM article_publication_type"
            " GROUP BY type_name ORDER BY 2 DESC",
        ),
        "top_mesh_descriptors.tsv": (
            ["descriptor_name", "n_articles"],
            "SELECT descriptor_name, COUNT(DISTINCT pmid) FROM article_mesh"
            " GROUP BY descriptor_name ORDER BY 2 DESC LIMIT 200",
        ),
        "query_block_contribution.tsv": (
            ["run_id", "block_id", "esearch_count", "n_in_corpus"],
            "SELECT qb.run_id, qb.block_id, qb.esearch_count,"
            " (SELECT COUNT(*) FROM article_query_block aqb"
            "  WHERE aqb.run_id = qb.run_id AND aqb.block_id = qb.block_id)"
            " FROM query_block qb ORDER BY qb.run_id, qb.block_id",
        ),
        "query_block_exclusivity.tsv": (
            ["block_id", "n_in_corpus", "n_only_this_block"],
            _QUERY_BLOCK_EXCLUSIVITY_SQL,
        ),
        "mesh_term_check.tsv": (
            ["facet_id", "descriptor_name", "is_valid", "pubmed_count", "corpus_count"],
            "SELECT facet_id, descriptor_name, is_valid, pubmed_count, corpus_count"
            " FROM mesh_term_check ORDER BY facet_id, descriptor_name",
        ),
        "resolver_attempts.tsv": (
            ["resolver", "status", "n"],
            "SELECT resolver, status, COUNT(*) FROM resolver_attempt"
            " GROUP BY resolver, status ORDER BY resolver, status",
        ),
    }
    written: list[Path] = []
    for filename, (headers, sql) in exports.items():
        path = out_dir / filename
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
            writer.writerow(headers)
            for row in store.rows(sql):
                writer.writerow(["" if v is None else v for v in tuple(row)])
        written.append(path)
    return written


def build_report(store: Store, vocab: Vocabulary) -> str:
    checks = run_checks(store, vocab)
    run = store.rows("SELECT * FROM harvest_run ORDER BY started_at DESC LIMIT 1")
    run_row = run[0] if run else None

    parts: list[str] = ["# Exposome / EHR PubMed harvest report\n"]

    if run_row:
        parts.append(
            _table(
                ["field", "value"],
                [
                    ["run_id", run_row["run_id"]],
                    ["started_at", run_row["started_at"]],
                    ["finished_at", run_row["finished_at"]],
                    ["query_profile", run_row["query_profile"]],
                    ["esearch_count", run_row["esearch_count"]],
                    ["pmids_retrieved", run_row["pmids_retrieved"]],
                    ["articles_stored", run_row["articles_stored"]],
                    ["facet_queries_run", run_row["facets_run"]],
                    ["eutils_requests", run_row["eutils_requests"]],
                    ["tool_version", run_row["tool_version"]],
                ],
            )
        )

    failed = [c for c in checks if not c.passed]
    parts.append(f"\n## Checks\n\n{len(checks) - len(failed)} of {len(checks)} passed.\n\n")
    parts.append(
        _table(
            ["result", "check", "detail"],
            [[c.mark, c.name, c.detail] for c in checks],
        )
    )

    parts.append("\n## Corpus query clause contribution\n\n")
    parts.append(
        "`esearch_count` is the clause's hit count across all of PubMed; "
        "`n_in_corpus` is how many of those ended up in this corpus.\n\n"
    )
    parts.append(
        _table(
            ["block_id", "esearch_count", "n_in_corpus", "rationale"],
            [
                [
                    row["block_id"],
                    row["esearch_count"],
                    store.scalar(
                        "SELECT COUNT(*) FROM article_query_block WHERE run_id = ? AND block_id = ?",
                        (row["run_id"], row["block_id"]),
                    ),
                    (row["rationale"] or "")[:110],
                ]
                for row in store.rows("SELECT * FROM query_block ORDER BY block_id")
            ],
        )
    )

    parts.append("\n### Clause exclusivity\n\n")
    parts.append(
        "`n_only_this_block` counts records that NO other clause retrieved. A clause with a "
        "large exclusive count is carrying the corpus on its own, which is where recall is "
        "won and where off-target noise enters.\n\n"
    )
    parts.append(
        _table(
            ["block_id", "n_in_corpus", "n_only_this_block"],
            [
                [row[0], row[1], row[2]]
                for row in store.rows(_QUERY_BLOCK_EXCLUSIVITY_SQL)
            ],
        )
    )

    parts.append("\n## Corpus composition\n\n")
    total = store.scalar("SELECT COUNT(*) FROM article") or 0
    parts.append(
        _table(
            ["metric", "n", "share"],
            [
                [label, value, f"{(value / total * 100):.1f}%" if total else "-"]
                for label, value in [
                    ["articles", total],
                    ["with abstract", store.scalar("SELECT COUNT(*) FROM article WHERE has_abstract = 1")],
                    ["MeSH indexed", store.scalar("SELECT COUNT(*) FROM article WHERE is_mesh_indexed = 1")],
                    ["human check-tag", store.scalar("SELECT COUNT(*) FROM article WHERE is_human_tagged = 1")],
                    ["animal check-tag", store.scalar("SELECT COUNT(*) FROM article WHERE is_animal_tagged = 1")],
                    ["retracted", store.scalar("SELECT COUNT(*) FROM article WHERE is_retracted = 1")],
                    ["has DOI", store.scalar("SELECT COUNT(*) FROM article WHERE doi IS NOT NULL")],
                    ["has PMC id", store.scalar("SELECT COUNT(*) FROM article WHERE pmc IS NOT NULL")],
                    ["non-English", store.scalar(
                        "SELECT COUNT(DISTINCT pmid) FROM article_language WHERE language <> 'eng'"
                    )],
                ]
            ],
        )
    )

    parts.append("\n### Publication types, top 15\n\n")
    parts.append(
        _table(
            ["type_name", "n_articles"],
            [
                [row[0], row[1]]
                for row in store.rows(
                    "SELECT type_name, COUNT(DISTINCT pmid) FROM article_publication_type"
                    " GROUP BY type_name ORDER BY 2 DESC LIMIT 15"
                )
            ],
        )
    )

    parts.append("\n## Facet coverage\n\n")
    parts.append(
        "`query` counts title/abstract query hits inside the corpus; `mesh` counts records "
        "NLM indexed with one of the facet's MeSH descriptors. The two paths are independent.\n\n"
    )
    parts.append(
        _table(
            ["group", "facet_id", "query", "mesh", "any"],
            [
                [row["group_id"], row["facet_id"], row["n_query_hits"], row["n_mesh_hits"], row["n_articles_any"]]
                for row in store.rows(
                    "SELECT * FROM v_facet_summary ORDER BY group_id, n_articles_any DESC"
                )
            ],
        )
    )

    parts.append("\n## Age strata\n\n")
    parts.append(
        "Articles by life stage, from either tag path. Age is a tag, not a filter: "
        "the pediatric corpus is the rows with any of prenatal, infant, child or "
        "adolescent.\n\n"
    )
    total = store.scalar("SELECT COUNT(*) FROM v_article_age_strata") or 0
    parts.append(
        _table(
            ["stratum", "n_articles"],
            [[col, store.scalar(f"SELECT SUM({col}) FROM v_article_age_strata") or 0]
             for col in ("prenatal", "infant", "child", "adolescent", "adult", "pediatric")]
            + [["no age tag", store.scalar(
                "SELECT COUNT(*) FROM v_article_age_strata WHERE pediatric = 0 AND adult = 0") or 0],
               ["all articles", total]],
        )
    )

    parts.append("\n## Most co-tagged exposure-outcome pairs\n\n")
    parts.append(
        "Articles tagged with both an exposure and an outcome facet. A co-tag says "
        "where the literature is, not what it found; the quoted links do that.\n\n"
    )
    parts.append(
        _table(
            ["exposure", "outcome", "n_articles"],
            [[r["exposure_id"], r["outcome_id"], r["n_articles"]] for r in store.rows(
                "SELECT * FROM v_exposure_outcome_cooccurrence"
                " ORDER BY n_articles DESC, exposure_id, outcome_id LIMIT 25")],
        )
    )

    parts.append("\n## Declared MeSH descriptors\n\n")
    mesh_checked = store.scalar("SELECT COUNT(*) FROM mesh_term_check") or 0
    if mesh_checked:
        parts.append(
            _table(
                ["status", "n_descriptors"],
                [
                    ["real descriptor, used in this corpus", store.scalar(
                        "SELECT COUNT(*) FROM mesh_term_check WHERE is_valid = 1 AND corpus_count > 0"
                    )],
                    ["real descriptor, unused in this corpus", store.scalar(
                        "SELECT COUNT(*) FROM mesh_term_check WHERE is_valid = 1 AND corpus_count = 0"
                    )],
                    ["not recognised by PubMed", store.scalar(
                        "SELECT COUNT(*) FROM mesh_term_check WHERE is_valid = 0"
                    )],
                ],
            )
        )
        unused_rows = store.rows(
            "SELECT facet_id, descriptor_name, pubmed_count FROM mesh_term_check"
            " WHERE is_valid = 1 AND corpus_count = 0 ORDER BY facet_id, descriptor_name"
        )
        if unused_rows:
            parts.append(
                "\nValid descriptors that no record in this corpus carries. Not errors: they "
                "mean no paper in this corpus is indexed that way, which is itself worth knowing when "
                "choosing between a MeSH-based and a text-based phenotype definition.\n\n"
            )
            parts.append(
                _table(
                    ["facet_id", "descriptor_name", "n_in_all_of_pubmed"],
                    [[r["facet_id"], r["descriptor_name"], r["pubmed_count"]] for r in unused_rows],
                )
            )
    else:
        parts.append("_Not checked. Run `exposome-ehr check-mesh`._\n")

    parts.append("\n## Ontology graph\n\n")
    parts.append(
        _table(
            ["predicate", "n_edges", "transitive", "subsumption"],
            [
                [
                    row["predicate"], row["n"],
                    "yes" if row["transitive"] else "no",
                    "yes" if row["subsumption"] else "no",
                ]
                for row in store.rows(
                    "SELECT p.predicate, p.transitive, p.subsumption,"
                    " (SELECT COUNT(*) FROM ontology_edge e WHERE e.predicate = p.predicate) AS n"
                    " FROM ontology_predicate p ORDER BY n DESC"
                )
            ],
        )
    )
    parts.append(
        f"\nNodes: {store.scalar('SELECT COUNT(*) FROM ontology_node')}. "
        f"Edges: {store.scalar('SELECT COUNT(*) FROM ontology_edge')}. "
        f"Closure rows: {store.scalar('SELECT COUNT(*) FROM ontology_closure')}.\n"
    )

    parts.append("\n## OMOP concept resolution\n\n")
    parts.append(
        _table(
            ["resolver", "status", "n_attempts"],
            [
                [row[0], row[1], row[2]]
                for row in store.rows(
                    "SELECT resolver, status, COUNT(*) FROM resolver_attempt"
                    " GROUP BY resolver, status ORDER BY resolver, status"
                )
            ],
        )
    )
    parts.append(
        _table(
            ["vocabulary_id", "n_candidates", "n_facets", "n_with_concept_id"],
            [
                [row[0], row[1], row[2], row[3]]
                for row in store.rows(
                    "SELECT vocabulary_id, COUNT(*), COUNT(DISTINCT facet_id),"
                    " SUM(CASE WHEN concept_id IS NOT NULL THEN 1 ELSE 0 END)"
                    " FROM facet_concept GROUP BY vocabulary_id ORDER BY 2 DESC"
                )
            ],
        )
    )
    parts.append("\n### Match quality of candidate mappings\n\n")
    parts.append(
        "Token-based: `exact` means the same set of meaningful words once case, "
        "punctuation, SNOMED's semantic tag, LOINC's unit brackets and British spellings "
        "are normalised away. `contains` means every word of the search term appears in the "
        "concept name, usually a more specifically named form of the same thing. `loose` "
        "means at least one word is missing, so the service matched on something else.\n\n"
    )
    parts.append(
        _table(
            ["label_match", "n_candidates", "n_rank_1"],
            [
                [row[0], row[1], row[2]]
                for row in store.rows(
                    "SELECT COALESCE(label_match, 'unknown'), COUNT(*),"
                    " SUM(CASE WHEN match_rank = 1 THEN 1 ELSE 0 END)"
                    " FROM facet_concept GROUP BY 1 ORDER BY 2 DESC"
                )
            ],
        )
    )
    loose = store.rows("SELECT * FROM v_loose_top_matches")
    if loose:
        parts.append(
            f"\n{len(loose)} best-candidate rows are loose matches and should be reviewed "
            "first. They are exported to `data/derived/mapping_review_priority.tsv`.\n\n"
        )
        parts.append(
            _table(
                ["facet_id", "vocabulary", "search_term", "concept_code", "concept_name"],
                [
                    [row["facet_id"], row["vocabulary_id"], row["search_term"],
                     row["concept_code"], row["concept_name"]]
                    for row in loose
                ],
            )
        )

    n_unreviewed = store.scalar(
        "SELECT COUNT(*) FROM facet_concept WHERE mapping_status = 'unreviewed'"
    ) or 0
    parts.append(
        f"\n{n_unreviewed} candidate mappings are `unreviewed`. None of them should be "
        "treated as a phenotype definition until a human has checked them against ATHENA.\n"
    )

    gaps = [
        [facet["id"], concept["vocabulary"], concept["search_term"]]
        for facet in vocab.facet_list
        for concept in facet["concepts"]
        if concept.get("fallback_gap")
    ]
    if gaps:
        parts.append("\n### Declared fallback gaps\n\n")
        parts.append(
            "These search terms are correct against full SNOMED but unreachable through the "
            "public fallback, because the EBI Ontology Lookup Service carries only a subset "
            "of SNOMED. They resolve once an ATHENA bundle is supplied with `--athena-dir`.\n\n"
        )
        parts.append(_table(["facet_id", "vocabulary", "search_term"], gaps))

    unmapped = store.rows(
        "SELECT facet_id FROM facet WHERE facet_id NOT IN (SELECT facet_id FROM facet_concept)"
    )
    if unmapped:
        parts.append(
            "\nFacets with no candidate concept: "
            + ", ".join(row["facet_id"] for row in unmapped)
            + "\n"
        )

    if failed:
        parts.append("\n## Failing checks\n\n")
        for check in failed:
            parts.append(f"- **{check.name}**: {check.detail}\n")

    return "".join(parts)
