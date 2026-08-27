# Makefile for the all-age exposome / EWAS literature collection + summarizer.
#
#   make help        show available targets
#   make setup       create venv + install all deps
#   make download    resume the literature discovery/full-text fetcher
#   make clean       remove downloaded PDFs + download log
#   make fresh       clean + download (start over)
#   make summary     print a compact inventory + regenerate paper_summary.md
#   make summarize   summarize all manuscripts via the configured LLM -> JSON
#   make scan        enrich summaries with focused data-availability evidence
#   make results     export readable results/ (SUMMARY.md, checklist.md, combined JSON)
#   make summarize-paper PMC=PMC7145790   summarize one paper
#   make test        run the summarizer unit tests
#
# Override the interpreter via env vars if needed:
#   make download PYTHON=python3.11

PYTHON        ?= python3
SCRIPT        := fetch_pmc_papers.py
SUMMARIZE_ARGS ?= --workers 4
SCAN_ARGS      ?= --workers 4
PAPERS_DIR    := papers
DOWNLOAD_LOG  := $(PAPERS_DIR)/download_log.json
MANIFEST      := $(PAPERS_DIR)/manifest.json
COMBINED_JSON := $(PAPERS_DIR)/manuscript_summaries.json
VENV          := .venv
PIP           := $(VENV)/bin/pip
VENV_PYTHON   := $(VENV)/bin/python

.PHONY: help setup download clean fresh summary summarize summarize-paper scan test rebuild-catalog results db-import db-export db-stats dagster materialize web site check-artifacts

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: $(VENV) ## Create virtualenv and install all deps
	@$(PIP) install --quiet --upgrade pip
	@$(PIP) install --quiet requests openai pypdf pydantic python-dotenv pytest tinydb dagster
	@echo "✓ venv ready at $(VENV)"

$(VENV):
	@$(PYTHON) -m venv $(VENV)
	@echo "✓ created venv at $(VENV)"

download: $(VENV) ## Resume discovery/download, reconciling existing state
	@$(VENV_PYTHON) $(SCRIPT)

$(DOWNLOAD_LOG): $(SCRIPT) $(VENV)
	@$(VENV_PYTHON) $(SCRIPT)

clean: ## Remove downloaded PDFs/XML + download log + summaries
	@rm -f $(PAPERS_DIR)/*.pdf $(PAPERS_DIR)/*.xml $(DOWNLOAD_LOG) $(MANIFEST) $(PAPERS_DIR)/db.json
	@rm -rf $(PAPERS_DIR)/summaries $(COMBINED_JSON)
	@echo "✓ cleaned $(PAPERS_DIR) (full text + manifest/log + summaries/DB removed)"

fresh: clean download ## Clean then re-download from scratch

summarize: $(VENV) ## Resume summarization; cached valid papers are skipped
	@$(VENV_PYTHON) -m summarizer.run --recover $(SUMMARIZE_ARGS)

summarize-paper: $(VENV) ## Summarize a single paper: make summarize-paper PMC=PMC7145790
	@test -n "$(PMC)" || (echo "Usage: make summarize-paper PMC=PMC7145790"; exit 2)
	@$(VENV_PYTHON) -m summarizer.run --pmcid $(PMC)

scan: $(VENV) ## Enrich current summaries with data-availability evidence
	@$(VENV_PYTHON) scan_data_availability.py $(SCAN_ARGS)

test: $(VENV) ## Run summarizer unit tests (no live API calls)
	@$(VENV_PYTHON) -m pytest tests/ -q

rebuild-catalog: $(VENV) ## Replace TinyDB + combined JSON from current valid per-paper summaries
	@$(VENV_PYTHON) -c "from pipeline_ops import rebuild_combined; data = rebuild_combined(); print(f'✓ rebuilt exact DB + combined catalog ({data[\"n\"]} records)')"

results: rebuild-catalog ## Exact-rebuild DB + combined JSON, then export readable results/
	@$(VENV_PYTHON) build_results.py
	@echo "✓ wrote results/ (SUMMARY.md, checklist.md, manuscript_summaries.json)"

db-import: rebuild-catalog ## Exact-replace TinyDB from current valid per-paper JSONs
	@echo "✓ TinyDB exactly mirrors the current publishable per-paper summaries"

db-export: rebuild-catalog ## Rebuild TinyDB first, then export combined + tracked results copy
	@$(VENV_PYTHON) db.py export
	@echo "✓ exported from an exact-rebuilt TinyDB store (papers/db.json)"

db-stats: $(VENV) ## Print TinyDB store record counts
	@$(VENV_PYTHON) db.py stats

dagster: $(VENV) ## Launch the Dagster asset UI + lineage browser
	@$(VENV)/bin/dagster dev -m pipeline

materialize: $(VENV) ## Materialize the whole asset graph (fetch -> screen -> summarize -> scan -> publish)
	@$(VENV)/bin/dagster asset materialize -m pipeline --select "*"

web: $(VENV) ## Serve the browsable review web app on http://localhost:8010
	@$(VENV_PYTHON) webapp.py

site: results ## Build the static GitHub Pages site (docs/index.html + tailwind.css)
	@$(VENV_PYTHON) build_site.py
	@command -v npx >/dev/null || { echo "npx is required to build docs/tailwind.css"; exit 2; }
	@npx --yes tailwindcss@3 -i docs/tailwind.input.css -o docs/tailwind.css \
		--content docs/index.html --minify
	@echo "✓ wrote docs/index.html + docs/tailwind.css (open docs/index.html)"

check-artifacts: $(VENV) ## Fail if manifest, files, summaries, DB, reports, or site drift
	@$(VENV_PYTHON) artifact_consistency.py

summary: $(DOWNLOAD_LOG) ## Print a compact inventory + regenerate paper_summary.md
	@$(VENV_PYTHON) build_summary.py
	@echo ""
	@$(VENV_PYTHON) -c "\
import json, pathlib; \
log = json.loads(pathlib.Path('$(DOWNLOAD_LOG)').read_text()); \
papers = log.get('papers', []); \
candidates = log.get('candidates', papers); \
screening = log.get('screening', {}); \
included = screening.get('included', papers); \
pending = screening.get('pending', log.get('pending', [])); \
excluded = screening.get('excluded', log.get('excluded', [])); \
print(f'{\"#\":<4} {\"PMCID\":<14} {\"Yr\":<5} {\"Journal\":<22} Title'); \
print('-' * 100); \
[print(f'{i:<4} {p[\"pmcid\"]:<14} {p[\"year\"]:<5} {p[\"journal\"][:21]:<22} {p[\"title\"][:60]}') \
 for i, p in enumerate(papers, 1)]; \
print(f'\nCandidates: {len(candidates)} | Included: {len(included)} | ' \
      f'Downloaded: {len(log.get(\"downloaded\", []))} | Pending: {len(pending)} | ' \
      f'Excluded: {len(excluded)} | Abstract-only: {len(log.get(\"abstract_only\", []))}')"
