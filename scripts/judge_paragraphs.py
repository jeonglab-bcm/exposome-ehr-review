"""Find literature links paragraph by paragraph with a System One decision model.

The four gated stages are in src/exposome_ehr/decision.py; the questions come
from config/facets.yaml and config/relations.yaml. Every round is stored with
its probabilities and no paragraph text:

    <out>/<folder>/<pmid>.jsonl   one line per paragraph: rounds by stage
    <out>/<folder>/run.json       model, backend, questions hash, papers, tokens

A hosted backend (typesafe) is sent only papers whose licence is CC BY or CC0
(<run>/papers.tsv, column cc_by); the rest are skipped and listed.

Usage::

    python scripts/judge_paragraphs.py --run extraction/2026-10-05-seeds --model winnow:e4b
    python scripts/judge_paragraphs.py --run extraction/2026-10-05-seeds --backend bioinfolder --model kev-4b
    python scripts/judge_paragraphs.py --run extraction/2026-10-05-seeds --backend typesafe --model jev-1.13.0
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from exposome_ehr import decision as D  # noqa: E402
from exposome_ehr.config import load_vocabulary  # noqa: E402


def papers_table(run: Path) -> dict[str, dict]:
    lines = (run / "papers.tsv").read_text().splitlines()
    head = lines[0].split("\t")
    return {row[0]: dict(zip(head, row)) for row in (line.split("\t") for line in lines[1:]) if row}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=Path, required=True, help="run folder with papers.tsv")
    ap.add_argument("--paragraphs", type=Path, default=ROOT / "work/paragraphs")
    ap.add_argument("--model", required=True)
    ap.add_argument("--backend", choices=["ollaya", "typesafe", "llamacpp", "bioinfolder"], default="ollaya")
    ap.add_argument("--papers", nargs="+", help="PMIDs (default: every paper in papers.tsv)")
    ap.add_argument("--gate", type=float, default=D.GATE)
    ap.add_argument("--folder", help="output folder name (default: the model name)")
    ap.add_argument("--resume", action="store_true", help="skip papers whose output already exists")
    ap.add_argument("--one-term-question", action="store_true",
                    help="stage 1 as a single question over every term (needs a 255-option model, e.g. Jev)")
    args = ap.parse_args()

    vocab = load_vocabulary()
    be = D.backend(args.backend)
    n_terms = sum(v["group"] in D.link_groups(vocab) for v in D.terms(vocab).values())
    if args.one_term_question and n_terms + 1 > be.max_options:
        sys.exit(f"{args.model} takes at most {be.max_options} options; drop --one-term-question")
    table = papers_table(args.run)
    wanted = args.papers or list(table)
    if be.hosted:
        refused = [p for p in wanted if table.get(p, {}).get("cc_by") != "yes"]
        if refused:
            print(f"hosted backend: not sending {len(refused)} papers without a CC BY licence: {' '.join(refused)}")
        wanted = [p for p in wanted if p not in refused]

    out = args.run / "runs" / (args.folder or args.model.replace(":", "_").replace("/", "_"))
    out.mkdir(parents=True, exist_ok=True)
    previous = json.loads((out / "run.json").read_text()) if (out / "run.json").exists() else {}
    qsha = D.questions_sha(vocab)
    if previous and previous.get("questions_sha") != qsha:
        sys.exit(f"{out} was asked different questions ({previous.get('questions_sha')} != {qsha}); use a new --folder")
    for pmid in wanted:
        if args.resume and (out / f"{pmid}.jsonl").exists():
            continue
        paper = json.loads((args.paragraphs / f"{pmid}.json").read_text())
        t0 = time.time()
        recs = [D.judge(be, args.model, p, vocab, args.gate, args.one_term_question) for p in paper["paragraphs"]]
        (out / f"{pmid}.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in recs))
        n_links = len({e for r in recs for e in D.cascade(r, vocab)})
        print(f"{args.model} {pmid}: {len(recs)} paragraphs, {n_links} links, {time.time() - t0:.0f}s", flush=True)

    answered = sorted(set(previous.get("answered_by") or []) | {m for m in be.answered_by if m})
    info = {"model": args.model, "backend": args.backend, "url": be.url, "gate": args.gate, "questions_sha": qsha,
            "term_question": "one question over all terms" if args.one_term_question else "one question per group",
            "answered_by": answered,
            "papers": sorted(set(previous.get("papers") or []) | set(wanted), key=int),
            "input_tokens": (previous.get("input_tokens") or 0) + be.input_tokens}
    (out / "run.json").write_text(json.dumps(info, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
