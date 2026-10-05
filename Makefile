PY ?= python3
export PYTHONPATH := src

.PHONY: help check counts harvest smoke ontology check-mesh seeds compact rebuild report test all

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

check:  ## validate the vocabulary and the generated links, no network
	$(PY) -m exposome_ehr.cli validate-vocab
	$(PY) scripts/build_relations.py --check

counts:  ## esearch-only size reconnaissance
	$(PY) -m exposome_ehr.cli counts --profile core

smoke:  ## 200-record harvest, for checking the pipeline end to end
	$(PY) -m exposome_ehr.cli harvest --profile core --limit 200

harvest:  ## full corpus harvest with facet tagging
	$(PY) -m exposome_ehr.cli harvest --profile core

ontology:  ## resolve facets to OMOP concepts (pass ATHENA_DIR=... if you have a bundle)
	$(PY) -m exposome_ehr.cli ontology $(if $(ATHENA_DIR),--athena-dir $(ATHENA_DIR),)

report:  ## write docs/HARVEST_REPORT.md and data/derived/*.tsv
	$(PY) -m exposome_ehr.cli report --out docs/HARVEST_REPORT.md
	$(PY) scripts/generate_data_dictionary.py > docs/DATA_DICTIONARY.md

check-mesh:  ## validate declared MeSH descriptors against PubMed
	$(PY) -m exposome_ehr.cli check-mesh

seeds:  ## re-check the seed references against the stored corpus
	$(PY) -m exposome_ehr.cli seeds

compact:  ## deduplicate and sort data/articles.jsonl
	$(PY) -m exposome_ehr.cli compact

rebuild:  ## rebuild the database from data/articles.jsonl, no network
	$(PY) -m exposome_ehr.cli rebuild

test:  ## run the offline test suite
	$(PY) -m pytest

all: check harvest check-mesh ontology report  ## full pipeline

# ---- decision models (see docs/DECISION_MODELS.md) --------------------------
RUN     ?= extraction/2026-10-05-seeds
PAPERS  ?= seeds
BACKEND ?= ollaya
MODEL   ?= winnow:e4b
READERS ?= jev=jev-1.13.0 winnow=winnow_e4b kev=kev-4b_q8
FOLDER  ?=

.PHONY: paragraphs judge code candidates claude-judge links verify

paragraphs:  ## build paragraph files for PAPERS (seeds|all|file) into work/, manifest into RUN
	$(PY) scripts/build_paragraphs.py --papers $(PAPERS) --fetch --run $(RUN)

judge:  ## find links paragraph by paragraph: make judge BACKEND=bioinfolder MODEL=<id> FOLDER=kev-4b_q8
	$(PY) scripts/judge_paragraphs.py --run $(RUN) --backend $(BACKEND) --model $(MODEL) $(if $(FOLDER),--folder $(FOLDER),) --resume

code:  ## per-paper study coding: make code BACKEND=bioinfolder MODEL=<id> FOLDER=kev-4b_q8
	$(PY) scripts/code_studies.py --run $(RUN) --backend $(BACKEND) --model $(MODEL) $(if $(FOLDER),--folder $(FOLDER),) --resume

candidates:  ## links at least two readers found, for the judge
	$(PY) scripts/assemble_links.py --run $(RUN) --readers $(READERS) --candidates

claude-judge:  ## Claude confirms each candidate with a verbatim quote (claude -p, 3 repeats)
	$(PY) scripts/claude_judge.py --run $(RUN)

links:  ## write config/relation_excerpts.yaml and regenerate the links in relations.yaml
	$(PY) scripts/assemble_links.py --run $(RUN) --readers $(READERS)
	$(PY) scripts/build_relations.py
	$(PY) scripts/verify_excerpts.py
