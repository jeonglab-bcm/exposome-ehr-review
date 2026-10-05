# exposome-ehr-review

A PubMed corpus, a controlled vocabulary and a decision-model reading pipeline
for the exposome studied in routinely collected health data: environmental,
social and medical exposures (including vaccines) in electronic health
records, claims, registries and linked administrative data.

Built the same way as [POTS-phenotyping](https://github.com/jeonglab-bcm/pots-phenotyping),
and sharing its code: one broad query, facets as tags rather than filters,
concept mappings with provenance, and literature links found by System One
decision models and confirmed by Claude with verbatim quotes. The previous
generative-summary pipeline is frozen in [`legacy/`](legacy/README.md).

## The premise

The old pipeline decided scope at search time (pediatric terms required, open
access only, reviews dropped, at most 200 hits per query) and then had one
generative model write a free-text checklist per paper. Nothing it produced
could be compared across papers or checked against the text.

Here every scope decision is made afterwards, on recorded evidence:

- **Retrieve broadly, decide later.** Age stratum, publication type and full-
  text availability are tags and columns. The pediatric corpus is a `WHERE`
  clause (`v_article_age_strata.pediatric = 1`), not a query.
- **Typed answers, not prose.** Each paper is coded with yes/no and fixed-
  option questions (`config/study_coding.yaml`), each answer stored with its
  probability and the paragraph that carried it.
- **Links are quoted claims.** An exposure-outcome link exists only when two
  decision models found it in a paragraph and Claude confirmed it with a quote
  that is word for word in the paper. Harm, protection and null results are
  separate predicates, so the evidence map does not drop null findings.

## What is here

| Path | Contents |
| --- | --- |
| `config/query.yaml` | The broad-recall query: four named blocks with a rationale each, no filters. |
| `config/facets.yaml` | 67 facets in six groups (exposure, outcome, data source, exposure assessment, design, age stratum) with PubMed queries, MeSH descriptors, OMOP concept search terms and eight seed papers. |
| `config/relations.yaml` | The ontology: `is_a` taxonomy, and six literature predicates whose decision-model questions (domain, range, option, statement) are defined next to them. |
| `config/study_coding.yaml` | The per-paper questions that replace the old checklist. |
| `docs/COMPETENCY_QUESTIONS.md` | What the graph and the coding must answer. |
| `docs/DECISION_MODELS.md` | How links and codes are made, the backends, licences, and the validation still to do. |
| `docs/HARVEST_REPORT.md` | Generated validation report for the committed harvest. |
| `src/exposome_ehr/` | Harvester, parser, SQLite store, concept resolvers and report (ported from POTS-phenotyping), plus `paragraphs.py` and `decision.py`. |
| `scripts/` | Paragraph building, the decision-model readers, study coding, candidate assembly, the Claude judge, quote verification. |
| `extraction/<run>/` | Stored reader runs: paragraph ids and hashes, model answers, judge verdicts. No paper text. |
| `data/articles/pmid_NN.jsonl` | The harvested corpus, one JSON object per article, sharded by PMID range. Everything else under `data/` rebuilds from it. |
| `legacy/` | The previous pipeline, frozen. |

## Results of the committed harvest

Profile `core`, harvested 2026-10-05. Full numbers in
[`docs/HARVEST_REPORT.md`](docs/HARVEST_REPORT.md).

| | |
| --- | --- |
| Articles | 15,913 (all of them; esearch reported 15,913) |
| Tagged pediatric (prenatal, infant, child or adolescent) | 6,951 |
| Seed papers in the corpus | 8 of 8 |
| MeSH descriptors declared | 201, all valid |
| Candidate concept mappings | 181 across SNOMED, LOINC and NCIt, all unreviewed; 19 of 67 facets unmapped |
| Literature links | none yet: the seed reader run is set up, not run |
| Validation checks | 24 of 26 pass (the two failures are the unmapped concept terms) |

## Quick start

```bash
pip install -r requirements.txt

make check        # validate the vocabulary and the generated links, no network
make counts       # corpus and facet sizes, esearch only
make harvest      # full corpus harvest with facet tagging
make ontology     # resolve facets to OMOP concepts
make report       # regenerate docs/HARVEST_REPORT.md and data/derived/
make test         # offline test suite

make paragraphs PAPERS=seeds   # then see docs/DECISION_MODELS.md
```

Set `NCBI_API_KEY` to lift the E-utilities rate limit, and `NCBI_TOOL_EMAIL`.

## Known limitations

- **The vocabulary is a draft.** It was written from the previous pipeline's
  summaries and the seed abstracts, not yet from reading the seeds; the seed
  reader run is what tests it.
- **Recall against the old pipeline.** Of its 30 EHR-positive papers, 11 are
  retrieved; 15 of the other 19 are out of scope (corrections, programme
  reports, research cohorts) and 4 are real misses (listed in
  `legacy/README.md`). Widening the vaccine block to name surveillance and
  pharmacovigilance would add about 1,600 records and recover one of them.
- **Studies that name only their outcomes of care are missed.** An
  administrative-data cohort whose abstract says "hospitalization or emergency
  room visit" but never names its data source is not retrieved. Adding those
  phrases grows the corpus to about 19,800, mostly aggregate time-series
  studies; that trade was declined for now (2026-10-05).
- **Nothing is validated against people yet.** See the validation plan in
  `docs/DECISION_MODELS.md`.
- **Concept mappings are candidates.** OLS4 carries only part of SNOMED;
  exposure-type concepts ("exposure to particulate matter") mostly need an
  ATHENA bundle.
- **Pages.** The old GitHub Pages site lived in `docs/` and is now in
  `legacy/docs/`; a new site for this corpus is not built yet.
