# Data dictionary
Generated from the live SQLite schema by `scripts/generate_data_dictionary.py`.
The schema itself is defined in `src/exposome_ehr/store.py`. The structure
below — tables, columns, types and row counts — comes from the schema and
cannot drift from it. The prose notes are hand-curated in the script, so a
newly added column renders with a blank note (the generator warns on stderr)
and stale note text is not detected automatically.

The database is disposable: it is rebuilt from `data/articles/` by `exposome-ehr rebuild`, with no network access. The row counts below are only as current as the last `make report` run before the enclosing commit.

## Row counts in the committed run

| table | rows |
| --- | --- |
| `article` | 15,913 |
| `article_abstract_section` | 46,967 |
| `article_author` | 121,703 |
| `article_chemical` | 14,246 |
| `article_facet` | 103,582 |
| `article_grant` | 19,144 |
| `article_integrity_link` | 1,749 |
| `article_keyword` | 54,148 |
| `article_language` | 15,945 |
| `article_mesh` | 190,885 |
| `article_publication_type` | 28,054 |
| `article_query_block` | 16,160 |
| `article_reference` | 390,111 |
| `facet` | 67 |
| `facet_concept` | 181 |
| `facet_group` | 6 |
| `facet_query_run` | 67 |
| `harvest_run` | 1 |
| `mesh_term_check` | 201 |
| `omop_concept` | 181 |
| `omop_concept_relationship` | 519 |
| `ontology_closure` | 221 |
| `ontology_edge` | 87 |
| `ontology_node` | 88 |
| `ontology_predicate` | 7 |
| `query_block` | 4 |
| `resolver_attempt` | 92 |
| `schema_meta` | 1 |
| `seed_reference` | 8 |

## Provenance

### `harvest_run`

One row per harvest. The provenance anchor for everything else.

| column | type | null | notes |
| --- | --- | --- | --- |
| `run_id` (pk) | TEXT | no |  |
| `started_at` | TEXT | no |  |
| `finished_at` | TEXT | yes |  |
| `query_profile` | TEXT | no |  |
| `corpus_query` | TEXT | no |  |
| `esearch_count` | INTEGER | yes |  |
| `pmids_retrieved` | INTEGER | yes |  |
| `articles_stored` | INTEGER | yes |  |
| `facets_run` | INTEGER | yes |  |
| `eutils_requests` | INTEGER | yes |  |
| `tool_version` | TEXT | yes |  |
| `notes` | TEXT | yes |  |

### `query_block`

One row per named clause of the corpus query, with its PubMed hit count and
query translation.

| column | type | null | notes |
| --- | --- | --- | --- |
| `run_id` (pk) | TEXT | no |  |
| `block_id` (pk) | TEXT | no |  |
| `query` | TEXT | no |  |
| `rationale` | TEXT | yes |  |
| `esearch_count` | INTEGER | yes |  |
| `translation` | TEXT | yes |  |

### `article_query_block`

Which clause of the corpus query retrieved each record. A record can be
attributed to several clauses.

| column | type | null | notes |
| --- | --- | --- | --- |
| `pmid` (pk) | TEXT | no |  |
| `run_id` (pk) | TEXT | no |  |
| `block_id` (pk) | TEXT | no |  |

### `schema_meta`

Schema version marker.

| column | type | null | notes |
| --- | --- | --- | --- |
| `key` (pk) | TEXT | no |  |
| `value` | TEXT | no |  |


## Articles

### `article`

One row per PubMed record. No filtering: language, publication type, retraction
status and species check-tags are stored, not applied.

| column | type | null | notes |
| --- | --- | --- | --- |
| `pmid` (pk) | TEXT | no |  |
| `doi` | TEXT | yes |  |
| `pmc` | TEXT | yes |  |
| `pii` | TEXT | yes |  |
| `record_type` | TEXT | yes |  |
| `title` | TEXT | yes |  |
| `vernacular_title` | TEXT | yes |  |
| `abstract` | TEXT | yes |  |
| `has_abstract` | INTEGER | no | Derived, so 'papers with an abstract' does not need a NULL test. |
| `abstract_section_count` | INTEGER | yes |  |
| `journal_title` | TEXT | yes |  |
| `journal_iso` | TEXT | yes |  |
| `journal_nlm_id` | TEXT | yes |  |
| `journal_country` | TEXT | yes |  |
| `issn` | TEXT | yes |  |
| `volume` | TEXT | yes |  |
| `issue` | TEXT | yes |  |
| `pagination` | TEXT | yes |  |
| `pub_year` | INTEGER | yes |  |
| `pub_month` | INTEGER | yes |  |
| `pub_day` | INTEGER | yes |  |
| `medline_date` | TEXT | yes | PubMed's free-text date, e.g. '2019 Sep-Oct', kept verbatim alongside the parsed parts. |
| `article_date` | TEXT | yes |  |
| `entrez_date` | TEXT | yes | When PubMed received the record, which is not the publication date. |
| `medline_status` | TEXT | yes |  |
| `owner` | TEXT | yes |  |
| `indexing_method` | TEXT | yes |  |
| `publication_status` | TEXT | yes |  |
| `author_count` | INTEGER | yes |  |
| `mesh_descriptor_count` | INTEGER | yes |  |
| `is_mesh_indexed` | INTEGER | no | False for in-process and publisher-supplied records. About a quarter of this corpus. |
| `is_retracted` | INTEGER | no | Set from a RetractionIn CommentsCorrections link, not from the publication type alone. |
| `is_human_tagged` | INTEGER | no |  |
| `is_animal_tagged` | INTEGER | no |  |
| `integrity_flags` | TEXT | yes | JSON array, e.g. ["has_erratum"]. Errata are not retractions. |
| `first_seen_run` | TEXT | yes | Never overwritten by a later run. |
| `fetched_at` | TEXT | yes |  |

### `article_abstract_section`

Structured abstract sections with their labels and NLM categories, kept
separately from the concatenated abstract.

| column | type | null | notes |
| --- | --- | --- | --- |
| `pmid` (pk) | TEXT | no |  |
| `section_order` (pk) | INTEGER | no |  |
| `label` | TEXT | yes |  |
| `nlm_category` | TEXT | yes |  |
| `text` | TEXT | no |  |

### `article_author`

Authors in byline order, with the first affiliation and ORCID where given.

| column | type | null | notes |
| --- | --- | --- | --- |
| `pmid` (pk) | TEXT | no |  |
| `position` (pk) | INTEGER | no |  |
| `last_name` | TEXT | yes |  |
| `fore_name` | TEXT | yes |  |
| `initials` | TEXT | yes |  |
| `collective_name` | TEXT | yes |  |
| `orcid` | TEXT | yes |  |
| `affiliation` | TEXT | yes |  |
| `affiliation_count` | INTEGER | yes |  |

### `article_chemical`

NLM chemical substance headings.

| column | type | null | notes |
| --- | --- | --- | --- |
| `pmid` (pk) | TEXT | no |  |
| `substance_ui` | TEXT | yes |  |
| `substance_name` (pk) | TEXT | no |  |

### `article_facet`

The tag join. evidence_source is 'pubmed_query' or 'mesh_term'; both paths are
recorded because they disagree.

| column | type | null | notes |
| --- | --- | --- | --- |
| `pmid` (pk) | TEXT | no |  |
| `facet_id` (pk) | TEXT | no |  |
| `evidence_source` (pk) | TEXT | no | 'pubmed_query' or 'mesh_term'. |
| `detail` | TEXT | yes | The run id for a query hit; the matched descriptor names for a MeSH hit. |

### `article_grant`

Funding acknowledgements as indexed.

| column | type | null | notes |
| --- | --- | --- | --- |
| `pmid` | TEXT | no |  |
| `grant_id` | TEXT | yes |  |
| `agency` | TEXT | yes |  |
| `country` | TEXT | yes |  |

### `article_integrity_link`

CommentsCorrections links: retractions, errata, expressions of concern.

| column | type | null | notes |
| --- | --- | --- | --- |
| `pmid` | TEXT | no |  |
| `ref_type` | TEXT | no |  |
| `ref_source` | TEXT | yes |  |
| `target_pmid` | TEXT | yes |  |

### `article_keyword`

Author-supplied keywords, not MeSH.

| column | type | null | notes |
| --- | --- | --- | --- |
| `pmid` (pk) | TEXT | no |  |
| `keyword` (pk) | TEXT | no |  |
| `is_major` | INTEGER | no |  |

### `article_language`

Languages declared on the record.

| column | type | null | notes |
| --- | --- | --- | --- |
| `pmid` (pk) | TEXT | no |  |
| `language` (pk) | TEXT | no |  |

### `article_mesh`

One row per descriptor/qualifier pair. A heading with no qualifier stores
qualifier_ui = '' and has_qualifier = 0.

| column | type | null | notes |
| --- | --- | --- | --- |
| `pmid` (pk) | TEXT | no |  |
| `descriptor_ui` (pk) | TEXT | no |  |
| `descriptor_name` | TEXT | no |  |
| `descriptor_major` | INTEGER | no |  |
| `qualifier_ui` (pk) | TEXT | no | '' rather than NULL when absent, because SQLite does not treat NULLs as equal inside a primary key. |
| `qualifier_name` | TEXT | yes |  |
| `qualifier_major` | INTEGER | no |  |
| `has_qualifier` | INTEGER | no |  |

### `article_publication_type`

PubMed publication types, e.g. Journal Article, Case Reports, Review, Retracted
Publication.

| column | type | null | notes |
| --- | --- | --- | --- |
| `pmid` (pk) | TEXT | no |  |
| `type_ui` | TEXT | yes |  |
| `type_name` (pk) | TEXT | no |  |

### `article_reference`

Reference PMIDs where the publisher supplied them. Coverage is partial.

| column | type | null | notes |
| --- | --- | --- | --- |
| `pmid` (pk) | TEXT | no |  |
| `referenced_pmid` (pk) | TEXT | no |  |


## Facets

### `facet_group`

The six facet groups: exposure, outcome, data_source, assessment, design,
population.

| column | type | null | notes |
| --- | --- | --- | --- |
| `group_id` (pk) | TEXT | no |  |
| `label` | TEXT | no |  |
| `definition` | TEXT | yes |  |

### `facet`

Snapshot of the curated facet vocabulary as of the run.

| column | type | null | notes |
| --- | --- | --- | --- |
| `facet_id` (pk) | TEXT | no |  |
| `group_id` | TEXT | no |  |
| `label` | TEXT | no |  |
| `definition` | TEXT | yes |  |
| `pubmed_query` | TEXT | no |  |
| `mesh_terms` | TEXT | yes |  |
| `source_refs` | TEXT | yes |  |

### `facet_query_run`

One row per facet query executed: PubMed hit count, how many landed inside the
corpus, and the query translation.

| column | type | null | notes |
| --- | --- | --- | --- |
| `run_id` (pk) | TEXT | no |  |
| `facet_id` (pk) | TEXT | no |  |
| `query` | TEXT | no |  |
| `esearch_count` | INTEGER | yes |  |
| `pmids_in_corpus` | INTEGER | yes |  |
| `translation` | TEXT | yes |  |
| `ran_at` | TEXT | yes |  |

### `article_facet`

The tag join. evidence_source is 'pubmed_query' or 'mesh_term'; both paths are
recorded because they disagree.

| column | type | null | notes |
| --- | --- | --- | --- |
| `pmid` (pk) | TEXT | no |  |
| `facet_id` (pk) | TEXT | no |  |
| `evidence_source` (pk) | TEXT | no | 'pubmed_query' or 'mesh_term'. |
| `detail` | TEXT | yes | The run id for a query hit; the matched descriptor names for a MeSH hit. |

### `seed_reference`

The papers the vocabulary was built from, with their lookup query, expected
PMID and how resolution went.

| column | type | null | notes |
| --- | --- | --- | --- |
| `ref_id` (pk) | TEXT | no |  |
| `citation` | TEXT | no |  |
| `pubmed_lookup` | TEXT | yes |  |
| `expected_title` | TEXT | yes |  |
| `expected_pmid` | TEXT | yes |  |
| `note` | TEXT | yes |  |
| `resolved_pmid` | TEXT | yes |  |
| `resolved_title` | TEXT | yes |  |
| `match_method` | TEXT | yes | 'title_exact', 'unique_hit', 'ambiguous' or 'not_found'. Anything but the first two means the lookup did not resolve. |
| `lookup_hits` | INTEGER | yes |  |
| `found_in_corpus` | INTEGER | yes |  |
| `expected_pmid_in_corpus` | INTEGER | yes | Checked directly against the article table, independently of the lookup query. |

### `mesh_term_check`

Whether each declared MeSH descriptor is a real NLM descriptor, and whether the
corpus uses it.

| column | type | null | notes |
| --- | --- | --- | --- |
| `facet_id` (pk) | TEXT | no |  |
| `descriptor_name` (pk) | TEXT | no |  |
| `is_valid` | INTEGER | yes | True only when the query translation still names the MeSH Terms field. PubMed silently falls back to free text for a descriptor that does not exist. |
| `pubmed_count` | INTEGER | yes |  |
| `corpus_count` | INTEGER | yes |  |
| `translation` | TEXT | yes |  |
| `checked_at` | TEXT | yes |  |


## Ontology

### `ontology_node`

Curated graph nodes: the 41 facets plus the abstract classes.

| column | type | null | notes |
| --- | --- | --- | --- |
| `node_id` (pk) | TEXT | no |  |
| `label` | TEXT | no |  |
| `kind` | TEXT | no |  |
| `group_id` | TEXT | yes |  |
| `definition` | TEXT | yes |  |

### `ontology_predicate`

Predicate declarations, including whether each is transitive and subsumption-
like.

| column | type | null | notes |
| --- | --- | --- | --- |
| `predicate` (pk) | TEXT | no |  |
| `transitive` | INTEGER | no |  |
| `subsumption` | INTEGER | no |  |
| `symmetric` | INTEGER | no |  |
| `inverse` | TEXT | yes |  |

### `ontology_edge`

Curated graph edges. source = 'curated' for everything written from
relations.yaml.

| column | type | null | notes |
| --- | --- | --- | --- |
| `subject_id` (pk) | TEXT | no |  |
| `predicate` (pk) | TEXT | no |  |
| `object_id` (pk) | TEXT | no |  |
| `provenance` | TEXT | yes |  |
| `source` (pk) | TEXT | no |  |

### `ontology_closure`

Materialised transitive closure of the transitive predicates, one predicate per
row, with the shortest path length.

| column | type | null | notes |
| --- | --- | --- | --- |
| `ancestor_id` (pk) | TEXT | no |  |
| `descendant_id` (pk) | TEXT | no |  |
| `predicate` (pk) | TEXT | no |  |
| `min_levels` | INTEGER | no | Shortest path length. The graph is a poly-hierarchy, so a node can reach an ancestor by several paths. |


## OMOP concepts

### `omop_concept`

Concepts returned by a resolver. concept_id is populated only from an ATHENA
bundle; the public fallbacks return codes without it.

| column | type | null | notes |
| --- | --- | --- | --- |
| `concept_id` | INTEGER | yes |  |
| `concept_code` (pk) | TEXT | no |  |
| `vocabulary_id` (pk) | TEXT | no |  |
| `concept_name` | TEXT | yes |  |
| `domain_id` | TEXT | yes |  |
| `concept_class_id` | TEXT | yes |  |
| `standard_concept` | TEXT | yes |  |
| `invalid_reason` | TEXT | yes |  |
| `source` (pk) | TEXT | no | The resolver name: athena_bundle, athena_api, ols4, nlm_clinical_tables or rxnav. |
| `resolved_at` | TEXT | yes |  |

### `omop_concept_relationship`

Terminology-derived relationships, kept entirely separate from the curated
graph.

| column | type | null | notes |
| --- | --- | --- | --- |
| `vocabulary_id_1` (pk) | TEXT | no |  |
| `concept_code_1` (pk) | TEXT | no |  |
| `relationship_id` (pk) | TEXT | no |  |
| `vocabulary_id_2` (pk) | TEXT | no |  |
| `concept_code_2` (pk) | TEXT | no |  |
| `concept_name_2` | TEXT | yes |  |
| `source` (pk) | TEXT | no |  |
| `resolved_at` | TEXT | yes |  |

### `facet_concept`

Candidate facet-to-concept mappings. Everything starts as mapping_status =
'unreviewed'.

| column | type | null | notes |
| --- | --- | --- | --- |
| `facet_id` (pk) | TEXT | no |  |
| `vocabulary_id` (pk) | TEXT | no |  |
| `search_term` (pk) | TEXT | no |  |
| `concept_code` (pk) | TEXT | no |  |
| `concept_name` | TEXT | yes |  |
| `concept_id` | INTEGER | yes | NULL unless resolved from an ATHENA bundle. The public fallbacks cannot supply it. |
| `domain_id` | TEXT | yes |  |
| `match_rank` | INTEGER | yes |  |
| `label_match` | TEXT | yes | 'exact', 'contains', 'loose' or 'unknown'. Token-based, normalising case, punctuation, SNOMED semantic tags, LOINC unit brackets, British spellings, terminology abbreviations and simple plurals. |
| `resolver` (pk) | TEXT | no |  |
| `mapping_status` | TEXT | no | 'unreviewed' for everything a resolver produced. Change it only after a human check. |
| `resolved_at` | TEXT | yes |  |

### `resolver_attempt`

Every resolver call: ok, no_match or error. This is how the report says which
sources actually ran.

| column | type | null | notes |
| --- | --- | --- | --- |
| `resolver` (pk) | TEXT | no |  |
| `target` (pk) | TEXT | no |  |
| `status` | TEXT | no |  |
| `detail` | TEXT | yes |  |
| `attempted_at` (pk) | TEXT | no |  |


## Views

### `v_article_age_strata`

One row per article with the age-stratum tags as 0/1 columns and a derived
pediatric flag; the pediatric corpus is a WHERE clause on it.

### `v_exposure_outcome_cooccurrence`

Articles carrying each exposure facet together with each outcome facet. Where
the literature is, not what it found.

### `v_facet_evidence_disagreement`

Per facet: how many articles only the query found, only MeSH indexing found,
and both.

### `v_facet_summary`

Per facet: articles found by query, by MeSH indexing, and by either.

### `v_loose_top_matches`

The best candidate for a term whose concept name does not contain the term.
Review these first.

### `v_mapping_review_queue`

Unreviewed candidate mappings, loose matches first.

### `v_ontology_is_a_paths`

The closure joined to node labels, for readable ancestor listings.


## Files on disk

| path | tracked in git | contents |
| --- | --- | --- |
| `data/raw/*.xml.gz` | no | Every efetch response verbatim. Re-parsing needs no network. |
| `data/articles/pmid_NN.jsonl` | yes | One JSON object per article: the corpus itself, in shards of one million PMIDs. Compacted at the end of each harvest (deduplicated by PMID, sorted numerically) so a re-run diffs readably. Everything else under data/ is derived from it. |
| `data/articles.jsonl` | no | Append-only staging during a harvest; folded into the shards and removed at the end. |
| `data/db/exposome_pubmed.sqlite` | no | The normalized database. Disposable. |
| `data/derived/*.tsv` | yes | Small reviewable outputs: facet counts, the relation graph and its closure, candidate mappings, corpus composition. |
| `docs/HARVEST_REPORT.md` | yes | Generated validation report for the last run. |
