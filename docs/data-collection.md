# Data collection

The collection stage creates an all-age exposome evidence universe while
keeping population, EHR use, and scope as explicit facets. It is implemented in
[`fetch_pmc_papers.py`](../fetch_pmc_papers.py), with eligibility logic in
[`screening.py`](../screening.py).

← back to the [README](../README.md)

## Scope decision

The primary product covers adult, pediatric, and mixed-age human studies. Its
named query registry currently contains **20 executable queries** in four
separately labeled arms:

| Arm | Population | Purpose |
|---|---|---|
| `core` | all age | `Exposome[mh]`, `exposom*`, ExWAS/P-ExWAS, and environment-/exposure-/exposome-wide association studies |
| `operational` | all age | Multiple exposures, mixtures, combined/cumulative exposure, and related operational exposomics |
| `operational_vaccine` | all age | Vaccine exposure followed by health, safety, immune, biomarker, or other biological outcomes |
| `adjacent_single_exposure` | explicitly labeled per query | EHR-linked and cohort-based single-exposure context retained separately from core exposomics |

Vaccine exposure studies are eligible exposome evidence; they are not treated
as an error or automatically relegated to an adjacent category. Records only
about uptake, coverage, attitudes, acceptance, or hesitancy remain out of scope
unless an exposure–outcome design is established.

## Retrieval contract

1. Validate every query against fields supported by PMC. The registry uses
   `[mh]`, `[tiab]`, `[ti]`, and `[filter]`; PubMed-only `[Publication Type]`
   clauses are forbidden because PMC otherwise remaps them silently.
2. Freeze the search at the last completed UTC day with a PMC Live Date range
   and `usehistory=y`. A root result above PMC's 9,999-history-ID limit is
   recursively split into non-overlapping date shards; every child count must
   reconcile with its parent and the leaves must cover the complete interval.
3. Page each frozen leaf through PMC ESummary at no more than 500 records per
   call. Persist and reconcile every `page_start` and `page_count`, the
   deduplicated UID total, each leaf total, and the root NCBI count.
4. Resolve PMC records to PMIDs and enrich screening metadata through PubMed
   EFetch. Abstracts, publication types, MeSH terms, missing-field status, and
   metadata source are treated as auditable screening provenance; omitted
   requested PMIDs fail the metadata batch.
5. Retry transport errors, HTTP 429, and 5xx responses using `Retry-After` when
   supplied or exponential backoff otherwise. Exhausted retries, malformed
   JSON/XML, missing metadata, unreconciled shard counts, duplicate IDs, and
   short pages fail the stage; partial results are never returned.
6. Identify every E-utilities call with `NCBI_TOOL` and required `NCBI_EMAIL`;
   send `NCBI_API_KEY` when configured.
7. Persist `search_runs` in `papers/download_log.json`: database, stable query
   name/arm, exact and effective snapshot queries, snapshot date, root and shard
   counts, page starts/counts, translated queries, full shard metadata, and UTC
   retrieval timestamps. Every candidate stores its matched query membership
   with the same snapshot/effective-query provenance.

The authoritative search cutoff is each run's `snapshot_date`; `retrieved_at`
records when the frozen snapshot was executed. The full strategy can be
reconstructed exactly from `search_runs` and `query_membership`.

## Screening flow

Search hits are candidates, not automatically included publications.
`screen_candidate` records one of `included`, `excluded`, or `pending`, plus:

- core/mixture/vaccine/adjacent scope classification;
- adult/pediatric/mixed/unclear population facet;
- human-study and primary-study evidence;
- EHR/administrative-record facet;
- query provenance and exclusion reasons;
- automated or manual-review provenance.

Automatic inclusion requires affirmative human and primary-study evidence.
Animal-only work, reviews/meta-analyses, corrections/errata, conference items,
and vaccine-attitude records without an outcome design are excluded. Ambiguous
records remain `pending` and cannot enter the published count until reviewed.
The complete candidate, included, excluded, and pending collections are
persisted separately in the download log and copied into manifest screening
metadata.

Manual decisions are durable inputs in `papers/screening_overrides.json`, keyed
by PMCID with reviewer, review timestamp, decision, and supporting evidence.
The fetcher reapplies them on every run; the publication verifier rejects a
manual decision that is not represented in that override file.

The live recall guard requires the union strategy to retrieve PMC9678903,
PMC6144482, PMC11117089, PMC10099694, and the all-age seed PMC13099396.

## Full-text resolution and state

Included candidates use this ordered fallback:

1. NCBI OA direct PDF;
2. NCBI OA tarball, selecting the main non-supplementary PDF;
3. Europe PMC PDF;
4. Europe PMC JATS XML.

Each response is validated before the next decision. An HTTP-successful HTML or
truncated “PDF” therefore falls through instead of blocking later sources.
PDFs require `%PDF-` plus a minimum size. XML requires a JATS article with a
real body and rejects abstract-only records.

Before and after a complete run, `papers/manifest.json` is reconciled with the
download log and validated filesystem. Logged-but-missing papers are retried;
valid unlogged files are registered; PDF/XML duplicates are reduced to one
deterministic PMCID representation for downstream processing. Only validated
local files whose screening decision is explicitly `included` enter
summarization; publication additionally requires current `summarized` manifest
status. Retained excluded, pending, or stale files remain audit material and are
not counted as published papers.

An included record that remains abstract-only or cannot be materialized as
validated full text makes the download stage fail. Summarization likewise
refuses to replace combined output until every included record has local full
text, so an incomplete corpus cannot be published as a smaller successful run.
