"""Code each paper with the typed questions in config/study_coding.yaml.

This replaces the old summarizer: no generative model, no free-text JSON. Each
field is a yes/no or a fixed set of options, asked of the passages the field
reads (abstract, methods, data-availability statement) and stored per passage
with probabilities, never text:

    <run>/coding/<folder>/<pmid>.json   per field, per passage: the answers
    <run>/coding/<folder>/run.json      model, backend, questions hash

`--table` reads the stored answers (no model calls) and writes one row per
paper, with the paragraph id that carried each answer:

    <run>/coding/<folder>/studies.tsv

Usage::

    python scripts/code_studies.py --run extraction/<run> --backend bioinfolder --model kev-4b
    python scripts/code_studies.py --run extraction/<run> --folder kev-4b --table
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from exposome_ehr import decision as D  # noqa: E402
from exposome_ehr.config import load_vocabulary  # noqa: E402
from judge_paragraphs import papers_table  # noqa: E402


def table(folder: Path, coding: dict, gate: float) -> str:
    fields = list(coding["fields"])
    rows = ["pmid\tsource\t" + "\t".join(f"{f}\t{f}_evidence" for f in fields)]
    for path in sorted(folder.glob("*.json"), key=lambda p: (not p.stem.isdigit(), p.stem.zfill(12))):
        if path.name == "run.json":
            continue
        rec = json.loads(path.read_text())
        cells = [rec["paper"], rec.get("source") or ""]
        for f in fields:
            spec, answers = coding["fields"][f], rec["fields"].get(f, [])
            ans = D.coding_answer(spec, answers, gate)
            ev = D.evidence_for(spec, answers, ans)
            cells += ["" if ans is None else ";".join(ans) if isinstance(ans, list) else str(ans).lower(), ev or ""]
        rows.append("\t".join(cells))
    return "\n".join(rows) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--paragraphs", type=Path, default=ROOT / "work/paragraphs")
    ap.add_argument("--model")
    ap.add_argument("--backend", choices=["ollaya", "typesafe", "llamacpp", "bioinfolder"], default="ollaya")
    ap.add_argument("--folder")
    ap.add_argument("--papers", nargs="+")
    ap.add_argument("--limit", type=int, default=12, help="passages per kind per field")
    ap.add_argument("--gate", type=float, default=D.GATE)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--table", action="store_true", help="write studies.tsv from stored answers; no model calls")
    args = ap.parse_args()

    vocab = load_vocabulary()
    coding = yaml.safe_load((ROOT / "config/study_coding.yaml").read_text())
    name = args.folder or (args.model or "").replace(":", "_").replace("/", "_")
    if not name:
        sys.exit("give --model, or --folder with --table")
    out = args.run / "coding" / name
    if args.table:
        (out / "studies.tsv").write_text(table(out, coding, args.gate))
        print(f"wrote {out / 'studies.tsv'}")
        return 0
    if not args.model:
        sys.exit("--model is required to ask questions")

    be = D.backend(args.backend)
    tbl = papers_table(args.run)
    wanted = args.papers or list(tbl)
    if be.hosted:
        refused = [p for p in wanted if tbl.get(p, {}).get("cc_by") != "yes"]
        if refused:
            print(f"hosted backend: not sending {len(refused)} papers without a CC BY licence")
        wanted = [p for p in wanted if p not in refused]
    out.mkdir(parents=True, exist_ok=True)
    qsha = D.questions_sha(vocab, coding)
    previous = json.loads((out / "run.json").read_text()) if (out / "run.json").exists() else {}
    if previous and previous.get("questions_sha") != qsha:
        sys.exit(f"{out} was asked different questions; use a new --folder")
    for pmid in wanted:
        if args.resume and (out / f"{pmid}.json").exists():
            continue
        paper = json.loads((args.paragraphs / f"{pmid}.json").read_text())
        rec = D.code_paper(be, args.model, paper, coding, vocab, args.limit)
        (out / f"{pmid}.json").write_text(json.dumps(rec, indent=1, sort_keys=True) + "\n")
        print(f"{args.model} {pmid}: coded", flush=True)
    info = {"model": args.model, "backend": args.backend, "questions_sha": qsha, "limit": args.limit,
            "answered_by": sorted(set(previous.get("answered_by") or []) | {m for m in be.answered_by if m}),
            "papers": sorted(set(previous.get("papers") or []) | set(wanted), key=int),
            "input_tokens": (previous.get("input_tokens") or 0) + be.input_tokens}
    (out / "run.json").write_text(json.dumps(info, indent=1) + "\n")
    (out / "studies.tsv").write_text(table(out, coding, args.gate))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
