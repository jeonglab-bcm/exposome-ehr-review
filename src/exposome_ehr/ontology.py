"""OMOP concept layer for the facet vocabulary.

Resolution order, best source first:

  1. A local OMOP vocabulary bundle downloaded from ATHENA. Authoritative:
     real ``concept_id`` values, real ``CONCEPT_RELATIONSHIP`` rows including
     'Is a' and 'Part of', and real ``CONCEPT_ANCESTOR`` levels. Point at it
     with ``--athena-dir``.
  2. The ATHENA public web API. Implemented, but at the time of writing it
     answers 403 to programmatic clients; the attempt is logged either way so
     the report says which sources actually ran.
  3. Public terminology services, one per vocabulary:
       SNOMED  EBI Ontology Lookup Service (OLS4), which also supplies real
               subclass parents and ancestors, giving is_a edges.
       LOINC   NLM Clinical Table Search Service.
       RxNorm  NLM RxNav.
     These return vocabulary codes but no OMOP ``concept_id``. That is the
     honest limit of the fallback: codes are portable, concept_ids are not
     derivable without the bundle.

Nothing here writes a mapping as reviewed. Every row lands as
``mapping_status='unreviewed'``: a string match against a concept name is a
candidate for a human to accept or reject, not a phenotype definition.
"""

from __future__ import annotations

import csv
import logging
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .config import Vocabulary
from .httpclient import HttpError, RateLimitedClient
from .store import Store, utcnow

log = logging.getLogger(__name__)

ATHENA_API = "https://athena.ohdsi.org/api/v1/concepts"
OLS4_API = "https://www.ebi.ac.uk/ols4/api"
LOINC_API = "https://clinicaltables.nlm.nih.gov/api/loinc_items/v3/search"
RXNAV_API = "https://rxnav.nlm.nih.gov/REST"

OLS_ONTOLOGY = {"SNOMED": "snomed", "MeSH": "mesh", "NCIt": "ncit", "HPO": "hp"}


# British/American variants that would otherwise make a correct mapping look
# like a mismatch: SNOMED uses US spelling, the literature and this vocabulary
# often use the British form.
SPELLING_VARIANTS = {
    "fibre": "fiber",
    "fibres": "fibers",
    "manoeuvre": "maneuver",
    "manoeuvres": "maneuvers",
    "oedema": "edema",
    "haemoglobin": "hemoglobin",
    "anaemia": "anemia",
    "hypovolaemia": "hypovolemia",
    "hypovolaemic": "hypovolemic",
    "sjogrens": "sjogren",
    "tumour": "tumor",
    "paediatric": "pediatric",
    "oesophageal": "esophageal",
}

# Tokens that carry no identifying meaning in a LOINC long common name or a
# SNOMED semantic phrasing. Deliberately excludes words that carry real
# meaning even though the terminologies also use them structurally —
# "therapy", "regime", "situation" and "value" distinguish genuinely
# different concepts (e.g. "Sweat therapy measure" vs "Sweat measure"), so
# dropping them could collapse a real qualifier mismatch into a false `exact`
# and hide it from the review-priority export.
NOISE_TOKENS = frozenset(
    {
        "in", "of", "by", "the", "and", "or", "a", "an", "s",
        "disorder", "finding", "procedure", "observable", "entity", "qualifier",
        "moles", "mass", "units", "volume", "presence", "ratio",
        "ab", "immunoassay", "panel",
    }
)


# Abbreviations the terminologies use where this vocabulary spells the word out.
ABBREVIATIONS = {
    "antibody": "ab",
    "antibodies": "ab",
    "immunoglobulin": "ig",
    "electrocardiography": "ecg",
    "electrocardiographic": "ecg",
    "electrocardiogram": "ecg",
}


def _tokens(value: str | None, drop_noise: bool = False) -> list[str]:
    """Normalised word tokens.

    Normalises case, punctuation, SNOMED's parenthesised semantic tag, British
    spelling, terminology abbreviations, and simple plurals. Plural stripping is
    naive (a trailing "s" on a token longer than four characters) which is fine
    for triaging mappings for review, and is not sound enough for anything that
    depends on the tokens themselves.
    """
    if not value:
        return []
    text = value.strip()
    # Drop SNOMED's trailing parenthesised semantic tag, e.g. "(disorder)".
    if text.endswith(")") and "(" in text:
        text = text[: text.rfind("(")]
    cleaned = "".join(ch if ch.isalnum() else " " for ch in text.lower())
    out = []
    for token in cleaned.split():
        token = SPELLING_VARIANTS.get(token, token)
        token = ABBREVIATIONS.get(token, token)
        if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        if drop_noise and token in NOISE_TOKENS:
            continue
        out.append(token)
    return out


def classify_label_match(search_term: str, concept_name: str | None) -> str:
    """How closely a candidate's name matches the term it was found by.

    Token-based rather than substring-based, because both vocabularies interleave
    the words: LOINC writes "Norepinephrine [Moles/volume] in Plasma --supine"
    for what this vocabulary calls "norepinephrine plasma", and SNOMED appends a
    semantic tag. Comparing raw strings would call both of those a mismatch.

    'exact'    same set of meaningful tokens.
    'contains' every token of the search term appears in the concept name, so
               the concept is the same thing, usually more specifically named.
    'loose'    at least one search token is absent. The service matched on
               something else and a human needs to look.
    """
    needle = _tokens(search_term, drop_noise=True)
    haystack = _tokens(concept_name, drop_noise=True)
    if not needle or not haystack:
        return "unknown"
    if set(needle) == set(haystack):
        return "exact"
    if set(needle) <= set(haystack):
        return "contains"
    return "loose"


def _normalize_label(value: str | None) -> str:
    """Lowercase, drop SNOMED's parenthesised semantic tag, squeeze whitespace."""
    return " ".join(_tokens(value))


@dataclass
class Candidate:
    vocabulary_id: str
    concept_code: str
    concept_name: str | None = None
    concept_id: int | None = None
    domain_id: str | None = None
    concept_class_id: str | None = None
    standard_concept: str | None = None
    invalid_reason: str | None = None
    rank: int = 0
    iri: str | None = None


@dataclass
class Edge:
    vocabulary_id_1: str
    concept_code_1: str
    relationship_id: str
    vocabulary_id_2: str
    concept_code_2: str
    concept_name_2: str | None = None


class Resolver:
    """Base class. ``vocabularies`` is the set this resolver can answer for."""

    name = "base"
    vocabularies: frozenset[str] = frozenset()

    def available(self) -> bool:  # pragma: no cover - trivial
        return True

    def search(self, vocabulary_id: str, term: str, limit: int = 5) -> list[Candidate]:
        raise NotImplementedError

    def parents(self, candidate: Candidate) -> list[Edge]:
        return []

    def ancestors(self, candidate: Candidate) -> list[Edge]:
        return []


# --------------------------------------------------------------------- Athena
class AthenaBundleResolver(Resolver):
    """Reads a downloaded OMOP vocabulary bundle (ATHENA CSV export)."""

    name = "athena_bundle"

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._concepts: list[dict[str, str]] = []
        self._by_code: dict[tuple[str, str], dict[str, str]] = {}
        self._relationships: list[dict[str, str]] = []
        self._loaded = False

    @staticmethod
    def _find(directory: Path, stem: str) -> Path | None:
        for name in (f"{stem}.csv", f"{stem}.CSV", f"{stem}.tsv", stem):
            candidate = directory / name
            if candidate.exists():
                return candidate
        return None

    def available(self) -> bool:
        return self.directory.is_dir() and self._find(self.directory, "CONCEPT") is not None

    def _load(self) -> None:
        if self._loaded:
            return
        concept_path = self._find(self.directory, "CONCEPT")
        if concept_path is None:
            raise FileNotFoundError(f"no CONCEPT file under {self.directory}")
        with concept_path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                self._concepts.append(row)
                self._by_code[(row["vocabulary_id"], row["concept_code"])] = row
        rel_path = self._find(self.directory, "CONCEPT_RELATIONSHIP")
        if rel_path is not None:
            by_id = {r["concept_id"]: r for r in self._concepts}
            with rel_path.open(encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh, delimiter="\t"):
                    left = by_id.get(row["concept_id_1"])
                    right = by_id.get(row["concept_id_2"])
                    if left and right:
                        self._relationships.append(
                            {
                                "vocabulary_id_1": left["vocabulary_id"],
                                "concept_code_1": left["concept_code"],
                                "relationship_id": row["relationship_id"],
                                "vocabulary_id_2": right["vocabulary_id"],
                                "concept_code_2": right["concept_code"],
                                "concept_name_2": right["concept_name"],
                            }
                        )
        self._loaded = True
        log.info(
            "athena bundle: %d concepts, %d relationships from %s",
            len(self._concepts), len(self._relationships), self.directory,
        )

    def search(self, vocabulary_id: str, term: str, limit: int = 5) -> list[Candidate]:
        self._load()
        needle = term.lower()
        scored: list[tuple[int, dict[str, str]]] = []
        for row in self._concepts:
            if vocabulary_id and row.get("vocabulary_id") != vocabulary_id:
                continue
            name = (row.get("concept_name") or "").lower()
            if name == needle:
                scored.append((0, row))
            elif needle in name:
                scored.append((1, row))
        scored.sort(key=lambda pair: (pair[0], len(pair[1].get("concept_name") or "")))
        out = []
        for rank, (_score, row) in enumerate(scored[:limit], start=1):
            out.append(
                Candidate(
                    vocabulary_id=row["vocabulary_id"],
                    concept_code=row["concept_code"],
                    concept_name=row.get("concept_name"),
                    concept_id=int(row["concept_id"]) if row.get("concept_id") else None,
                    domain_id=row.get("domain_id"),
                    concept_class_id=row.get("concept_class_id"),
                    standard_concept=row.get("standard_concept") or None,
                    invalid_reason=row.get("invalid_reason") or None,
                    rank=rank,
                )
            )
        return out

    def parents(self, candidate: Candidate) -> list[Edge]:
        self._load()
        out = []
        for row in self._relationships:
            same = (
                row["vocabulary_id_1"] == candidate.vocabulary_id
                and row["concept_code_1"] == candidate.concept_code
            )
            if same and row["relationship_id"] in {"Is a", "Part of", "Subsumes"}:
                out.append(Edge(**row))
        return out


class AthenaApiResolver(Resolver):
    """The ATHENA public web API. Frequently blocked; failures are recorded."""

    name = "athena_api"

    def __init__(self, client: RateLimitedClient | None = None):
        self.client = client or RateLimitedClient(min_interval=1.0)
        self._blocked = False

    def available(self) -> bool:
        return not self._blocked

    def search(self, vocabulary_id: str, term: str, limit: int = 5) -> list[Candidate]:
        params = {"query": term, "pageSize": limit, "page": 1}
        if vocabulary_id:
            params["vocabulary"] = vocabulary_id
        try:
            payload = self.client.get_json(ATHENA_API, params=params)
        except HttpError as exc:
            if exc.status in (401, 403, 404):
                self._blocked = True
            raise
        out = []
        for rank, row in enumerate(payload.get("content", []), start=1):
            out.append(
                Candidate(
                    vocabulary_id=row.get("vocabulary") or vocabulary_id,
                    concept_code=str(row.get("code")),
                    concept_name=row.get("name"),
                    concept_id=row.get("id"),
                    domain_id=row.get("domain"),
                    concept_class_id=row.get("className"),
                    standard_concept=row.get("standardConcept"),
                    rank=rank,
                )
            )
        return out


# ----------------------------------------------------------------------- OLS4
class Ols4Resolver(Resolver):
    """EBI Ontology Lookup Service. Supplies SNOMED codes and real is_a edges."""

    name = "ols4"
    vocabularies = frozenset({"SNOMED", "MeSH", "NCIt", "HPO"})

    def __init__(self, client: RateLimitedClient | None = None):
        self.client = client or RateLimitedClient(min_interval=0.25)

    def search(self, vocabulary_id: str, term: str, limit: int = 5) -> list[Candidate]:
        ontology = OLS_ONTOLOGY.get(vocabulary_id)
        if not ontology:
            return []
        payload = self.client.get_json(
            f"{OLS4_API}/search",
            params={"q": term, "ontology": ontology, "rows": limit, "exact": "false"},
        )
        docs = payload.get("response", {}).get("docs", [])

        def sort_key(doc: dict) -> tuple[int, int]:
            label = _normalize_label(doc.get("label"))
            needle = _normalize_label(term)
            # An exact label match beats a substring match, and among equals the
            # shortest label is the least qualified concept. OLS4 orders by its
            # own relevance score, which puts "Fetal heart rate variability"
            # ahead of a plain "Heart rate variability".
            return (0 if label == needle else 1 if label.startswith(needle) else 2, len(label))

        out = []
        for rank, doc in enumerate(sorted(docs, key=sort_key), start=1):
            obo_id = doc.get("obo_id") or ""
            code = obo_id.split(":", 1)[1] if ":" in obo_id else doc.get("short_form", "")
            if not code:
                continue
            out.append(
                Candidate(
                    vocabulary_id=vocabulary_id,
                    concept_code=code,
                    concept_name=doc.get("label"),
                    concept_class_id=doc.get("type"),
                    rank=rank,
                    iri=doc.get("iri"),
                )
            )
        return out

    def _terms_endpoint(self, candidate: Candidate, relation: str) -> list[dict]:
        ontology = OLS_ONTOLOGY.get(candidate.vocabulary_id)
        if not ontology or not candidate.iri:
            return []
        # OLS4 wants the IRI double URL-encoded in the path.
        encoded = urllib.parse.quote(urllib.parse.quote(candidate.iri, safe=""), safe="")
        try:
            payload = self.client.get_json(
                f"{OLS4_API}/ontologies/{ontology}/terms/{encoded}/{relation}",
                params={"size": 50},
            )
        except HttpError as exc:
            if exc.status == 404:
                return []
            raise
        return payload.get("_embedded", {}).get("terms", [])

    def _edges(self, candidate: Candidate, relation: str) -> list[Edge]:
        out = []
        for term in self._terms_endpoint(candidate, relation):
            obo_id = term.get("obo_id") or ""
            code = obo_id.split(":", 1)[1] if ":" in obo_id else ""
            if not code:
                continue
            out.append(
                Edge(
                    vocabulary_id_1=candidate.vocabulary_id,
                    concept_code_1=candidate.concept_code,
                    relationship_id="Is a",
                    vocabulary_id_2=candidate.vocabulary_id,
                    concept_code_2=code,
                    concept_name_2=term.get("label"),
                )
            )
        return out

    def parents(self, candidate: Candidate) -> list[Edge]:
        return self._edges(candidate, "parents")

    def ancestors(self, candidate: Candidate) -> list[Edge]:
        return self._edges(candidate, "hierarchicalAncestors")


# ---------------------------------------------------------------------- LOINC
class LoincResolver(Resolver):
    """NLM Clinical Table Search Service. No key required."""

    name = "nlm_clinical_tables"
    vocabularies = frozenset({"LOINC"})

    def __init__(self, client: RateLimitedClient | None = None):
        self.client = client or RateLimitedClient(min_interval=0.25)

    def search(self, vocabulary_id: str, term: str, limit: int = 5) -> list[Candidate]:
        if vocabulary_id != "LOINC":
            return []
        payload = self.client.get_json(
            LOINC_API,
            params={
                "terms": term,
                "maxList": limit,
                "df": "LOINC_NUM,LONG_COMMON_NAME,CLASS",
            },
        )
        # Response shape: [total, [codes], extras, [[display fields], ...]]
        rows = payload[3] if len(payload) > 3 and payload[3] else []
        out = []
        for rank, row in enumerate(rows, start=1):
            out.append(
                Candidate(
                    vocabulary_id="LOINC",
                    concept_code=row[0],
                    concept_name=row[1] if len(row) > 1 else None,
                    concept_class_id=row[2] if len(row) > 2 else None,
                    domain_id="Measurement",
                    rank=rank,
                )
            )
        return out


# --------------------------------------------------------------------- RxNorm
class RxNavResolver(Resolver):
    """NLM RxNav. Returns RxNorm ingredient RXCUIs."""

    name = "rxnav"
    vocabularies = frozenset({"RxNorm"})

    def __init__(self, client: RateLimitedClient | None = None):
        self.client = client or RateLimitedClient(min_interval=0.1)

    def search(self, vocabulary_id: str, term: str, limit: int = 5) -> list[Candidate]:
        if vocabulary_id != "RxNorm":
            return []
        payload = self.client.get_json(
            f"{RXNAV_API}/rxcui.json", params={"name": term, "search": 1}
        )
        rxcuis = payload.get("idGroup", {}).get("rxnormId", [])[:limit]
        out = []
        for rank, rxcui in enumerate(rxcuis, start=1):
            name = None
            concept_class = None
            try:
                detail = self.client.get_json(f"{RXNAV_API}/rxcui/{rxcui}/properties.json")
                props = detail.get("properties", {})
                name = props.get("name")
                concept_class = props.get("tty")
            except HttpError:  # pragma: no cover - best effort enrichment
                pass
            out.append(
                Candidate(
                    vocabulary_id="RxNorm",
                    concept_code=str(rxcui),
                    concept_name=name,
                    concept_class_id=concept_class,
                    domain_id="Drug",
                    rank=rank,
                )
            )
        return out

    def parents(self, candidate: Candidate) -> list[Edge]:
        """RxNorm ingredient-of / tradename-of edges, mapped to 'Is a'-like RxNorm relations."""
        try:
            payload = self.client.get_json(
                f"{RXNAV_API}/rxcui/{candidate.concept_code}/related.json",
                params={"tty": "IN+PIN"},
            )
        except HttpError:  # pragma: no cover
            return []
        out = []
        for group in payload.get("relatedGroup", {}).get("conceptGroup", []):
            for prop in group.get("conceptProperties", []) or []:
                if str(prop.get("rxcui")) == str(candidate.concept_code):
                    continue
                out.append(
                    Edge(
                        vocabulary_id_1="RxNorm",
                        concept_code_1=candidate.concept_code,
                        relationship_id="RxNorm has ing",
                        vocabulary_id_2="RxNorm",
                        concept_code_2=str(prop.get("rxcui")),
                        concept_name_2=prop.get("name"),
                    )
                )
        return out


# ------------------------------------------------------------------ pipeline
@dataclass
class OntologyReport:
    resolvers_used: list[str] = field(default_factory=list)
    resolvers_unavailable: list[str] = field(default_factory=list)
    concepts_written: int = 0
    facet_mappings: int = 0
    relationships: int = 0
    unresolved: list[tuple[str, str, str]] = field(default_factory=list)
    by_vocabulary: dict[str, int] = field(default_factory=dict)


class OntologyBuilder:
    def __init__(
        self,
        store: Store,
        vocab: Vocabulary,
        athena_dir: Path | None = None,
        use_fallbacks: bool = True,
        try_athena_api: bool = True,
        max_candidates: int = 3,
        fetch_hierarchy: bool = True,
    ):
        self.store = store
        self.vocab = vocab
        self.max_candidates = max_candidates
        self.fetch_hierarchy = fetch_hierarchy

        self.resolvers: list[Resolver] = []
        if athena_dir:
            bundle = AthenaBundleResolver(athena_dir)
            if bundle.available():
                self.resolvers.append(bundle)
            else:
                log.warning("no usable ATHENA bundle at %s", athena_dir)
        if try_athena_api:
            self.resolvers.append(AthenaApiResolver())
        if use_fallbacks:
            self.resolvers.extend([Ols4Resolver(), LoincResolver(), RxNavResolver()])

    def _log_attempt(self, resolver: str, target: str, status: str, detail: str = "") -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO resolver_attempt"
                "(resolver, target, status, detail, attempted_at) VALUES (?,?,?,?,?)",
                (resolver, target, status, detail[:500], utcnow()),
            )

    def _resolvers_for(self, vocabulary_id: str) -> Iterable[Resolver]:
        for resolver in self.resolvers:
            if not resolver.available():
                continue
            if resolver.vocabularies and vocabulary_id not in resolver.vocabularies:
                continue
            yield resolver

    def run(self) -> OntologyReport:
        report = OntologyReport()
        self.store.upsert_vocabulary(self.vocab)

        for facet in self.vocab.facet_list:
            for spec in facet["concepts"]:
                vocabulary_id = spec["vocabulary"]
                term = spec["search_term"]
                target = f"{vocabulary_id}:{term}"
                resolved = False

                for resolver in self._resolvers_for(vocabulary_id):
                    try:
                        candidates = resolver.search(vocabulary_id, term, self.max_candidates)
                    except HttpError as exc:
                        self._log_attempt(
                            resolver.name, target, "error", f"HTTP {exc.status}: {exc}"
                        )
                        if resolver.name not in report.resolvers_unavailable:
                            report.resolvers_unavailable.append(resolver.name)
                        continue
                    except Exception as exc:  # pragma: no cover - defensive
                        self._log_attempt(resolver.name, target, "error", str(exc))
                        continue

                    if not candidates:
                        self._log_attempt(resolver.name, target, "no_match")
                        continue

                    self._log_attempt(
                        resolver.name, target, "ok", f"{len(candidates)} candidates"
                    )
                    if resolver.name not in report.resolvers_used:
                        report.resolvers_used.append(resolver.name)

                    for candidate in candidates:
                        self._write_candidate(facet["id"], term, candidate, resolver.name)
                        report.concepts_written += 1
                        report.facet_mappings += 1
                        report.by_vocabulary[vocabulary_id] = (
                            report.by_vocabulary.get(vocabulary_id, 0) + 1
                        )

                    if self.fetch_hierarchy:
                        report.relationships += self._write_hierarchy(
                            resolver, candidates[0]
                        )
                    resolved = True
                    break

                if not resolved:
                    report.unresolved.append((facet["id"], vocabulary_id, term))
                    log.warning("unresolved concept: %s for facet %s", target, facet["id"])

        self.store.rebuild_closure()
        return report

    def _write_candidate(
        self, facet_id: str, search_term: str, candidate: Candidate, resolver_name: str
    ) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO omop_concept"
                "(concept_id, concept_code, vocabulary_id, concept_name, domain_id,"
                " concept_class_id, standard_concept, invalid_reason, source, resolved_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    candidate.concept_id, candidate.concept_code, candidate.vocabulary_id,
                    candidate.concept_name, candidate.domain_id, candidate.concept_class_id,
                    candidate.standard_concept, candidate.invalid_reason, resolver_name, utcnow(),
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO facet_concept"
                "(facet_id, vocabulary_id, search_term, concept_code, concept_name, concept_id,"
                " domain_id, match_rank, label_match, resolver, mapping_status, resolved_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?, 'unreviewed', ?)",
                (
                    facet_id, candidate.vocabulary_id, search_term, candidate.concept_code,
                    candidate.concept_name, candidate.concept_id, candidate.domain_id,
                    candidate.rank, classify_label_match(search_term, candidate.concept_name),
                    resolver_name, utcnow(),
                ),
            )

    def _write_hierarchy(self, resolver: Resolver, candidate: Candidate) -> int:
        edges: list[Edge] = []
        try:
            edges.extend(resolver.parents(candidate))
            edges.extend(resolver.ancestors(candidate))
        except HttpError as exc:  # pragma: no cover - network dependent
            log.warning("hierarchy lookup failed for %s: %s", candidate.concept_code, exc)
            return 0
        if not edges:
            return 0
        seen: set[tuple] = set()
        rows = []
        for edge in edges:
            key = (
                edge.vocabulary_id_1, edge.concept_code_1, edge.relationship_id,
                edge.vocabulary_id_2, edge.concept_code_2,
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append((*key, edge.concept_name_2, resolver.name, utcnow()))
        with self.store.transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO omop_concept_relationship"
                "(vocabulary_id_1, concept_code_1, relationship_id, vocabulary_id_2,"
                " concept_code_2, concept_name_2, source, resolved_at) VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)
