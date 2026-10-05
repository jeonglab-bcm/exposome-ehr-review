# Run 2026-10-05-seeds: the eight seed papers

The first decision-model run: the seed papers in `config/facets.yaml`
(`seed_references`), paragraph by paragraph, to find the literature links the
evidence graph starts from, as POTS-phenotyping did with its seven seeds.

Status: **paragraphs built; no reader has run yet.** The readers need the
model servers (Ollaya, the lab's llama-swap, TypeSafe), which this repository
reaches from the lab, not from CI. See `docs/DECISION_MODELS.md`.

| Seed | PMID | Source | Licence | Paragraphs | Hosted model may read |
|---|---|---|---|---|---|
| wild2005 | 16103423 | PubMed (no abstract) | — | 0 | no |
| patel2010 | 20505766 | PMC JATS | CC BY 4.0 | 49 | yes |
| maitre2018 | 30206078 | PMC JATS | CC BY-NC 4.0 | 51 | no |
| vrijheid2020 | 32579081 | PMC JATS | EHP licence | 58 | no |
| brokamp2018 | 29126118 | PMC JATS | CC BY-NC 4.0 | 32 | no |
| hu2023 | 37089437 | PMC JATS | CC BY-NC 4.0 | 28 | no |
| correa2022 | 35970309 | PMC JATS | author manuscript | 33 | no |
| macdonald2014 | 24914115 | PubMed abstract | — | 4 | no |

255 paragraphs. `paragraphs.tsv` lists every id with its kind (abstract,
methods, results, discussion, availability, other), section and a hash of its
text; `papers.tsv` the source and licence of each paper. Rebuild the texts
with `make paragraphs` and compare the hashes.

Wild 2005 is a commentary with no abstract in PubMed and no PMC full text, so
it contributes nothing to the reader run; it stays a seed because it defines
the concept the corpus is about.

Next:

```bash
make judge BACKEND=ollaya MODEL=winnow:e4b FOLDER=winnow_e4b
make judge BACKEND=bioinfolder MODEL=<id> FOLDER=kev-4b_q8
make judge BACKEND=typesafe MODEL=jev-1.13.0 FOLDER=jev-1.13.0
make candidates claude-judge links
```
