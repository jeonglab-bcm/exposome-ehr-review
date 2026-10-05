# Legacy pipeline (frozen 2026-10-05)

The previous version of this repository, kept for reference and not
maintained. It searched PubMed Central with 20 tiered queries, downloaded 185
open-access papers into `../papers/`, and had one generative model (Gemma 4
12B, then Qwen3.6-MoE, then ornith-1.5-35b) write a JSON checklist per paper.

It was replaced by the decision-model pipeline at the repository root
(`../docs/DECISION_MODELS.md`) because:

- **Its corpus was a sample, not a search.** Each query kept at most 200 hits
  (`MAX_PER_QUERY`), and the PMC `[Title/Abstract]` searches admitted animal
  studies, vaccine-hesitancy surveys, case reports and methods papers. Of the
  30 papers it marked `ehr_used`, 11 are in the new corpus. Of the 19 that are
  not: 2 are published corrections, 10 are programme reports, descriptive
  surveillance or attitude surveys, 2 are research-cohort studies with no
  routinely collected data, 1 is an economic estimate, and 4 are in-scope
  studies the new query misses (PMC4997457, PMC4381500, PMC8986305,
  PMC4113360; see the limitations in the top-level README).
- **Retrieval filters made scope decisions irreversible.** Pediatric terms,
  open access and "not a review" were required at search time. They are now
  tags and decision-model answers, filtered downstream.
- **Free text could not be checked or compared.** Exposures, outcomes and
  designs were free text; the quotes the model gave were never checked against
  the paper; there was no agreement measure against people. The new pipeline
  uses typed answers with probabilities, a controlled vocabulary mapped to
  MeSH and OMOP, and quotes verified word for word.

What carried over: the data-availability regex safety net
(`scan_data_availability.py`) is still the way accession numbers are found,
and `papers/` (the 185 PDFs/XML, Git LFS) is kept at the repository root.

The code here expects to run from the repository root with the old layout
(`papers/`, `summarizer/` importable); it is not wired into the new Makefile
or CI. The old GitHub Pages site is in `docs/`.
