# Storage, orchestration, and CI

The pipeline uses two explicit sources of truth: a PMCID-keyed manifest for
retrieved full text and validated per-paper JSON for the summary catalog.

← back to the [README](../README.md)

## State model

`papers/manifest.json` contains `schema_version`, `updated_at`, and a `records`
object keyed by PMCID. Each record carries status, selected path, `sha256:`
checksum, exact-query provenance, timestamps, optional screening metadata, and
bibliographic fields. The redundant `publication_eligible` flag must agree with
the explicit screening decision and is checked before publication.

Reconciliation validates the filesystem, repairs legacy `downloaded` and
`xml_only` lists, registers valid unlogged files, marks missing files for retry,
and deterministically chooses one representation when PDF and XML both exist.
Writes use a temporary file plus `os.replace` so readers do not observe partial
JSON.

TinyDB is a derived, validated index. `pipeline_ops.rebuild_combined` validates
the manifest's current included-and-summarized projection, builds a fresh
temporary database, exports a fresh combined JSON, and publishes each complete
file with an atomic replacement. This removes rows whose summary files no
longer exist or whose sources require re-summarization. Manifest-driven rebuilds
also reject missing/stale cache sidecars before replacing either output. The
rebuild refuses partial publication when even one included record is not ready.
The consistency gate detects cross-file drift if a process is interrupted
between the two final replacements.

The supported Make targets preserve that invariant: `make results`,
`make db-import`, and `make db-export` all exact-rebuild the database from the
current publishable per-paper summaries before exposing a combined batch. The
lower-level `db.py import` command is an upsert utility for interactive work and
is not a publication operation.

## Dagster lineage

```text
download_log → paper_state → per_paper_summaries → data_availability_scan
                    │                                      │
                    └→ paper_summary        manuscript_summaries → results → site
                              └──────────────────────────────────────────────┘
                                                     artifact_consistency
```

```bash
make dagster
make materialize
make check-artifacts
```

The final consistency asset audits all 20 exact query runs, translated-query
contracts, NCBI/retrieved/page counts, candidate membership, and the separate
included/excluded/pending screening partitions. It then compares the explicit
included-study projection across manifest, validated source files, download
log, per-paper summaries, TinyDB, both combined JSON files, Markdown
inventories, and the embedded static-site data. Every structured summary is
Pydantic-validated; manifest checksums and cache identities must be current;
record payloads and model provenance must agree; and reports/site must be
byte-for-byte equal to deterministic rebuilds.

## GitHub Actions configuration

Pushes and pull requests run the offline test suite with read-only repository
permissions. Publishing is a separate manual-dispatch job: it first passes the
same tests, materializes the full graph, rebuilds the site stylesheet, runs the
consistency checker again, and only then commits generated artifacts. Configure:

- repository variable `NCBI_TOOL` with a stable E-utilities client name;
- repository variable `NCBI_EMAIL` with a monitored E-utilities contact;
- optional secret `NCBI_API_KEY`;
- Tailscale OAuth secrets `TS_OAUTH_CLIENT_ID` and `TS_OAUTH_SECRET`; the LLM
  endpoint is reachable only from inside the tailnet;
- optional secret `EXPOSOME_LLM_API_KEY`. The tailnet-internal endpoint needs no
  key; set this only when pointing at a key-requiring endpoint;
- optional repository variable `EXPOSOME_LLM_MODEL` when it should differ from
  the code default.

The manual workflow rejects a nonempty `limit` input before contacting external
services. Pilot limits remain useful for local development, but CI publication
is deliberately full-corpus only.

## Artifacts

| Path | Contents |
|---|---|
| `papers/download_log.json` | Frozen exact/effective queries, shard/page proofs, metadata provenance, candidate decisions, and compatibility state |
| `papers/screening_overrides.json` | Durable PMCID-keyed manual decisions and reviewer evidence |
| `papers/manifest.json` | Canonical PMCID status/path/checksum/query/screening state |
| `papers/*.pdf`, `papers/*.xml` | Retrieved full text; one validated representation selected per PMCID |
| `papers/summaries/<pmcid>.json` | Validated per-paper checklist |
| `papers/summaries/.cache/<pmcid>.json` | Canonical source/checksum, title/year, prompt/schema/model, processing-code, and execution-mode cache identity |
| `papers/db.json` | Atomically rebuilt TinyDB index |
| `papers/manuscript_summaries.json` | Record-derived combined `SummaryBatch` |
| `results/manuscript_summaries.json` | Published combined copy |
| `paper_summary.md`, `results/*.md` | Generated inventories with count markers |
| `docs/index.html` | Static site with embedded record data and count metadata |
