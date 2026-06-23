"""LLM client + structured-output extraction for manuscript summarization.

Talks to the external OpenAI-compatible Gemma 4 12B endpoint via the ``openai``
SDK. The model tends to wrap JSON in chain-of-thought reasoning and markdown
fences, so we extract the last ```json``` block (or the largest balanced
``{...}``) and validate with Pydantic, retrying with a corrective nudge on
failure.

Configuration is entirely env-based so no API key is ever committed:

    GEMMA_BASE_URL   default http://bioinfolder.com:8000/v1
    GEMMA_API_KEY    required (placeholder sk-unsloth-PLACEHOLDER)
    GEMMA_MODEL      default unsloth/gemma-4-12B-it-qat-GGUF
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from openai import OpenAI
from pydantic import ValidationError

from .schema import LLM_FIELDS_SCHEMA, ManuscriptChecklist

# ── config ───────────────────────────────────────────────────────────────────
DEFAULT_BASE_URL = "http://bioinfolder.com:8000/v1"
DEFAULT_MODEL = "unsloth/gemma-4-12B-it-qat-GGUF"
PLACEHOLDER_KEY = "sk-unsloth-PLACEHOLDER"

MAX_RETRIES = 3
MAX_OUTPUT_TOKENS = 3072
# Approximate char budget for the source text sent to the model. The model
# reasons heavily, so a tighter budget leaves tokens for the JSON output.
SOURCE_CHAR_BUDGET = 6000


def _env(key: str, default: str) -> str:
    val = os.environ.get(key, "").strip()
    return val if val else default


def get_client() -> tuple[OpenAI, str]:
    """Build an OpenAI client + model id from env vars."""
    api_key = os.environ.get("GEMMA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "GEMMA_API_KEY is not set. Copy .env.example to .env and fill in the key, "
            "or export GEMMA_API_KEY in your shell."
        )
    base_url = _env("GEMMA_BASE_URL", DEFAULT_BASE_URL)
    model = _env("GEMMA_MODEL", DEFAULT_MODEL)
    # explicit timeout so a stalled connection cannot hang the whole batch.
    return OpenAI(base_url=base_url, api_key=api_key, timeout=120.0), model


# ── prompt ───────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = (
    "You are a strict structured-data extractor for a systematic review on "
    "pediatric environmental-exposure (exposome / EWAS) studies that use EHR "
    "or linked health data. Read the manuscript text and extract a single JSON "
    "object with EXACTLY these keys and no others: ehr_used, ehr_evidence, "
    "summary, key_findings, captured_features, pathologies_diseases, "
    "study_design, data_source_type, population, exposure_domain, "
    "limitations, confidence. Do NOT invent keys like article, authors, title, "
    "abstract. ehr_used must be a JSON boolean (true/false). confidence must be "
    "one of: high, medium, low, unclear. Output ONLY a JSON object starting "
    "with { and ending with } — no markdown fences, no reasoning, no prose."
)


def build_user_prompt(text: str) -> str:
    keys = list(LLM_FIELDS_SCHEMA["properties"].keys())
    example = {
        "ehr_used": True,
        "ehr_evidence": "We used Hospital Episode Statistics (HES).",
        "summary": "EWAS of childhood T1DM across England.",
        "key_findings": ["15 of 53 environmental factors associated with T1DM."],
        "captured_features": ["HES ICD codes", "incident diabetes cases"],
        "pathologies_diseases": ["type 1 diabetes"],
        "study_design": "ecological EWAS",
        "data_source_type": "EHR",
        "population": "children 0-9 yrs, England",
        "exposure_domain": "air pollution",
        "limitations": ["ecological design"],
        "confidence": "medium",
    }
    return (
        "Extract the checklist JSON for this manuscript.\n\n"
        "Use EXACTLY these keys and no others:\n"
        + json.dumps(keys) + "\n\n"
        "Example output shape:\n"
        + json.dumps(example, ensure_ascii=False) + "\n\n"
        "Rules:\n"
        "- ehr_used is a JSON boolean (true/false).\n"
        "- confidence is lowercase: high, medium, low, or unclear.\n"
        "- Output ONLY the JSON object, starting with { and ending with }.\n\n"
        "=== MANUSCRIPT TEXT ===\n" + text
    )


# ── JSON extraction ──────────────────────────────────────────────────────────
_FENCED_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


def extract_json_object(raw: str) -> dict[str, Any] | None:
    """Pull the first valid JSON object out of a chatty model response.

    Tries, in order: the last ```json fenced block, then any fenced block, then
    the largest balanced ``{...}`` span. Returns None if nothing parses.
    """
    if not raw:
        return None

    fenced = _FENCED_RE.findall(raw)
    for cand in reversed(fenced):
        obj = _try_parse_repair(cand)
        if obj is not None:
            return obj

    # fall back to the largest balanced object span
    best = _largest_balanced_object(raw)
    if best is not None:
        return _try_parse_repair(best)

    # last resort: first '{' to last '}' (handles reasoning prefix + trailing junk)
    if "{" in raw and "}" in raw:
        return _try_parse_repair(raw[raw.index("{") : raw.rindex("}") + 1])
    return None


def _try_parse_repair(s: str) -> dict[str, Any] | None:
    """json.loads with a light repair for truncated responses (close open braces)."""
    s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # truncated mid-object: append the missing closing braces
    opens = s.count("{") - s.count("}")
    if opens > 0:
        try:
            # drop a trailing incomplete key/value, then close
            trimmed = s.rstrip()
            if trimmed.endswith(",") or trimmed.endswith('"'):
                trimmed = trimmed.rstrip(',"')
            return json.loads(trimmed + ("}" * opens))
        except json.JSONDecodeError:
            pass
    return None


def _largest_balanced_object(text: str) -> str | None:
    """Return the largest balanced ``{...}`` substring, naive but effective."""
    best = ""
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    cand = text[start : i + 1]
                    if len(cand) > len(best):
                        best = cand
                    start = -1
    return best or None


# ── summarization ────────────────────────────────────────────────────────────
def summarize_text(
    text: str,
    pmcid: str,
    title: str,
    year: str,
    source_format: str,
    client: OpenAI | None = None,
    model: str | None = None,
) -> ManuscriptChecklist:
    """Summarize a single manuscript's text into a validated checklist.

    Retries up to MAX_RETRIES times, feeding the validation error back to the
    model so it can self-correct.
    """
    own_client = client is None
    if own_client:
        client, model = get_client()
    assert client is not None and model is not None

    text = (text or "").strip()[:SOURCE_CHAR_BUDGET]
    if not text:
        raise ValueError(f"No extractable text for {pmcid}")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(text)},
    ]

    last_err = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=MAX_OUTPUT_TOKENS,
                temperature=0.0,
                timeout=120.0,
            )
            raw = resp.choices[0].message.content or ""
        except Exception as e:  # network / API error → wait + retry
            last_err = f"API error: {e}"
            time.sleep(2 * attempt)
            continue

        obj = extract_json_object(raw)
        if obj is None:
            last_err = "No JSON object found in model response."
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": _corrective_nudge(
                "No JSON object could be parsed. Output ONLY a ```json fenced "
                "block containing the JSON object, nothing else.")})
            continue

        try:
            partial = ManuscriptChecklist(
                pmcid=pmcid, title=title, year=year,
                source_format=source_format, model=model, **obj,
            )
            return partial
        except (ValidationError, TypeError) as e:
            last_err = str(e)
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": _corrective_nudge(
                f"Validation failed: {last_err}. Fix the JSON and re-emit ONLY "
                "the ```json fenced block.")})

    raise RuntimeError(f"Failed to summarize {pmcid} after {MAX_RETRIES} attempts: {last_err}")


def _corrective_nudge(msg: str) -> str:
    return msg
