# Manuscript summarization and data availability

Each included, validated full text is converted into a Pydantic-validated
`ManuscriptChecklist`. The LLM is selected with `EXPOSOME_LLM_MODEL` and called
through the configured OpenAI-compatible endpoint; generated reports derive
model provenance from each record rather than hard-coded prose.

← back to the [README](../README.md)

## Run

```bash
cp .env.example .env        # EXPOSOME_LLM_* (key optional on the tailnet)
make summarize
make summarize-paper PMC=PMC7145790
make scan
make results
python -m summarizer.run --recover --workers 4
```

`paper_manifest.discover_included_papers` validates the manifest's explicit
included-study projection and selects one PDF/XML representation per PMCID
before concurrent work begins. Publication consumers use the narrower
`summarized_paper_files` projection, so a failed refresh cannot republish an old
JSON after its source changes. Excluded or pending full text may remain on disk
for audit but cannot enter summaries or publication outputs. A cached summary
is reused only when all of these identities still match:

- canonical source path, source SHA-256 checksum, title, and year;
- prompt checksum;
- Pydantic schema checksum;
- configured model ID;
- extraction/processing-code checksum and summarization execution mode.

Cache sidecars live under `papers/summaries/.cache/`, outside the per-paper JSON
glob. Missing or mismatched metadata triggers re-summarization. Summary JSON and
combined outputs are published atomically where they become shared state. A
missing manifest is an error and leaves existing combined output untouched;
run download/reconciliation before a standalone summarization or scan.

## Checklist

| Field | Meaning |
|---|---|
| `pmcid`, `title`, `year` | Identity from retrieval metadata |
| `ehr_used`, `ehr_evidence` | Whether routinely collected individual electronic clinical/administrative data were used, with evidence |
| `summary`, `key_findings` | Plain-language study description and results |
| `captured_features` | EHR/administrative features, empty for non-EHR work |
| `pathologies_diseases` | Health outcomes |
| `study_design`, `data_source_type`, `population`, `exposure_domain` | Review facets |
| `limitations`, `confidence` | Limitations and extraction confidence |
| `data_availability`, `data_accession_links`, `data_availability_statement` | Data-access classification and evidence |
| `source_format`, `model` | Source and model provenance |

Population and EHR status are descriptive facets. Adult and non-EHR records do
not receive lower eligibility solely for those characteristics.

## Focused data-availability pass

[`scan_data_availability.py`](../scan_data_availability.py) performs a focused
second pass around data-availability text. Its Pydantic result distinguishes
public repositories, upon-request access, in-house data, supplementary-only
data, and unstated availability. A deterministic regex safety net preserves
repository URLs and accessions such as dbGaP, GEO, ArrayExpress, Zenodo,
figshare, Dryad, and GitHub identifiers.

The final combined JSON is rebuilt from the current included-and-summarized
projection of validated per-paper summaries. A manifest status alone is not
enough: the rebuild rechecks the source, prompt, schema, model, processing, and
execution-mode cache identity before publishing, and every explicitly included
record must be ready. A failed refresh updates manifest state but leaves the
last combined artifact unchanged for a clean retry. A limited/selected run also
leaves it unchanged unless the complete included set is current. TinyDB is
replaced from the exact complete set, so deleted, stale, or screened-out
summaries cannot survive as rows.
