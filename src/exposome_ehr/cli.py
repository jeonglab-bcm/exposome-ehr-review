"""Command line interface.

    exposome-ehr show-query --profile core
    exposome-ehr validate-vocab
    exposome-ehr harvest --profile core
    exposome-ehr ontology [--athena-dir DIR]
    exposome-ehr report --out docs/HARVEST_REPORT.md
    exposome-ehr graph --node particulate_matter
    exposome-ehr rebuild
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .config import load_vocabulary, repo_root
from .eutils import EutilsClient
from .harvest import Harvester, check_mesh_terms, rebuild_from_jsonl
from .ontology import OntologyBuilder
from .report import build_report, export_derived, run_checks
from .store import Store


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def _store(args: argparse.Namespace) -> Store:
    return Store(Path(args.data_dir))


# ------------------------------------------------------------------ commands
def cmd_show_query(args: argparse.Namespace) -> int:
    vocab = load_vocabulary()
    if args.facet:
        print(vocab.facet_query(args.facet, args.profile))
        return 0
    print(vocab.corpus_query(args.profile))
    if args.blocks:
        print("\n--- clauses ---", file=sys.stderr)
        for block in vocab.profile_blocks(args.profile):
            print(f"[{block['id']}] {block['query']}", file=sys.stderr)
    return 0


def cmd_validate_vocab(args: argparse.Namespace) -> int:
    vocab = load_vocabulary()
    problems = vocab.validate()
    if problems:
        for problem in problems:
            print(f"PROBLEM: {problem}")
        return 1
    print(
        f"vocabulary clean: {len(vocab.facet_ids)} facets, "
        f"{len(vocab.nodes)} nodes, {len(vocab.edges)} edges, "
        f"{len(vocab.predicates)} predicates"
    )
    return 0


def cmd_counts(args: argparse.Namespace) -> int:
    """esearch-only reconnaissance: how big is everything, with no fetching."""
    vocab = load_vocabulary()
    client = EutilsClient()
    corpus = client.esearch(vocab.corpus_query(args.profile))
    print(f"corpus\t{corpus.count}")
    for block in vocab.profile_blocks(args.profile):
        result = client.esearch(block["query"])
        print(f"block:{block['id']}\t{result.count}")
    if args.facets:
        for facet_id in vocab.facet_ids:
            result = client.esearch(vocab.facet_query(facet_id, args.profile))
            print(f"facet:{facet_id}\t{result.count}")
    return 0


def cmd_harvest(args: argparse.Namespace) -> int:
    vocab = load_vocabulary()
    problems = vocab.validate()
    if problems:
        print("refusing to harvest with an inconsistent vocabulary:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    store = _store(args)
    harvester = Harvester(store, vocab, profile=args.profile)
    summary = harvester.run(
        limit=args.limit, skip_facets=args.skip_facets, batch_size=args.batch_size
    )
    print(json.dumps(summary.as_dict(), indent=2))
    store.close()
    return 0


def cmd_seeds(args: argparse.Namespace) -> int:
    """Re-resolve the seed references against the stored corpus, no re-harvest."""
    vocab = load_vocabulary()
    store = _store(args)
    store.upsert_vocabulary(vocab)
    harvester = Harvester(store, vocab, profile=args.profile)
    harvester.resolve_seed_references()
    for row in store.rows("SELECT * FROM seed_reference ORDER BY ref_id"):
        print(
            f"{row['ref_id']:12s} resolved={row['resolved_pmid'] or '-':10s}"
            f" method={row['match_method'] or '-':12s}"
            f" in_corpus={bool(row['found_in_corpus'])}"
            f" expected={row['expected_pmid'] or '-':10s}"
            f" expected_in_corpus={bool(row['expected_pmid_in_corpus'])}"
        )
    store.close()
    return 0


def cmd_check_mesh(args: argparse.Namespace) -> int:
    """Validate the declared MeSH descriptor names against PubMed's MeSH index."""
    vocab = load_vocabulary()
    store = _store(args)
    stats = check_mesh_terms(store, vocab, EutilsClient())
    print(json.dumps(stats, indent=2))
    for row in store.rows(
        "SELECT facet_id, descriptor_name, pubmed_count, translation FROM mesh_term_check"
        " WHERE is_valid = 0 ORDER BY facet_id, descriptor_name"
    ):
        print(
            f"INVALID {row['facet_id']}: {row['descriptor_name']!r} "
            f"({row['pubmed_count']} hits) -> {row['translation'][:100]}"
        )
    store.close()
    return 1 if (stats["invalid"] and args.strict) else 0


def cmd_ontology(args: argparse.Namespace) -> int:
    vocab = load_vocabulary()
    store = _store(args)
    builder = OntologyBuilder(
        store,
        vocab,
        athena_dir=Path(args.athena_dir) if args.athena_dir else None,
        use_fallbacks=not args.no_fallbacks,
        try_athena_api=not args.no_athena_api,
        max_candidates=args.max_candidates,
        fetch_hierarchy=not args.no_hierarchy,
    )
    report = builder.run()
    print(
        json.dumps(
            {
                "resolvers_used": report.resolvers_used,
                "resolvers_unavailable": report.resolvers_unavailable,
                "facet_mappings": report.facet_mappings,
                "relationships": report.relationships,
                "by_vocabulary": report.by_vocabulary,
                "unresolved": report.unresolved,
            },
            indent=2,
        )
    )
    store.close()
    # Unresolved concepts are expected (declared fallback_gap terms), so a run
    # that leaves some unresolved is still a success.
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    vocab = load_vocabulary()
    store = _store(args)
    text = build_report(store, vocab)
    exports = export_derived(store, vocab)
    out_path = Path(args.out) if args.out else None
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"wrote {out_path}", file=sys.stderr)
    else:
        print(text)
    for path in exports:
        print(f"exported {path.relative_to(repo_root())}", file=sys.stderr)
    failed = [c for c in run_checks(store, vocab) if not c.passed]
    store.close()
    return 1 if (failed and args.strict) else 0


def cmd_graph(args: argparse.Namespace) -> int:
    """Print the relation neighbourhood of a node."""
    store = _store(args)
    node = args.node
    row = store.rows("SELECT * FROM ontology_node WHERE node_id = ?", (node,))
    if not row:
        print(f"unknown node {node!r}", file=sys.stderr)
        store.close()
        return 1
    print(f"{node}  [{row[0]['kind']}]  {row[0]['label']}")
    if row[0]["definition"]:
        print(f"  definition: {row[0]['definition']}")
    print("\n  outgoing:")
    for edge in store.rows(
        "SELECT predicate, object_id, provenance, source FROM ontology_edge"
        " WHERE subject_id = ? ORDER BY predicate, object_id",
        (node,),
    ):
        print(
            f"    -{edge['predicate']}-> {edge['object_id']}"
            f"   ({edge['source']}: {edge['provenance']})"
        )
    print("\n  incoming:")
    for edge in store.rows(
        "SELECT predicate, subject_id, provenance, source FROM ontology_edge"
        " WHERE object_id = ? ORDER BY predicate, subject_id",
        (node,),
    ):
        print(
            f"    {edge['subject_id']} -{edge['predicate']}-> {node}"
            f"   ({edge['source']}: {edge['provenance']})"
        )
    print("\n  transitive ancestors:")
    for edge in store.rows(
        "SELECT predicate, ancestor_id, min_levels FROM ontology_closure"
        " WHERE descendant_id = ? ORDER BY predicate, min_levels",
        (node,),
    ):
        print(f"    {edge['predicate']} +{edge['min_levels']} -> {edge['ancestor_id']}")
    print("\n  candidate OMOP concepts:")
    for concept in store.rows(
        "SELECT vocabulary_id, concept_code, concept_name, concept_id, resolver, mapping_status"
        " FROM facet_concept WHERE facet_id = ? ORDER BY vocabulary_id, match_rank",
        (node,),
    ):
        print(
            f"    {concept['vocabulary_id']}:{concept['concept_code']}"
            f"  {concept['concept_name']}"
            f"  [concept_id={concept['concept_id']}, {concept['resolver']},"
            f" {concept['mapping_status']}]"
        )
    store.close()
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    """Deduplicate and sort the JSONL record layer. No network."""
    store = _store(args)
    kept, dropped = store.compact_jsonl()
    print(f"{kept} records kept, {dropped} duplicates dropped in {store.jsonl_path}")
    store.close()
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    vocab = load_vocabulary()
    store = _store(args)
    total = rebuild_from_jsonl(store, vocab)
    print(f"rebuilt {total} articles from {store.jsonl_path}")
    store.close()
    return 0


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="exposome-ehr",
        description="Harvest and structure the PubMed literature on the exposome in routinely collected health data.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--data-dir",
        default=str(repo_root() / "data"),
        help="where raw XML, JSONL and the SQLite database live (default: ./data)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("show-query", help="print the PubMed query for a profile or facet")
    p.add_argument("--profile", default="core")
    p.add_argument("--facet")
    p.add_argument("--blocks", action="store_true", help="also list clauses on stderr")
    p.set_defaults(func=cmd_show_query)

    p = sub.add_parser("validate-vocab", help="check the curated vocabulary for consistency")
    p.set_defaults(func=cmd_validate_vocab)

    p = sub.add_parser("counts", help="esearch-only size reconnaissance, no records fetched")
    p.add_argument("--profile", default="core")
    p.add_argument("--facets", action="store_true")
    p.set_defaults(func=cmd_counts)

    p = sub.add_parser("harvest", help="run the corpus harvest and facet tagging")
    p.add_argument("--profile", default="core")
    p.add_argument("--limit", type=int, help="cap PMIDs fetched (smoke runs)")
    p.add_argument("--skip-facets", action="store_true")
    p.add_argument("--batch-size", type=int, default=200)
    p.set_defaults(func=cmd_harvest)

    p = sub.add_parser("seeds", help="re-check the seed references against the stored corpus")
    p.add_argument("--profile", default="core")
    p.set_defaults(func=cmd_seeds)

    p = sub.add_parser("check-mesh", help="validate declared MeSH descriptors against PubMed")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_check_mesh)

    p = sub.add_parser("ontology", help="resolve facets to OMOP concepts and relationships")
    p.add_argument("--athena-dir", help="directory holding a downloaded ATHENA vocabulary bundle")
    p.add_argument("--no-fallbacks", action="store_true", help="bundle only, no public services")
    p.add_argument("--no-athena-api", action="store_true")
    p.add_argument("--no-hierarchy", action="store_true", help="skip parent/ancestor lookups")
    p.add_argument("--max-candidates", type=int, default=3)
    p.set_defaults(func=cmd_ontology)

    p = sub.add_parser("report", help="write the validation report and derived tables")
    p.add_argument("--out", help="markdown output path")
    p.add_argument("--strict", action="store_true", help="exit non-zero if a check fails")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("graph", help="print a node's relation neighbourhood")
    p.add_argument("--node", required=True)
    p.set_defaults(func=cmd_graph)

    p = sub.add_parser("compact", help="deduplicate and sort the JSONL record layer")
    p.set_defaults(func=cmd_compact)

    p = sub.add_parser("rebuild", help="rebuild SQLite from the JSONL layer, no network")
    p.set_defaults(func=cmd_rebuild)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
