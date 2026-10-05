"""Ask Claude to judge candidate links against the paragraphs that produced them.

Ported from POTS-phenotyping. Each candidate is one paper and one link, with
the paragraph(s) where the decision models found it. Claude gets
<run>/JUDGE_INSTRUCTIONS.md as its system prompt and, as the message, the claim
(with short definitions) and the numbered paragraphs. It returns supports /
partial / not_stated, the passage number and a verbatim quote. Calls are made
with `claude -p` (pinned model, no tools, no history, no settings, empty
working directory, no API key) and stored per call with their input hashes, so
a finished call is never repeated.

    <run>/judge/r<N>/<candidate>.json       Claude's answer
    <run>/judge/r<N>/<candidate>.meta.json  inputs, model, usage, quote check

The default model is the one the POTS-phenotyping judge was validated with
(10 of 12 blind-adjudicated disagreements matched the person), so the two
projects' links are judged alike. Pass --model to change it.

Usage::

    python scripts/claude_judge.py --run extraction/<run> --repeats 3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from exposome_ehr import decision as D  # noqa: E402
from exposome_ehr.config import load_vocabulary  # noqa: E402
from quotes import match, variants  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["supports", "partial", "not_stated"]},
        "passage": {"type": "integer", "minimum": 0, "maximum": 3},
        "quote": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "passage", "quote", "reason"],
    "additionalProperties": False,
}


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def message(cand: dict, paras: dict[str, dict], vocab) -> tuple[str, list[str]]:
    tv = D.terms(vocab)
    s, p, o = cand["edge"]
    spec = vocab.predicates[p]
    texts = [paras[f"{cand['paper']}:{pid}"] for pid in cand["paragraphs"]]
    lines = [f"Claim: {spec['statement'].format(a=tv[s]['label'], b=tv[o]['label'])}", "",
             f"A = {tv[s]['label']}: {tv[s]['gloss'] or tv[s]['label']}",
             f"B = {tv[o]['label']}: {tv[o]['gloss'] or tv[o]['label']}",
             f"Relation: {p} ({spec['option']})", ""]
    for i, t in enumerate(texts, 1):
        lines += [f"<passage {i}> (PMID {cand['paper']}, {t['section']}, paragraph {t['id'].split(':')[1]})",
                  t["raw_text"].strip(), f"</passage {i}>", ""]
    lines.append("Do the passages state this claim?")
    return "\n".join(lines), [t["raw_text"] for t in texts]


def command(model: str, effort: str, schema_json: str, system_prompt_file: Path) -> list[str]:
    return ["claude", "-p", "--model", model, "--effort", effort,
            "--system-prompt-file", str(system_prompt_file), "--json-schema", schema_json,
            "--output-format", "json", "--tools", "", "--setting-sources", "", "--no-session-persistence"]


def run_one(job: dict, env: dict) -> str:
    out, meta_path = job["out"], job["meta"]
    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with tempfile.TemporaryDirectory() as cwd:   # nothing to auto-discover here
        proc = subprocess.run(job["cmd"], input=job["msg"], capture_output=True, text=True, cwd=cwd, env=env, timeout=900)
    try:
        res = json.loads(proc.stdout)
    except json.JSONDecodeError:
        res = {"is_error": True, "result": proc.stdout[-500:] or proc.stderr[-500:]}
    data, models = res.get("structured_output"), sorted(res.get("modelUsage") or {})
    if res.get("is_error") or not isinstance(data, dict) or models != [job["model"]]:
        return f"FAILED {job['label']}: {str(res.get('result'))[:150]} {models}"
    quote = data.get("quote") or ""
    found = match(quote, [v for t in job["texts"] for v in variants(t)]) if quote else None
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    meta_path.write_text(json.dumps({"candidate": job["id"], "repeat": job["repeat"], "inputs": job["hashes"],
                                     "models_used": models, "claude_cli": job["cli"],
                                     "wall_seconds": round(time.time() - t0, 1),
                                     "usage": {k: (res.get("usage") or {}).get(k) for k in
                                               ("input_tokens", "cache_read_input_tokens", "output_tokens")},
                                     "quote_check": found or ("empty" if not quote else "not_found")}, indent=1) + "\n")
    return f"ok {job['label']}: {data['verdict']}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--paragraphs", type=Path, default=ROOT / "work/paragraphs")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--effort", default="high")
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args()
    run = args.run.resolve()
    system_file = run / "JUDGE_INSTRUCTIONS.md"
    if not system_file.exists():
        system_file.write_text((ROOT / "extraction/JUDGE_INSTRUCTIONS.md").read_text())
    system = system_file.read_text()
    vocab = load_vocabulary()
    cands = json.loads((run / "candidates.json").read_text())["candidates"]
    paras: dict[str, dict] = {}
    for paper in {c["paper"] for c in cands}:
        for p in json.loads((args.paragraphs / f"{paper}.json").read_text())["paragraphs"]:
            paras[p["id"]] = p
    schema_json = json.dumps(SCHEMA, separators=(",", ":"))
    cmd = command(args.model, args.effort, schema_json, system_file)
    cli = subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip()
    jobs, skipped = [], 0
    for c in cands:
        msg, texts = message(c, paras, vocab)
        h = {"system_prompt": sha(system), "message": sha(msg), "schema": sha(schema_json),
             "model": args.model, "effort": args.effort}
        h["call"] = sha(json.dumps([h["system_prompt"], h["message"], h["schema"], args.model, args.effort]))
        for r in range(1, args.repeats + 1):
            out = run / "judge" / f"r{r}" / f"{c['id']}.json"
            meta = out.with_suffix(".meta.json")
            if out.exists() and meta.exists() and json.loads(meta.read_text())["inputs"]["call"] == h["call"]:
                skipped += 1
                continue
            jobs.append({"id": c["id"], "repeat": r, "label": f"r{r}/{c['id']}", "out": out, "meta": meta, "msg": msg,
                         "texts": texts, "cmd": cmd, "hashes": h, "cli": cli, "model": args.model})
    print(f"{len(jobs)} calls to make, {skipped} already done", flush=True)
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)   # use the signed-in subscription, as in POTS
    failed = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for line in pool.map(lambda j: run_one(j, env), jobs):
            print(line, flush=True)
            failed += line.startswith("FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
