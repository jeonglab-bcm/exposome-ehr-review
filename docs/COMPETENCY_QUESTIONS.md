# Competency questions

The project exists to answer these questions. Every predicate in
`config/relations.yaml` is here because one of them needs it, and every
per-paper field in `config/study_coding.yaml` is here for the same reason.

## Evidence graph (links, each backed by a verbatim quote)

| # | Question | Predicate | Path |
|---|---|---|---|
| CQ1 | Which exposures are reported to raise the risk of an outcome? | `risk_factor_for` | exposure → outcome |
| CQ2 | Which exposures are reported to lower it? | `protective_for` | exposure → outcome |
| CQ3 | Which exposure-outcome pairs were studied and found null? | `no_association_with` | exposure → outcome |
| CQ4 | How was each exposure measured or assigned to people? | `assessed_by` | exposure → assessment |
| CQ5 | Which data source was each outcome (or exposure) taken from? | `ascertained_from` | outcome → data source |
| CQ6 | Which data sources are linked to each other, or to an exposure model? | `linked_with` | data source ↔ data source |
| CQ7 | What kind of thing is each topic? | `is_a` | topic → class |

CQ1–CQ6 are answered only by quoted claims. CQ7 is structure (`taxonomy`
provenance) and is not a literature claim.

CQ1–CQ3 are kept apart on purpose. A single `associated_with` predicate would
merge harm, benefit and null results, and an evidence map that drops null
results over-states every exposure.

## Study coding (one answer per paper)

| # | Question | Field |
|---|---|---|
| SQ1 | Is this original research? | `primary_research` |
| SQ2 | Does it analyse routinely collected individual-level data (EHR, claims, registry, linkage)? | `routinely_collected_data` |
| SQ3 | Which data sources? | `data_sources` |
| SQ4 | Which life stages (prenatal, infant, child, adolescent, adult)? | `age_strata` |
| SQ5 | Which design? | `study_design` |
| SQ6 | Which exposures and outcomes? | `exposures_studied`, `outcomes_studied` |
| SQ7 | Can the data be obtained, and how? | `data_availability` |

SQ1 and SQ4 replace the old pipeline's retrieval filters (reviews dropped,
pediatric terms required). A pediatric EHR exposome corpus is the set of papers
with SQ1 = yes, SQ2 = yes and SQ4 ∋ infant, child or adolescent.
