# All-age Exposome Evidence Map

A reproducible pipeline for finding, screening, downloading, and summarizing
open-access exposome research in PubMed Central. The primary universe includes
adult, pediatric, and mixed-age studies. Population and EHR use are recorded as
facets rather than used as universal eligibility gates.

Vaccination is treated as a valid exposome exposure when a study evaluates a
vaccine exposure against a health or biological outcome. Uptake, coverage, and
hesitancy papers without an exposure–outcome design are excluded. Core
exposomics, operational mixtures, vaccine exposures, and adjacent
single-exposure studies remain separately labeled.

The current generated inventory is available in [`paper_summary.md`](./paper_summary.md)
and on the [static review site](https://hyunhwan-bcm.github.io/exposome-ehr-review/).
Counts and model provenance are generated from the underlying records rather
than maintained in this README.

## Quick start

```bash
cp .env.example .env  # set a monitored NCBI_EMAIL; API key is optional
make setup             # create .venv and install dependencies
make download          # complete, resumable search/screen/download stage
make summarize         # resume summaries; stale caches are invalidated
make scan              # enrich data-availability fields in current summaries
make results           # rebuild TinyDB, combined JSON, and Markdown reports
make site              # rebuild the static site from the combined records
make check-artifacts   # fail if manifest/files/reports/site disagree
make test              # offline unit tests
```

NCBI E-utilities requests always include `NCBI_TOOL` and `NCBI_EMAIL`, plus
`NCBI_API_KEY` when configured. Each run freezes the last completed UTC day,
date-shards result sets above the PMC history limit, and fails closed if a page,
shard count, or PubMed metadata batch is incomplete. See
[data collection](./docs/data-collection.md) for the exact retrieval and
screening contract.

`make site` additionally requires Node.js with `npx`; all Python-only targets
are installed by `make setup`.

## Pipeline

```text
named PMC queries → candidate screen → validated PDF/XML → PMCID manifest
       → per-paper summaries → rebuilt TinyDB/combined JSON
       → Markdown reports + static site → cross-artifact consistency gate
```

The PMCID-keyed manifest is the source of truth for retrieved full text. Only
records with an explicit `included` screening decision enter summarization, and
only those with current `summarized` status enter the publication projection;
excluded, pending, or stale full text may remain on disk for audit. The summary
directory is used to exact-rebuild TinyDB atomically, which removes stale
database rows. Each generated output carries a machine-checkable count, and CI
rejects count, payload, provenance, or deterministic-render drift.

## Documentation

| Guide | Contents |
|---|---|
| [Data collection](./docs/data-collection.md) | All-age search arms, NCBI pagination/retry contract, screening, provenance, and full-text validation |
| [Summarization](./docs/summarization.md) | Checklist extraction, PMCID deduplication, cache identity, and data-availability scan |
| [Storage, orchestration, and CI](./docs/orchestration.md) | Manifest, atomic rebuilds, Dagster lineage, consistency checks, and workflow configuration |

## Important files

| Path | Purpose |
|---|---|
| `fetch_pmc_papers.py` | Named query registry, complete ESearch retrieval, screening, and full-text fallback |
| `screening.py` | Human/primary-study eligibility and scope/population/EHR facets |
| `paper_manifest.py` | PMCID-keyed state, checksums, reconciliation, deduplication, and cache metadata |
| `summarizer/` | Pydantic checklist extraction through the configured OpenAI-compatible model |
| `database.py` / `db.py` | Validated TinyDB access and export |
| `pipeline.py` / `pipeline_ops.py` | Dagster assets and atomic rebuild operations |
| `artifact_consistency.py` | Final manifest-to-site consistency gate |
| `build_summary.py` / `build_results.py` / `build_site.py` | Generated review artifacts |
| `papers/download_log.json` | Exact queries, NCBI counts/timestamps, candidates, decisions, and query membership |
| `papers/manifest.json` | Canonical PMCID status/path/checksum/provenance records |

Large `papers/*.pdf`, `papers/*.xml`, and `papers/db.json` files are tracked with
Git LFS. Run `git lfs install` before pulling the corpus.
