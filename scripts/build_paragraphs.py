"""Build the paragraph files the decision models read, for a set of papers.

For each paper: PMC JATS full text when the paper is in PMC and the XML has a
body, else the PubMed abstract from the corpus record layer. JATS files are
fetched into --jats once and reused. Paragraph texts are copyrighted and go to
--out, which is not committed (work/ is ignored); the run folder gets only the
ids, hashes and licences, so a later parse can be checked against them.

    <out>/<pmid>.json          paragraphs (text; not committed)
    <run>/paragraphs.tsv       id, sha, chars, kind, section (no text)
    <run>/papers.tsv           pmid, pmc, source, licence, cc_by, paragraphs

cc_by decides what may be sent to a hosted model (TypeSafe's Jev): only CC BY
or CC0 text, as in POTS-phenotyping.

Usage::

    python scripts/build_paragraphs.py --papers seeds --fetch --run extraction/2026-10-05-seeds
    python scripts/build_paragraphs.py --papers pmids.txt --fetch --run extraction/<run>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from exposome_ehr import paragraphs as P  # noqa: E402
from exposome_ehr.config import load_vocabulary  # noqa: E402
from exposome_ehr.eutils import EutilsClient  # noqa: E402
from exposome_ehr.store import Store  # noqa: E402


def wanted(spec: str, vocab) -> list[str]:
    if spec == "seeds":
        return [r["expected_pmid"] for r in vocab.seed_references.values() if r.get("expected_pmid")]
    if spec == "all":
        return []
    return [line.split()[0] for line in Path(spec).read_text().splitlines() if line.strip() and not line.startswith("#")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--papers", required=True, help="'seeds', 'all', or a file with one PMID per line")
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--jats", type=Path, default=ROOT / "work/jats")
    ap.add_argument("--out", type=Path, default=ROOT / "work/paragraphs")
    ap.add_argument("--run", type=Path, required=True, help="run folder for the manifest (committed)")
    ap.add_argument("--fetch", action="store_true", help="download missing JATS from PMC")
    args = ap.parse_args()

    vocab = load_vocabulary()
    pmids = wanted(args.papers, vocab)
    store = Store(args.data_dir)
    records = {}
    for r in store.iter_jsonl():
        if not pmids or r["pmid"] in pmids:
            records[r["pmid"]] = r
    store.close()
    missing = [p for p in pmids if p not in records]
    if missing:
        print(f"not in the corpus, skipped: {', '.join(missing)}", file=sys.stderr)

    args.jats.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)
    client = EutilsClient() if args.fetch else None
    built, rows = [], ["pmid\tpmc\tsource\tlicense\tcc_by\tparagraphs"]
    for pmid in (pmids or sorted(records, key=int)):
        if pmid not in records:
            continue
        rec = records[pmid]
        jats = args.jats / f"{rec['pmc']}.xml" if rec.get("pmc") else None
        if jats is not None and not jats.exists() and client is not None:
            jats.write_bytes(client.efetch_pmc_xml(rec["pmc"]))
        paper = P.build(rec, jats)
        (args.out / f"{pmid}.json").write_text(json.dumps(paper, indent=1, ensure_ascii=False) + "\n")
        built.append(paper)
        rows.append(f"{pmid}\t{rec.get('pmc') or ''}\t{paper['source']}\t{paper['license']}\t"
                     f"{'yes' if P.is_cc_by(paper['license']) else 'no'}\t{len(paper['paragraphs'])}")
        print(f"{pmid} {paper['source']:8s} {len(paper['paragraphs']):4d} paragraphs  {paper['license'][:60]}")
    args.run.mkdir(parents=True, exist_ok=True)
    (args.run / "paragraphs.tsv").write_text(P.manifest(built))
    (args.run / "papers.tsv").write_text("\n".join(rows) + "\n")
    print(f"{len(built)} papers, {sum(len(p['paragraphs']) for p in built)} paragraphs "
          f"({sum(p['source'] == 'jats' for p in built)} full text)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
