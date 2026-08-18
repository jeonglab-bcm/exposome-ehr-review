# Running the summarizer against a local LLM (oMLX)

The pipeline talks to any **OpenAI-compatible** endpoint via three env vars
(`GEMMA_BASE_URL`, `GEMMA_API_KEY`, `GEMMA_MODEL` — see `.env.example`). The
default is the homelab server (`https://llm.bioinfolder.com/v1`). This page
documents the fallback: running the models **locally on an Apple Silicon Mac**
via [oMLX](https://github.com/unsloth/omlx) — useful when the homelab
endpoint is down, for offline work, or for targeted batch follow-ups
(re-summarizing a handful of papers).

Tested on: **M2 Max, 32 GB unified memory**, oMLX 0.6.1.

## Models (tested)

| Model (HF) | Weights | Notes |
|---|---|---|
| `mlx-community/Qwen3.6-35B-A3B-4bit` | ~19 GB | **Recommended.** Same family as the corpus (`qwen3.6-35b-vllm-nvfp4`). MoE A3B: only ~3B params active per token, so it runs fast despite 35B weights. |
| `mlx-community/gemma-3-12b-it-qat-4bit` | ~7.5 GB | Lightweight fallback, 128k context. Smaller model — more prone to anchoring on the prompt's example output (see PR #43 for the prompt hardening that made it reliable). |

Both fit a 32 GB Mac (macOS caps the GPU at ~25.6 GB). They **cannot both be
resident at once** — oMLX's LRU swaps models on first use (~30–60 s reload).

## Install a model

```bash
# 1. download into the HF cache (external SSD recommended)
hf download mlx-community/Qwen3.6-35B-A3B-4bit

# 2. link the snapshot into oMLX's model dir (flat subdirs, each with
#    config.json + *.safetensors). Symlink — no extra disk space.
SNAP=$(ls -d "$HF_HUB_CACHE"/models--mlx-community--Qwen3.6-35B-A3B-4bit/snapshots/*/ | head -1)
ln -s "$SNAP" /Volumes/SSD/caches/omlx/Jundot/Qwen3.6-35B-A3B-4bit

# 3. restart the server (models are discovered at startup)
kill $(pgrep -f omlx-server) && omlx start

# 4. verify
curl -s http://localhost:8000/v1/models -H "Authorization: Bearer no-key"
```

## Point the pipeline at it

```dotenv
# .env (gitignored)
GEMMA_BASE_URL=http://localhost:8000/v1
GEMMA_API_KEY=no-key
GEMMA_MODEL=Qwen3.6-35B-A3B-4bit
```

`summarizer/run.py` loads `.env` automatically (python-dotenv). The venv needs
`openai`, `python-dotenv`, `pypdf`, `requests`, `pydantic`, `tinydb`.

Smoke test before any batch run:

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer no-key" -H "Content-Type: application/json" \
  -d '{"model":"Qwen3.6-35B-A3B-4bit","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":20}'
```

## Gotcha: thinking models

Qwen3.6 is a *thinking* model. The pipeline sends
`extra_body={"enable_thinking": False}`, but **oMLX does not pass that field
through** — instead it reads a per-model setting. Without the setting,
responses start with `Thinking Process: …` and the JSON extraction breaks.

Fix — add to `~/.omlx/model_settings.json` (edit while the server is **stopped**;
settings are loaded at startup):

```json
{
  "models": {
    "Qwen3.6-35B-A3B-4bit": { "enable_thinking": false }
  }
}
```

(Add both the short dir name and the `mlx-community--…` HF-style id to be safe.)

## Offloading / swapping models

- oMLX is multi-model with LRU memory management: a model loads on first
  request and is evicted under memory pressure.
- To fully offload a model: `kill $(pgrep -f omlx-server) && omlx start`.
- **Reliability note:** the managed control channel is flaky — `omlx restart`
  and sometimes `omlx start` fail with `Failed to control oMLX.app: … control
  socket`. The manual `kill` + `omlx start` (retry once if needed) is the
  reliable sequence.

## Pipeline caveats when re-summarizing locally

1. **Corpus model parity** — each summary records the `model` it was made
   with. Re-run follow-ups with the corpus family (Qwen3.6-35B) so the
   collection stays one model family.
2. **DA fields reset** — re-summarizing rewrites the per-paper JSON with the
   LLM output, which resets `data_availability`/`data_accession_links` to
   schema defaults. Re-run the scan for the touched papers:
   `python scan_data_availability.py --pmcid <PMC>` (this also refreshes the
   combined JSON — see `scan_data_availability.py`'s end-of-`main` rebuild).
3. **Year comes from the download log** — `summarizer/run.py` fills `year`
   from `papers/download_log.json`, not the LLM. Papers missing from the log
   lose their year on re-summarization; keep the log complete.

## End-to-end check

```bash
.venv/bin/python -m summarizer.run --pmcid <PMC> --force   # re-summarize one paper
.venv/bin/python db.py import --from-dir papers/summaries  # sync DB
.venv/bin/python db.py export                              # sync combined + results
```
