"""Deterministic first-pass eligibility screening for search candidates.

**No LLM is involved here.** This is pure, offline, reproducible pattern
matching over PubMed/PMC metadata: compiled regexes against title and abstract,
set membership against publication types and indexed MeSH headings, plus the
name of the query arm that retrieved the record. The same input always yields
the same decision, and it costs nothing to re-run. LLM extraction happens
later, on full text, and answers a different question (what the study *says*),
not this one (should this record be in the review at all).

Search results are *candidates*, not publications accepted into the evidence
map. Each candidate gets one of three decisions:

- ``included``  -- affirmative evidence on every axis; safe to publish
- ``excluded``  -- positive disqualifying evidence (a review, an animal study)
- ``pending``   -- relevant but unproven; a human decides

The bias is deliberate: **absence of evidence never counts as evidence.** Any
axis that comes back undetermined blocks automatic inclusion but does not
exclude, so ambiguous records queue for review rather than silently entering
or leaving the corpus.

Four axes feed the decision, each independently recorded so the trail is
auditable:

1. *primary study* -- a study, not a review/protocol/correction/conference item
2. *human subject* -- human participants, not animal-only or in-vitro
3. *scope*         -- see below
4. *metadata complete* -- was PubMed enrichment actually retrieved

"Scope" here means **which of the review's search arms this record's subject
matter belongs to** -- a topical label, not a quality judgment:

- ``core-exposomics``          -- exposome/ExWAS/EWAS proper
- ``operational-mixtures``     -- multi-pollutant / chemical-mixture studies
- ``vaccine-exposure``         -- vaccination as the exposure
- ``adjacent-single-exposure`` -- one exposure, adjacent to the core question
- ``out-of-scope``             -- affirmatively not an exposure study
- ``unclear``                  -- no arm established; blocks inclusion

It is derived from the record text *and* the arm that retrieved it, so query
provenance informs the label rather than the text alone having to carry it.

Age and EHR use are never inclusion requirements. Cohort composition is
reported per paper as ``cohort_type``, extracted from full text during
summarization; this module does not second-guess it from an abstract.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Iterable, Literal, Mapping, Sequence, get_args
import re


Decision = Literal["included", "excluded", "pending"]
_DECISIONS: frozenset[str] = frozenset(get_args(Decision))
ScopeClassification = Literal[
    "core-exposomics",
    "operational-mixtures",
    "vaccine-exposure",
    "adjacent-single-exposure",
    "out-of-scope",
    "unclear",
]
_SCOPE_CLASSES: frozenset[str] = frozenset(get_args(ScopeClassification))


_NON_PRIMARY_TYPES = {
    "review",
    "systematic review",
    "meta-analysis",
    "editorial",
    "comment",
    "published erratum",
    "correction",
    "retracted publication",
    "retraction of publication",
    "protocol",
    "congress",
}
_PRIMARY_TYPES = {
    "journal article",
    "clinical trial",
    "randomized controlled trial",
    "observational study",
    "comparative study",
    "evaluation study",
    "multicenter study",
}

_NON_PRIMARY_TITLE = re.compile(
    r"\b(review|systematic review|scoping review|narrative review|meta[- ]analysis|"
    r"bibliometric|editorial|commentary|protocol|roadmap|correction|erratum|"
    r"retraction|conference abstract|poster)\b",
    re.IGNORECASE,
)
_PRIMARY_DESIGN = re.compile(
    r"\b(cohort|case[- ]control|cross[- ]sectional|clinical trial|randomi[sz]ed|"
    r"longitudinal|prospective|retrospective|association stud(?:y|ies)|"
    r"follow[- ]?up|participants?|patients?|population[- ]based)\b",
    re.IGNORECASE,
)
_ANIMAL_ONLY = re.compile(
    r"\b(mice|mouse|murine|rats?|zebrafish|drosophila|porcine|bovine|"
    r"non[- ]?human primates?|monkeys?|animal model|in vitro|cell lines?)\b",
    re.IGNORECASE,
)
_NONHUMAN_LAB = re.compile(
    r"\b(in[ -]?vitro|cell lines?|cultured cells?|organoids?|tissue explants?|"
    r"primary cells?|immortali[sz]ed cells?)\b",
    re.IGNORECASE,
)
_HUMAN_PARTICIPANT = re.compile(
    r"\b(people|persons?|participants?|patients?|subjects?|women|men|mothers?|"
    r"pregnant (?:people|persons?|women)|birth cohort|infants?|newborns?|"
    r"neonates?|children|child|childhood|offspring|adolescents?|youth|"
    r"adults?|elderly|workers?)\b|\bhuman (?:participants?|subjects?|patients?|"
    r"populations?|cohorts?)\b",
    re.IGNORECASE,
)
_HUMAN = re.compile(
    r"\b(humans?|people|persons?|participants?|patients?|women|men|mothers?|"
    r"pregnan(?:t|cy)|prenatal|birth cohort|infants?|newborns?|neonates?|"
    r"children|child|childhood|early[- ]life|offspring|adolescents?|youth|"
    r"adults?|elderly|workers?)\b",
    re.IGNORECASE,
)
_EHR = re.compile(
    r"\b(electronic (?:health|medical) records?|EHR|EMR|claims data|"
    r"administrative health data|hospital discharge|medical records?|"
    r"health registr(?:y|ies)|record linkage|linked health)\b",
    re.IGNORECASE,
)
# ── search-arm vocabularies ──────────────────────────────────────────────────
# One regex per scope value. "Core" is the review's high-precision arm, named
# after the search strategy in #46: the field's own vocabulary (exposom*, ExWAS,
# environment/exposure-wide association). A paper using these terms is exposome
# research *proper*, as opposed to `operational-mixtures` (multi-pollutant work
# that is exposome-shaped without using the word) or `adjacent-single-exposure`
# (one exposure, adjacent to the core question).
_CORE_EXPOSOMICS = re.compile(
    r"\b(exposom\w*|ExWAS|P[- ]?ExWAS|environment[- ]wide association|"
    r"exposure[- ]wide association)\b",
    re.IGNORECASE,
)
_OPERATIONAL_MIXTURES = re.compile(
    r"\b(chemical mixtures?|exposure mixtures?|multiple exposures?|"
    r"combined exposures?|cumulative exposures?|multi[- ]pollutant|"
    r"environmental risk score|internal exposome|external exposome)\b",
    re.IGNORECASE,
)
_VACCINE_TOPIC = re.compile(
    r"\b(vaccines?|vaccination|vaccinated|immuni[sz]ation)\b",
    re.IGNORECASE,
)
_GENETICS = re.compile(
    r"\b(genome[- ]wide association|GWAS|genetics?|genomics?|genotypes?|"
    r"genetic variants?|polygenic|Mendelian randomi[sz]ation|SNPs?)\b",
    re.IGNORECASE,
)
_EXPOSURE_ANALYSIS = re.compile(
    r"\b(environmental exposures?|chemical exposures?|exposure[- ]wide|"
    r"environment[- ]wide|exposome[- ]wide|mixtures?|multi[- ]pollutant|"
    r"air pollution|particulate matter|vaccin(?:e|ation)|internal exposome|"
    r"external exposome|gene[- ]environment)\b",
    re.IGNORECASE,
)


def _strings(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(item) for item in value]
    return [str(value)]


def _normalise_types(item: Mapping[str, object]) -> set[str]:
    raw = item.get("pubtype", item.get("publication_types", []))
    return {value.strip().lower() for value in _strings(raw) if value.strip()}


def _mesh_terms(item: Mapping[str, object]) -> set[str]:
    raw: list[str] = []
    for key in ("mesh", "mesh_terms", "meshheadings"):
        raw.extend(_strings(item.get(key)))
    return {value.strip().lower() for value in raw if value.strip()}


def _text(item: Mapping[str, object]) -> str:
    return ". ".join(
        value for key in ("title", "abstract", "sorttitle")
        if (value := str(item.get(key, "") or "").strip())
    )


def _arm_tokens(query_arms: Iterable[str]) -> set[str]:
    return {
        token
        for arm in query_arms
        for token in re.split(r"[^a-z0-9]+", str(arm).lower())
        if token
    }


@dataclass(frozen=True)
class ScreeningDecision:
    """Persistable candidate-level screening decision."""

    pmcid: str
    title: str
    decision: Decision
    scope_classification: ScopeClassification
    human_study: bool | None
    primary_study: bool | None
    ehr_facet: bool | None
    query_provenance: tuple[str, ...] = ()
    eligibility_evidence: tuple[str, ...] = ()
    exclusion_reasons: tuple[str, ...] = ()
    screening_method: str = "automated-metadata-v1"
    reviewer: str = ""
    reviewed_at: str = ""
    metadata_complete: bool | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        for name in (
            "query_provenance",
            "eligibility_evidence",
            "exclusion_reasons",
        ):
            data[name] = list(data[name])
        return data


def classify_scope(text: str, query_arms: Iterable[str] = ()) -> ScopeClassification:
    """Classify scope using record text plus the arms that retrieved it."""
    tokens = _arm_tokens(query_arms)
    if _CORE_EXPOSOMICS.search(text) or "core" in tokens:
        return "core-exposomics"
    if _VACCINE_TOPIC.search(text) or "vaccine" in tokens:
        return "vaccine-exposure"
    if _OPERATIONAL_MIXTURES.search(text) or {"mixture", "mixtures", "operational"} & tokens:
        return "operational-mixtures"
    if {"single", "adjacent", "ehr"} & tokens:
        return "adjacent-single-exposure"
    return "unclear"


def validate_manual_override(override: Mapping[str, object]) -> dict[str, object]:
    """Validate the durable human-review contract and return a plain copy."""
    data = dict(override)
    decision = str(data.get("decision", "")).strip().lower()
    if decision not in {"included", "excluded", "pending"}:
        raise ValueError("manual override requires decision included/excluded/pending")
    reviewer = str(data.get("reviewer", "") or "").strip()
    if not reviewer:
        raise ValueError("manual override requires reviewer")
    reviewed_at = str(data.get("reviewed_at", "") or "").strip()
    if not reviewed_at:
        raise ValueError("manual override requires reviewed_at")
    try:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("manual override reviewed_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("manual override reviewed_at must include a timezone")

    eligibility = _strings(data.get("eligibility_evidence"))
    exclusions = _strings(data.get("exclusion_reasons"))
    if decision == "included" and not any(value.strip() for value in eligibility):
        raise ValueError("included manual override requires eligibility_evidence")
    if decision == "excluded" and not any(value.strip() for value in exclusions):
        raise ValueError("excluded manual override requires exclusion_reasons")
    if decision == "pending" and not any(
        value.strip() for value in [*eligibility, *exclusions]
    ):
        raise ValueError("pending manual override requires review evidence")
    data.update({
        "decision": decision,
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "eligibility_evidence": eligibility,
        "exclusion_reasons": exclusions,
    })
    return data


def load_screening_overrides(path: str | Path) -> dict[str, dict[str, object]]:
    """Load durable PMCID-keyed decisions without mutating the review file."""
    source = Path(path)
    if not source.exists():
        return {}
    try:
        raw = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable screening override file {source}: {exc}") from exc
    if isinstance(raw, Mapping) and isinstance(raw.get("records"), Mapping):
        raw = raw["records"]
    if not isinstance(raw, Mapping):
        raise ValueError("screening override file must be a PMCID-keyed object")
    out: dict[str, dict[str, object]] = {}
    for raw_pmcid, value in raw.items():
        pmcid = str(raw_pmcid).strip().upper()
        if not pmcid.startswith("PMC"):
            pmcid = f"PMC{pmcid}"
        if not pmcid[3:].isdigit():
            raise ValueError(f"invalid override PMCID: {raw_pmcid!r}")
        if pmcid in out:
            raise ValueError(f"duplicate override PMCID after normalization: {pmcid}")
        if not isinstance(value, Mapping):
            raise ValueError(f"manual override for {pmcid} must be an object")
        out[pmcid] = validate_manual_override(value)
    return out


def _classify_primary_study(
    types: set[str], title: str, text: str
) -> tuple[bool | None, list[str], list[str]]:
    """Is this a primary study? Returns (verdict, evidence, exclusion reasons).

    ``None`` means undetermined, which keeps the record out of automatic
    inclusion without excluding it.
    """
    evidence: list[str] = []
    reasons: list[str] = []

    non_primary_type = sorted(types & _NON_PRIMARY_TYPES)
    if non_primary_type:
        reasons.append("non-primary publication type: " + ", ".join(non_primary_type))
        return False, evidence, reasons
    if _NON_PRIMARY_TITLE.search(title):
        reasons.append("title identifies a review/protocol/correction or conference item")
        return False, evidence, reasons
    if types & _PRIMARY_TYPES:
        evidence.append(
            "primary-study-compatible publication type: "
            + ", ".join(sorted(types & _PRIMARY_TYPES))
        )
        return True, evidence, reasons
    if _PRIMARY_DESIGN.search(text):
        evidence.append("primary study design/population language in title or abstract")
        return True, evidence, reasons
    return None, evidence, reasons


def _classify_human_subject(
    title: str, text: str, mesh: set[str]
) -> tuple[bool | None, list[str], list[str]]:
    """Is this a human study? Returns (verdict, evidence, exclusion reasons).

    Indexed MeSH headings outrank title/abstract wording, because ``Humans`` /
    ``Animals`` are curated by NLM while the text is not. ``None`` means
    undetermined.
    """
    evidence: list[str] = []
    reasons: list[str] = []

    if _NONHUMAN_LAB.search(title) and not _HUMAN_PARTICIPANT.search(title):
        reasons.append("title identifies an in-vitro, cell-line, or organoid-only study")
        return False, evidence, reasons
    if {"humans", "human"} & mesh:
        evidence.append("PubMed/PMC subject metadata includes Humans")
        return True, evidence, reasons
    if "animals" in mesh:
        reasons.append("subject metadata identifies an animal-only study")
        return False, evidence, reasons
    if _ANIMAL_ONLY.search(title) and not _HUMAN_PARTICIPANT.search(title):
        reasons.append("title identifies an animal-only or in-vitro study")
        return False, evidence, reasons
    if _HUMAN.search(text):
        evidence.append("human population language in title or abstract")
        return True, evidence, reasons
    return None, evidence, reasons


def _apply_override(
    override: Mapping[str, object],
    *,
    decision: Decision,
    scope: ScopeClassification,
    human: bool | None,
    primary: bool | None,
    provenance: tuple[str, ...],
    evidence: list[str],
    reasons: list[str],
) -> tuple[Decision, ScopeClassification, bool | None, bool | None, str, str, str]:
    """Fold a reviewer's manual decision over the automated one.

    An override may *downgrade* freely, but a manual `included` still has to
    meet the same evidence bar as an automatic one — reviewer, timestamp,
    evidence, human, primary study, a real scope class, query provenance —
    otherwise it lands back on `pending`. `evidence` and `reasons` are appended
    to in place, so the automated trail is never erased.
    """
    requested = str(override.get("decision", "")).strip().lower()
    override_scope = str(override.get("scope_classification", "")).strip()
    if override_scope in _SCOPE_CLASSES:
        scope = override_scope  # type: ignore[assignment]
    evidence.extend(_strings(override.get("eligibility_evidence")))
    reasons.extend(_strings(override.get("exclusion_reasons")))
    reviewer = str(override.get("reviewer", "") or "")
    reviewed_at = str(override.get("reviewed_at", "") or "")
    if isinstance(override.get("human_study"), bool):
        human = bool(override["human_study"])
    if isinstance(override.get("primary_study"), bool):
        primary = bool(override["primary_study"])

    method = "automated-metadata-v1"
    if requested in _DECISIONS:
        decision = requested  # type: ignore[assignment]
        method = "manual-override"
    if requested == "included" and not (
        reviewer and reviewed_at and evidence and human is True
        and primary is True and scope not in {"unclear", "out-of-scope"}
        and bool(provenance)
    ):
        decision = "pending"
        reasons.append(
            "manual inclusion requires reviewer, review timestamp, eligibility "
            "evidence, human_study=true, primary_study=true, a scope class, and "
            "named query provenance"
        )
    return decision, scope, human, primary, reviewer, reviewed_at, method


def screen_candidate(
    item: Mapping[str, object],
    *,
    query_names: Sequence[str] = (),
    query_arms: Sequence[str] = (),
    override: Mapping[str, object] | None = None,
) -> ScreeningDecision:
    """Return a fail-closed eligibility decision for one candidate.

    Automatic inclusion requires affirmative human and primary-study evidence.
    Relevant but ambiguous records stay pending for manual review.  A manual
    override may set ``decision`` and supporting reviewer/evidence fields; it
    never erases the automated evidence trail.
    """
    uid = str(item.get("pmcid", item.get("uid", ""))).strip().upper()
    if uid and not uid.startswith("PMC"):
        uid = f"PMC{uid}"
    title = str(item.get("title", "") or "").strip()
    text = _text(item)
    types = _normalise_types(item)
    mesh = _mesh_terms(item)
    evidence: list[str] = []
    reasons: list[str] = []
    provenance = tuple(sorted(set(query_names)))
    if provenance:
        evidence.append("retrieved by named query arm(s): " + ", ".join(provenance))
    else:
        evidence.append("manual review required; named query provenance is unavailable")

    primary, primary_evidence, primary_reasons = _classify_primary_study(
        types, title, text
    )
    evidence.extend(primary_evidence)
    reasons.extend(primary_reasons)

    human, human_evidence, human_reasons = _classify_human_subject(title, text, mesh)
    evidence.extend(human_evidence)
    reasons.extend(human_reasons)

    scope = classify_scope(text, query_arms)
    metadata_complete_raw = item.get("metadata_complete")
    metadata_complete = (
        metadata_complete_raw if isinstance(metadata_complete_raw, bool) else None
    )
    if metadata_complete is True:
        evidence.append("PubMed screening metadata enrichment completed")
    elif metadata_complete is False:
        missing = ", ".join(_strings(item.get("metadata_missing"))) or "PMID metadata"
        evidence.append(f"manual review required; enriched metadata unavailable: {missing}")
    ehr: bool | None = True if _EHR.search(text) else None
    if ehr:
        evidence.append("EHR/administrative-record language in title or abstract")

    # Vaccine records need a dedicated exposure-outcome gate: vaccination is
    # only an exposure when a later health or biological outcome is measured,
    # and uptake / coverage / hesitancy papers must not be swept in with it.
    # That gate is not part of this module yet, so vaccine-scoped records fail
    # closed to `pending` and reach a human instead of being auto-included.
    if scope == "vaccine-exposure":
        evidence.append(
            "manual review required; vaccine exposure-outcome eligibility is not "
            "automated"
        )
    if _GENETICS.search(title) and not _EXPOSURE_ANALYSIS.search(text):
        scope = "out-of-scope"
        reasons.append("genetics-only record has no environmental exposure analysis")
    if scope == "unclear":
        reasons.append("exposome relevance is not established by text or query arm")

    if reasons:
        decision: Decision = "excluded"
    elif (
        human is True
        and primary is True
        and scope not in {"unclear", "out-of-scope", "vaccine-exposure"}
        and metadata_complete is not False
        and bool(provenance)
    ):
        decision = "included"
    else:
        decision = "pending"

    reviewer = reviewed_at = ""
    method = "automated-metadata-v1"
    if override:
        decision, scope, human, primary, reviewer, reviewed_at, method = _apply_override(
            override,
            decision=decision, scope=scope, human=human, primary=primary,
            provenance=provenance, evidence=evidence, reasons=reasons,
        )

    return ScreeningDecision(
        pmcid=uid,
        title=title,
        decision=decision,
        scope_classification=scope,
        human_study=human,
        primary_study=primary,
        ehr_facet=ehr,
        query_provenance=provenance,
        eligibility_evidence=tuple(dict.fromkeys(evidence)),
        exclusion_reasons=tuple(dict.fromkeys(reasons)),
        screening_method=method,
        reviewer=reviewer,
        reviewed_at=reviewed_at,
        metadata_complete=metadata_complete,
    )


def partition_screening(
    records: Iterable[ScreeningDecision],
) -> dict[Decision, list[dict]]:
    """Return explicitly separate included, excluded, and pending collections."""
    out: dict[Decision, list[dict]] = {
        "included": [],
        "excluded": [],
        "pending": [],
    }
    for record in records:
        out[record.decision].append(record.to_dict())
    for values in out.values():
        values.sort(key=lambda value: value["pmcid"])
    return out
