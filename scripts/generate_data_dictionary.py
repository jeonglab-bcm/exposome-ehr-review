#!/usr/bin/env python3
"""Generate docs/DATA_DICTIONARY.md from the live SQLite schema.

Run after a harvest so the row counts match what is committed:

    PYTHONPATH=src python3 scripts/generate_data_dictionary.py > docs/DATA_DICTIONARY.md
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import textwrap

TABLE_NOTES = {
    "schema_meta": "Schema version marker.",
    "harvest_run": "One row per harvest. The provenance anchor for everything else.",
    "query_block": "One row per named clause of the corpus query, with its PubMed hit count and query translation.",
    "article_query_block": "Which clause of the corpus query retrieved each record. A record can be attributed to several clauses.",
    "article": "One row per PubMed record. No filtering: language, publication type, retraction status and species check-tags are stored, not applied.",
    "article_language": "Languages declared on the record.",
    "article_publication_type": "PubMed publication types, e.g. Journal Article, Case Reports, Review, Retracted Publication.",
    "article_mesh": "One row per descriptor/qualifier pair. A heading with no qualifier stores qualifier_ui = '' and has_qualifier = 0.",
    "article_author": "Authors in byline order, with the first affiliation and ORCID where given.",
    "article_chemical": "NLM chemical substance headings.",
    "article_keyword": "Author-supplied keywords, not MeSH.",
    "article_grant": "Funding acknowledgements as indexed.",
    "article_abstract_section": "Structured abstract sections with their labels and NLM categories, kept separately from the concatenated abstract.",
    "article_integrity_link": "CommentsCorrections links: retractions, errata, expressions of concern.",
    "article_reference": "Reference PMIDs where the publisher supplied them. Coverage is partial.",
    "facet_group": "The six facet groups: exposure, outcome, data_source, assessment, design, population.",
    "facet": "Snapshot of the curated facet vocabulary as of the run.",
    "facet_query_run": "One row per facet query executed: PubMed hit count, how many landed inside the corpus, and the query translation.",
    "article_facet": "The tag join. evidence_source is 'pubmed_query' or 'mesh_term'; both paths are recorded because they disagree.",
    "seed_reference": "The papers the vocabulary was built from, with their lookup query, expected PMID and how resolution went.",
    "ontology_node": "Curated graph nodes: the 41 facets plus the abstract classes.",
    "ontology_predicate": "Predicate declarations, including whether each is transitive and subsumption-like.",
    "ontology_edge": "Curated graph edges. source = 'curated' for everything written from relations.yaml.",
    "ontology_closure": "Materialised transitive closure of the transitive predicates, one predicate per row, with the shortest path length.",
    "omop_concept": "Concepts returned by a resolver. concept_id is populated only from an ATHENA bundle; the public fallbacks return codes without it.",
    "omop_concept_relationship": "Terminology-derived relationships, kept entirely separate from the curated graph.",
    "facet_concept": "Candidate facet-to-concept mappings. Everything starts as mapping_status = 'unreviewed'.",
    "resolver_attempt": "Every resolver call: ok, no_match or error. This is how the report says which sources actually ran.",
    "mesh_term_check": "Whether each declared MeSH descriptor is a real NLM descriptor, and whether the corpus uses it.",
}

VIEW_NOTES = {
    "v_facet_summary": "Per facet: articles found by query, by MeSH indexing, and by either.",
    "v_exposure_outcome_cooccurrence": "Articles carrying each exposure facet together with each outcome facet. Where the literature is, not what it found.",
    "v_article_age_strata": "One row per article with the age-stratum tags as 0/1 columns and a derived pediatric flag; the pediatric corpus is a WHERE clause on it.",
    "v_facet_evidence_disagreement": "Per facet: how many articles only the query found, only MeSH indexing found, and both.",
    "v_ontology_is_a_paths": "The closure joined to node labels, for readable ancestor listings.",
    "v_mapping_review_queue": "Unreviewed candidate mappings, loose matches first.",
    "v_loose_top_matches": "The best candidate for a term whose concept name does not contain the term. Review these first.",
}

COLUMN_NOTES = {
    ("article", "has_abstract"): "Derived, so 'papers with an abstract' does not need a NULL test.",
    ("article", "medline_date"): "PubMed's free-text date, e.g. '2019 Sep-Oct', kept verbatim alongside the parsed parts.",
    ("article", "entrez_date"): "When PubMed received the record, which is not the publication date.",
    ("article", "is_mesh_indexed"): "False for in-process and publisher-supplied records. About a quarter of this corpus.",
    ("article", "is_retracted"): "Set from a RetractionIn CommentsCorrections link, not from the publication type alone.",
    ("article", "integrity_flags"): 'JSON array, e.g. ["has_erratum"]. Errata are not retractions.',
    ("article", "first_seen_run"): "Never overwritten by a later run.",
    ("article_facet", "evidence_source"): "'pubmed_query' or 'mesh_term'.",
    ("article_facet", "detail"): "The run id for a query hit; the matched descriptor names for a MeSH hit.",
    ("article_mesh", "qualifier_ui"): "'' rather than NULL when absent, because SQLite does not treat NULLs as equal inside a primary key.",
    ("facet_concept", "label_match"): "'exact', 'contains', 'loose' or 'unknown'. Token-based, normalising case, punctuation, SNOMED semantic tags, LOINC unit brackets, British spellings, terminology abbreviations and simple plurals.",
    ("facet_concept", "mapping_status"): "'unreviewed' for everything a resolver produced. Change it only after a human check.",
    ("facet_concept", "concept_id"): "NULL unless resolved from an ATHENA bundle. The public fallbacks cannot supply it.",
    ("omop_concept", "source"): "The resolver name: athena_bundle, athena_api, ols4, nlm_clinical_tables or rxnav.",
    ("ontology_closure", "min_levels"): "Shortest path length. The graph is a poly-hierarchy, so a node can reach an ancestor by several paths.",
    ("seed_reference", "match_method"): "'title_exact', 'unique_hit', 'ambiguous' or 'not_found'. Anything but the first two means the lookup did not resolve.",
    ("seed_reference", "expected_pmid_in_corpus"): "Checked directly against the article table, independently of the lookup query.",
    ("mesh_term_check", "is_valid"): "True only when the query translation still names the MeSH Terms field. PubMed silently falls back to free text for a descriptor that does not exist.",
}

FILES = [
    ("data/raw/*.xml.gz", "no", "Every efetch response verbatim. Re-parsing needs no network."),
    ("data/articles/pmid_NN.jsonl", "yes", "One JSON object per article: the corpus itself, in shards of one million PMIDs. Compacted at the end of each harvest (deduplicated by PMID, sorted numerically) so a re-run diffs readably. Everything else under data/ is derived from it."),
    ("data/articles.jsonl", "no", "Append-only staging during a harvest; folded into the shards and removed at the end."),
    ("data/db/exposome_pubmed.sqlite", "no", "The normalized database. Disposable."),
    ("data/derived/*.tsv", "yes", "Small reviewable outputs: facet counts, the relation graph and its closure, candidate mappings, corpus composition."),
    ("docs/HARVEST_REPORT.md", "yes", "Generated validation report for the last run."),
]


def main(db_path: str = "data/db/exposome_pubmed.sqlite") -> int:
    path = pathlib.Path(db_path)
    if not path.exists():
        print(f"no database at {path}; run a harvest first", file=sys.stderr)
        return 1
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    out: list[str] = ["# Data dictionary\n"]
    out.append(
        textwrap.fill(
            "Generated from the live SQLite schema by "
            "`scripts/generate_data_dictionary.py`. The schema itself is defined in "
            "`src/exposome_ehr/store.py`. The structure below — tables, columns, "
            "types and row counts — comes from the schema and cannot drift from it. "
            "The prose notes are hand-curated in the script, so a newly added column "
            "renders with a blank note (the generator warns on stderr) and stale note "
            "text is not detected automatically.",
            79,
        )
    )
    out.append(
        "\n\nThe database is disposable: it is rebuilt from `data/articles/` by "
        "`exposome-ehr rebuild`, with no network access. The row counts below are "
        "only as current as the last `make report` run before the enclosing commit.\n"
    )

    names = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    groups = {
        "Provenance": ["harvest_run", "query_block", "article_query_block", "schema_meta"],
        "Articles": [n for n in names if n.startswith("article") and n != "article_query_block"],
        "Facets": [
            "facet_group", "facet", "facet_query_run", "article_facet",
            "seed_reference", "mesh_term_check",
        ],
        "Ontology": ["ontology_node", "ontology_predicate", "ontology_edge", "ontology_closure"],
        "OMOP concepts": [
            "omop_concept", "omop_concept_relationship", "facet_concept", "resolver_attempt",
        ],
    }
    placed = {n for g in groups.values() for n in g}
    leftover = [n for n in names if n not in placed]
    if leftover:
        groups["Other"] = leftover

    out.append("\n## Row counts in the committed run\n\n| table | rows |\n| --- | --- |\n")
    for name in names:
        count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        out.append(f"| `{name}` | {count:,} |\n")

    for group, tables in groups.items():
        out.append(f"\n## {group}\n\n")
        for name in tables:
            if name not in names:
                continue
            out.append(f"### `{name}`\n\n")
            note = TABLE_NOTES.get(name)
            if note:
                out.append(textwrap.fill(note, 79) + "\n\n")
            else:
                # Column notes are selective, but every table is meant to carry
                # one. A missing table note means the schema grew a table the
                # curated prose hasn't caught up with.
                print(f"warning: no TABLE_NOTES entry for {name}", file=sys.stderr)
            out.append("| column | type | null | notes |\n| --- | --- | --- | --- |\n")
            for col in conn.execute(f"PRAGMA table_info({name})"):
                pk = " (pk)" if col["pk"] else ""
                nullable = "no" if col["notnull"] or col["pk"] else "yes"
                cnote = COLUMN_NOTES.get((name, col["name"]), "")
                out.append(
                    f"| `{col['name']}`{pk} | {col['type'] or 'TEXT'} | {nullable} | {cnote} |\n"
                )
            out.append("\n")

    out.append("\n## Views\n\n")
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"):
        out.append(f"### `{row['name']}`\n\n")
        note = VIEW_NOTES.get(row["name"])
        if note:
            out.append(textwrap.fill(note, 79) + "\n\n")
        else:
            print(f"warning: no VIEW_NOTES entry for {row['name']}", file=sys.stderr)

    out.append("\n## Files on disk\n\n| path | tracked in git | contents |\n| --- | --- | --- |\n")
    for file_path, tracked, desc in FILES:
        out.append(f"| `{file_path}` | {tracked} | {desc} |\n")

    conn.close()
    sys.stdout.write("".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
