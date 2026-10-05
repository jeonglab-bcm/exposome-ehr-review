# Decision models: how links and study codes are made

This replaces the old generative summarizer (`legacy/summarizer/`). The method
is POTS-phenotyping's (branch `feature/whole-corpus`), with the questions read
from config instead of hard-coded.

## The idea

A System One decision model does not write text. It answers a typed question
about one passage with a probability: a yes/no, or a choice among fixed
options that always includes "none". Every answer is stored with its
probabilities, so a stricter threshold is an offline change, not a re-run.
Claude is used only at the end, to confirm a candidate link against the
passage with a verbatim quote.

| Output | Unit | Questions from | Script |
|---|---|---|---|
| Literature links (exposure → outcome, …) | paragraph | `config/facets.yaml`, `config/relations.yaml` | `scripts/judge_paragraphs.py` |
| Study coding (EHR use, data sources, ages, design, data availability) | paper | `config/study_coding.yaml` | `scripts/code_studies.py` |

### Links: four gated stages per paragraph

1. **Terms.** "Which of these exposures / health outcomes / data sources /
   exposure assessment methods does `paragraph` mention?", asked in rounds
   until "none" wins. Gate: two or more terms.
2. **Relational.** Yes/no: does it state a relationship between them?
3. **Relation.** Which predicates (`option` text in `relations.yaml`).
4. **Statement.** For each predicate, which ordered pairs (allowed by its
   `domain` and `range`) the paragraph states, phrased by `statement`.

`risk_factor_for`, `protective_for` and `no_association_with` are separate so
null results are kept (CQ1–CQ3 in `docs/COMPETENCY_QUESTIONS.md`).

### From readers to links

1. Two or three readers (decision models) read every paragraph.
2. `assemble_links.py --candidates`: a link is a candidate for a paper when at
   least two of the readers that read the paper found it.
3. `claude_judge.py`: Claude reads the candidate's paragraphs three times and
   answers supports / partial / not_stated with a verbatim quote
   (`extraction/JUDGE_INSTRUCTIONS.md`).
4. `assemble_links.py`: kept when two of three confirm and the quote is word
   for word in the paragraph; the rest go to `not_added` with the reason.
   Output: `config/relation_excerpts.yaml`.
5. `build_relations.py` generates the links into `config/relations.yaml`;
   `verify_excerpts.py` re-checks every quote.

## Running it

The paragraph texts are copyrighted and stay in `work/` (ignored). Only ids,
hashes, licences and the models' answers are committed.

```bash
make paragraphs RUN=extraction/2026-10-05-seeds PAPERS=seeds

# readers (use the model ids your server lists; the folder names are what
# assemble_links.py --readers refers to)
make judge BACKEND=ollaya      MODEL=winnow:e4b                FOLDER=winnow_e4b
make judge BACKEND=bioinfolder MODEL=<Kev-4B id on llama-swap> FOLDER=kev-4b_q8
make judge BACKEND=typesafe    MODEL=jev-1.13.0                FOLDER=jev-1.13.0

make candidates READERS="jev=jev-1.13.0 winnow=winnow_e4b kev=kev-4b_q8"
make claude-judge
make links READERS="jev=jev-1.13.0 winnow=winnow_e4b kev=kev-4b_q8"

make code BACKEND=bioinfolder MODEL=<id> FOLDER=kev-4b_q8   # study coding -> <run>/coding/kev-4b_q8/studies.tsv
```

Backends and keys (never written to the repository):

| `--backend` | Server | Key |
|---|---|---|
| `ollaya` | local Ollaya, `$OLLAYA_URL` (default `127.0.0.1:11435`) | none |
| `llamacpp` | local llama-server with a decision GGUF, `$LLAMACPP_URL` | none |
| `bioinfolder` | the lab's llama-swap at llm.bioinfolder.com | `BIOINFOLDER_KEY` or Keychain `bioinfolder-llm` |
| `typesafe` | TypeSafe's hosted Jev | `TYPESAFE_API_KEY` or Keychain `typesafe-api-key` |

**Licences.** A hosted backend (`typesafe`) is sent only papers whose PMC
licence is CC BY or CC0 (`<run>/papers.tsv`, column `cc_by`). TypeSafe keeps
requests under its Data Processing Agreement, so CC BY-NC, author manuscripts
and abstract-only papers stay on local or lab models. For the seeds that means
only patel2010 goes to Jev; the other papers need winnow and Kev to agree.

**Cost of the whole corpus.** Study coding reads the abstract (all 15.9k
papers) and the methods and data-availability paragraphs of papers with PMC
full text (about half the corpus has a PMCID; fewer have an open-access body), up to 12 passages per kind per field. Links read every paragraph
of the full-text papers. Plan the whole-corpus run on the lab server, not a
laptop; POTS measured about 9 s per paragraph for winnow on an M2 Pro and
about 6x faster on the lab server.

## Validation still to do

The old pipeline was never checked against people, and neither is this one
yet. Before the coding is used in a review:

1. Hand-code 30–50 papers (stratified by data source and age) for the
   study-coding fields, blind to the models.
2. Report per-field agreement (kappa for one_of, precision/recall for any_of)
   for each reader and for the reader majority.
3. For links, blind-adjudicate a sample of reader/Claude disagreements, as
   POTS did (`extraction/2026-10-04-judged/VALIDATION.md` there: 10 of 12).
