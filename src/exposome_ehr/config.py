"""Load the curated vocabulary files under ``config/``."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


# Vocabularies a facet concept may name. This is the single source of truth:
# validate() enforces it and the test suite imports it, so the runtime gate that
# `harvest` depends on cannot drift away from what the tests assert.
CONCEPT_VOCABULARIES = frozenset({"SNOMED", "LOINC", "RxNorm", "MeSH", "NCIt", "HPO"})


def repo_root() -> Path:
    """Return the project root, overridable with EXPOSOME_EHR_ROOT."""
    override = os.environ.get("EXPOSOME_EHR_ROOT")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    return repo_root() / "config"


def _load(name: str, config_path: Path | None = None) -> dict[str, Any]:
    base = config_path or config_dir()
    with (base / name).open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@dataclass(frozen=True)
class Vocabulary:
    """The curated facet vocabulary, query profiles and relation graph."""

    query: dict[str, Any]
    facets: dict[str, Any]
    relations: dict[str, Any]

    # -- query profiles ---------------------------------------------------
    def profile(self, name: str) -> dict[str, Any]:
        profiles = self.query["profiles"]
        if name not in profiles:
            raise KeyError(f"unknown query profile {name!r}; have {sorted(profiles)}")
        return profiles[name]

    def profile_blocks(self, name: str) -> list[dict[str, str]]:
        """Return a profile's blocks, following ``inherit`` one level at a time."""
        seen: set[str] = set()
        blocks: list[dict[str, str]] = []
        current: str | None = name
        chain: list[str] = []
        while current:
            if current in seen:
                raise ValueError(f"circular inherit in query profile {name!r}")
            seen.add(current)
            chain.append(current)
            current = self.profile(current).get("inherit")
        # Walk base-first so inherited blocks keep their original order.
        for prof in reversed(chain):
            for block in self.profile(prof)["blocks"]:
                blocks.append(dict(block))
        return blocks

    def corpus_query(self, name: str = "core") -> str:
        parts = [f"({b['query']})" for b in self.profile_blocks(name)]
        return " OR ".join(parts)

    # -- facets -----------------------------------------------------------
    @property
    def facet_list(self) -> list[dict[str, Any]]:
        return self.facets["facets"]

    def facet(self, facet_id: str) -> dict[str, Any]:
        for f in self.facet_list:
            if f["id"] == facet_id:
                return f
        raise KeyError(f"unknown facet {facet_id!r}")

    @property
    def facet_ids(self) -> list[str]:
        return [f["id"] for f in self.facet_list]

    @property
    def groups(self) -> dict[str, Any]:
        return self.facets["groups"]

    @property
    def seed_references(self) -> dict[str, Any]:
        return self.facets["seed_references"]

    def facet_query(self, facet_id: str, profile: str = "core") -> str:
        """The tagging query: corpus AND facet terms."""
        facet = self.facet(facet_id)
        return f"({self.corpus_query(profile)}) AND ({facet['pubmed_query']})"

    # -- relations --------------------------------------------------------
    @property
    def predicates(self) -> dict[str, Any]:
        return self.relations["predicates"]

    @property
    def classes(self) -> list[dict[str, Any]]:
        return self.relations["classes"]

    @property
    def edges(self) -> list[tuple[str, str, str, str]]:
        """Well-formed edges only.

        Malformed rows are skipped rather than unpacked, and reported by
        validate(). Raising here would take down the one function whose job is
        to survive a hand-edited file and describe what is wrong with it.
        """
        out = []
        for row in self.relations["relations"]:
            if not isinstance(row, (list, tuple)) or len(row) != 4:
                continue
            subject, predicate, obj, provenance = row
            out.append((subject, predicate, obj, provenance))
        return out

    @property
    def malformed_edges(self) -> list[tuple[int, Any]]:
        """(index, row) for every relation row that is not a 4-element sequence.

        Two realistic causes, both from hand-editing relations.yaml: a forgotten
        provenance field, and an unquoted comma inside the provenance. The
        latter is not hypothetical -- 'hu2023: EHR, linked to census data'
        parses as four elements only because it is quoted; unquoted, YAML splits
        the flow sequence on the inner comma and yields five.
        """
        rows = []
        for index, row in enumerate(self.relations["relations"]):
            if not isinstance(row, (list, tuple)) or len(row) != 4:
                rows.append((index, row))
        return rows

    @property
    def nodes(self) -> dict[str, dict[str, Any]]:
        """Every graph node: facets plus abstract classes, keyed by id."""
        nodes: dict[str, dict[str, Any]] = {}
        for cls in self.classes:
            nodes[cls["id"]] = {
                "id": cls["id"],
                "label": cls["label"],
                "kind": cls.get("kind", "class"),
                "group": None,
                "definition": None,
            }
        for facet in self.facet_list:
            nodes[facet["id"]] = {
                "id": facet["id"],
                "label": facet["label"],
                "kind": "facet",
                "group": facet["group"],
                "definition": " ".join(facet["definition"].split()),
            }
        return nodes

    def validate(self) -> list[str]:
        """Return a list of vocabulary integrity problems; empty means clean."""
        problems: list[str] = []
        ids = self.facet_ids
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            problems.append(f"duplicate facet ids: {sorted(dupes)}")

        required = (
            "id",
            "group",
            "label",
            "definition",
            "pubmed_query",
            "mesh_terms",
            "concepts",
            "source_refs",
        )
        for facet in self.facet_list:
            for key in required:
                if not facet.get(key):
                    problems.append(f"facet {facet.get('id')!r} missing {key}")
            if facet.get("group") not in self.groups:
                problems.append(
                    f"facet {facet.get('id')!r} has unknown group {facet.get('group')!r}"
                )
            for ref in facet.get("source_refs", []):
                if ref not in self.seed_references:
                    problems.append(
                        f"facet {facet['id']!r} cites unknown reference {ref!r}"
                    )
            for concept in facet.get("concepts", []):
                if "vocabulary" not in concept or "search_term" not in concept:
                    problems.append(
                        f"facet {facet['id']!r} has malformed concept {concept!r}"
                    )
                unknown = set(concept) - {"vocabulary", "search_term", "fallback_gap"}
                if unknown:
                    problems.append(
                        f"facet {facet['id']!r} concept has unknown keys {sorted(unknown)}"
                    )
                vocabulary = concept.get("vocabulary")
                if vocabulary is not None and vocabulary not in CONCEPT_VOCABULARIES:
                    problems.append(
                        f"facet {facet['id']!r} names unknown vocabulary {vocabulary!r}; "
                        f"expected one of {sorted(CONCEPT_VOCABULARIES)}"
                    )
                for banned in ("concept_id", "concept_code"):
                    if banned in concept:
                        problems.append(
                            f"facet {facet['id']!r} hand-asserts {banned}; codes must "
                            "come from the resolver with provenance"
                        )

        for index, row in self.malformed_edges:
            size = len(row) if isinstance(row, (list, tuple)) else 1
            problems.append(
                f"relations[{index}] has {size} elements, expected 4 "
                f"[subject, predicate, object, provenance]: {row!r}"
            )

        nodes = self.nodes
        for subject, predicate, obj, _prov in self.edges:
            if predicate not in self.predicates:
                problems.append(f"unknown predicate {predicate!r}")
            if subject not in nodes:
                problems.append(f"edge subject {subject!r} is not a node")
            if obj not in nodes:
                problems.append(f"edge object {obj!r} is not a node")

        for profile in self.query["profiles"]:
            try:
                self.profile_blocks(profile)
            except ValueError as exc:
                problems.append(str(exc))

        cycles = _find_cycles(
            [
                (s, o)
                for s, p, o, _ in self.edges
                if self.predicates.get(p, {}).get("transitive")
            ]
        )
        for cycle in cycles:
            problems.append(f"cycle in transitive relations: {' -> '.join(cycle)}")

        return problems


def _find_cycles(edges: list[tuple[str, str]]) -> list[list[str]]:
    """Depth-first cycle detection over subject -> object edges."""
    adjacency: dict[str, list[str]] = {}
    for subject, obj in edges:
        adjacency.setdefault(subject, []).append(obj)

    cycles: list[list[str]] = []
    state: dict[str, int] = {}

    def visit(node: str, path: list[str]) -> None:
        state[node] = 1
        for nxt in adjacency.get(node, []):
            if state.get(nxt) == 1:
                cycles.append(path[path.index(nxt) :] + [nxt] if nxt in path else [nxt, nxt])
            elif state.get(nxt, 0) == 0:
                visit(nxt, path + [nxt])
        state[node] = 2

    for node in list(adjacency):
        if state.get(node, 0) == 0:
            visit(node, [node])
    return cycles


def load_vocabulary(config_path: Path | None = None) -> Vocabulary:
    return Vocabulary(
        query=_load("query.yaml", config_path),
        facets=_load("facets.yaml", config_path),
        relations=_load("relations.yaml", config_path),
    )
