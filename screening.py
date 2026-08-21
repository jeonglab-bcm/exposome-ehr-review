"""Deterministic first-pass eligibility screening for search candidates.

Search results are *candidates*, not publications accepted into the evidence
map.  This module records an auditable first-pass decision from PubMed/PMC
metadata and query provenance.  Ambiguous records remain ``pending`` for human
review instead of being silently published as included studies.

The review is intentionally all-age.  Population and EHR use are facets, not
hard inclusion requirements.  Vaccine exposure--outcome studies are a valid
operational exposome class; uptake/coverage/hesitancy papers without an
exposure--outcome design are excluded.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Iterable, Literal, Mapping, Sequence
import re


Decision = Literal["included", "excluded", "pending"]
ScopeClassification = Literal[
    "core-exposomics",
    "operational-mixtures",
    "vaccine-exposure",
    "adjacent-single-exposure",
    "out-of-scope",
    "unclear",
]
PopulationFacet = Literal["adult", "pediatric", "mixed", "unclear"]


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
_PEDIATRIC = re.compile(
    r"\b(pediatric|paediatric|infants?|newborns?|neonates?|children|child|"
    r"childhood|adolescents?|adolescence|youth|prenatal|early[- ]life|"
    r"birth cohort|offspring)\b",
    re.IGNORECASE,
)
_ADULT = re.compile(
    r"\b(adults?|elderly|older (?:people|adults?)|men|women|workers?|"
    r"occupational|midlife|middle[- ]aged|postmenopausal)\b",
    re.IGNORECASE,
)
_EHR = re.compile(
    r"\b(electronic (?:health|medical) records?|EHR|EMR|claims data|"
    r"administrative health data|hospital discharge|medical records?|"
    r"health registr(?:y|ies)|record linkage|linked health)\b",
    re.IGNORECASE,
)
_CORE = re.compile(
    r"\b(exposom\w*|ExWAS|P[- ]?ExWAS|environment[- ]wide association|"
    r"exposure[- ]wide association)\b",
    re.IGNORECASE,
)
_MIXTURES = re.compile(
    r"\b(chemical mixtures?|exposure mixtures?|multiple exposures?|"
    r"combined exposures?|cumulative exposures?|multi[- ]pollutant|"
    r"environmental risk score|internal exposome|external exposome)\b",
    re.IGNORECASE,
)
_VACCINE = re.compile(
    r"\b(vaccines?|vaccination|vaccinated|immuni[sz]ation)\b",
    re.IGNORECASE,
)
_VACCINE_ATTITUDE_TERM = (
    r"hesitan(?:cy|t)|acceptance|attitudes?|perceptions?|knowledge|intention|"
    r"willingness|concerns?|beliefs?|confidence|trust|refusal"
)
_VACCINE_TOPIC_SUFFIX = (
    rf"(?:{_VACCINE_ATTITUDE_TERM}|awareness|screening|education|training|"
    r"communication|surveillance|prevention|control|participation|attendance|"
    r"reporting|systems?)"
)
_VACCINE_UPTAKE = re.compile(
    rf"\b(?:uptake|coverage|{_VACCINE_ATTITUDE_TERM})\b",
    re.IGNORECASE,
)
_VACCINE_HEALTH_OUTCOME = re.compile(
    r"\b(adverse[- ]events?|safety(?: outcomes?)?|reactogenicity|immunogenicity|"
    r"immune responses?|antibod(?:y|ies)|hospitali[sz]ation|biomarkers?|"
    r"clinical outcomes?|health outcomes?|mortality|morbidity|seizures?|fever|"
    r"effectiveness|myocarditis|pericarditis|anaphylaxis|thrombosis|"
    r"thrombocytopenia)\b",
    re.IGNORECASE,
)

# A health noun anywhere in an abstract is not enough to establish that
# vaccination preceded that outcome.  In particular, survey titles often say
# things like "hesitancy during infection" or "disease knowledge".  These
# clause-local patterns require an exposure form and a directional/paired link,
# or a conventional endpoint phrase whose exposure--outcome meaning is
# intrinsic (for example, "vaccine immunogenicity").
_VACCINE_EXPOSURE_FORM = (
    r"(?:(?:MMR|DTaP|BCG)(?:\s+vaccines?)?|vaccinated|"
    r"vaccination(?!\s+(?:hesitan(?:cy|t)|acceptance|attitudes?|perceptions?|"
    r"knowledge|intention|willingness))|"
    r"immuni[sz]ation(?!\s+(?:hesitan(?:cy|t)|acceptance|attitudes?|"
    r"perceptions?|knowledge|intention|willingness))|"
    r"(?:receipt|receiving|received|administration)\s+of\s+"
    r"(?:an?\s+|the\s+)?(?:[A-Za-z0-9-]+\s+){0,3}vaccines?|"
    r"(?:vaccines?|vaccination|immuni[sz]ation)\s+"
    r"(?:status|exposure|dose|doses|schedule|uptake|coverage))"
)
_VACCINE_MEASURED_EXPOSURE_FORM = (
    r"(?:(?:receipt|receiving|received|administration)\s+of\s+"
    r"(?:an?\s+|the\s+)?(?:[A-Za-z0-9-]+\s+){0,3}vaccines?|"
    r"(?:vaccines?|vaccination|immuni[sz]ation)\s+"
    r"(?:status|exposure|dose|doses|schedule|uptake|coverage))"
)
_VACCINE_ENDPOINT = (
    r"(?:(?:adverse[- ]events?|safety(?: outcomes?)?|reactogenicity|immunogenicity|"
    r"immune responses?|antibody(?: responses?)?|antibodies|"
    r"hospitali[sz]ation|biomarkers?|"
    r"clinical outcomes?|health outcomes?|mortality|morbidity|seizures?|fever|"
    r"effectiveness|myocarditis|pericarditis|anaphylaxis|thrombosis|"
    r"thrombocytopenia|neurodevelopmental outcomes?|neurological outcomes?)"
    rf"(?![-\s]+{_VACCINE_TOPIC_SUFFIX}\b))"
)
_VACCINE_GENERIC_CONDITION = (
    r"(?:infections?|infectious diseases?|diseases?|asthma|diabetes|cancers?|"
    r"myocarditis|pericarditis|anaphylaxis|thrombosis|thrombocytopenia|"
    r"strokes?|autism spectrum disorder|Bell'?s? palsy|autoimmune diseases?|"
    r"sudden infant death syndrome|SIDS|body mass index|BMI|"
    r"neurodevelopmental outcomes?|"
    r"[A-Za-z][A-Za-z0-9-]*(?:itis|osis|emia|pathy|syndrome))"
    rf"(?![-\s]+{_VACCINE_TOPIC_SUFFIX}\b)"
)
_VACCINE_FORWARD_RELATION = re.compile(
    rf"\b{_VACCINE_EXPOSURE_FORM}\b[^.;:\n]{{0,90}}?"
    rf"(?:\band(?:\s+subsequent(?:ly)?)?\b|\bsubsequent(?:ly)?\b|"
    rf"\bfollowed by\b|\bafter\b|\bfollowing\b|\bpost[- ]|"
    rf"\bassociated(?: with)?\b|\beffects? on\b|\brisk of\b|"
    rf"\bversus\b|\bvs\.?\b|\bcompared with\b)"
    rf"[^.;:\n]{{0,60}}?\b{_VACCINE_ENDPOINT}\b",
    re.IGNORECASE,
)
_VACCINE_DIRECTIONAL_LINK = (
    r"(?:subsequent(?:ly)?|followed by|after|following|post[- ]|"
    r"associated(?: with)?|effects? on|risk of|versus|vs\.?|compared with)"
)
_NO_VACCINE_ATTITUDE = (
    rf"(?:(?!\b(?:{_VACCINE_ATTITUDE_TERM})\b)[^.;:\n])"
)
_VACCINE_BEHAVIOR_FORWARD_RELATION = re.compile(
    # A bare "and" must pair the measured exposure tightly with its endpoint;
    # otherwise survey language later in the clause can masquerade as an
    # outcome ("uptake and parental attitudes about respiratory disease").
    rf"(?:\b{_VACCINE_MEASURED_EXPOSURE_FORM}\b\s+and"
    rf"(?:\s+subsequent(?:ly)?)?\s+(?:"
    rf"\b{_VACCINE_ENDPOINT}\b|"
    rf"\b(?:risk|incidence|rates?|odds)\s+of\s+{_VACCINE_GENERIC_CONDITION}\b))|"
    # Directional relations may be farther apart, but cannot cross an
    # attitude/knowledge marker in the same clause.
    rf"(?:\b{_VACCINE_MEASURED_EXPOSURE_FORM}\b{_NO_VACCINE_ATTITUDE}{{0,35}}?"
    rf"\b{_VACCINE_DIRECTIONAL_LINK}\b{_NO_VACCINE_ATTITUDE}{{0,50}}?"
    rf"\b{_VACCINE_ENDPOINT}\b)|"
    rf"(?:\b{_VACCINE_EXPOSURE_FORM}\b{_NO_VACCINE_ATTITUDE}{{0,35}}?"
    rf"\b{_VACCINE_DIRECTIONAL_LINK}\b{_NO_VACCINE_ATTITUDE}{{0,50}}?"
    rf"\b{_VACCINE_ENDPOINT}\b)",
    re.IGNORECASE,
)
_VACCINE_REVERSE_RELATION = re.compile(
    rf"\b{_VACCINE_ENDPOINT}\b[^.;:\n]{{0,90}}?"
    rf"(?:\bafter\b|\bfollowing\b|\bassociated with\b|\bamong\b)"
    rf"[^.;:\n]{{0,60}}?\b{_VACCINE_EXPOSURE_FORM}\b",
    re.IGNORECASE,
)
_VACCINE_INTRINSIC_OUTCOME = re.compile(
    rf"\b(?:vaccines?|vaccination|immuni[sz]ation)\s+{_VACCINE_ENDPOINT}\b|"
    rf"\b{_VACCINE_ENDPOINT}\s+(?:(?:of|to)\s+)?(?:[A-Za-z0-9-]+\s+){{0,3}}"
    rf"(?:vaccines?|vaccination|immuni[sz]ation)\b|"
    rf"\b(?:vaccines?|vaccination|immuni[sz]ation)\b[^.;\n]{{0,90}}?"
    rf"(?:\s*[:—–-]\s*)\b{_VACCINE_ENDPOINT}\b|"
    rf"\b(?:vaccines?|vaccination|immuni[sz]ation)[ -]associated\s+"
    rf"(?:[A-Za-z0-9-]+\s+){{0,3}}{_VACCINE_GENERIC_CONDITION}\b|"
    rf"\bpost[- ](?:vaccination|immuni[sz]ation)\s+"
    rf"(?:[A-Za-z0-9-]+\s+){{0,3}}{_VACCINE_GENERIC_CONDITION}\b|"
    r"\badverse events? following immuni[sz]ation\b",
    re.IGNORECASE,
)
_VACCINE_GENERIC_RELATION = re.compile(
    rf"(?:\b(?:risk|incidence|odds|rates?|hazard)\s+(?:of|for)\s+"
    rf"(?:[A-Za-z0-9-]+\s+){{0,4}}{_VACCINE_GENERIC_CONDITION}\b"
    rf"[^.;:\n]{{0,50}}?\b(?:after|following)\b[^.;:\n]{{0,40}}?"
    rf"\b{_VACCINE_EXPOSURE_FORM}\b)|"
    rf"(?:\b{_VACCINE_GENERIC_CONDITION}\b[^.;:\n]{{0,30}}?"
    rf"\b(?:after|following)\b[^.;:\n]{{0,40}}?\b{_VACCINE_EXPOSURE_FORM}\b)|"
    rf"(?:\b{_VACCINE_EXPOSURE_FORM}\b[^.;:\n]{{0,50}}?"
    rf"\b(?:associated with|followed by)\b[^.;:\n]{{0,50}}?"
    rf"(?:\b(?:risk|incidence|odds|rates?|hazard)\s+(?:of|for)\s+)?"
    rf"(?:[A-Za-z0-9-]+\s+){{0,3}}{_VACCINE_GENERIC_CONDITION}\b)",
    re.IGNORECASE,
)
_VACCINE_FORWARD_GENERIC_PAIRED = re.compile(
    rf"\b{_VACCINE_EXPOSURE_FORM}\b[^.;:\n]{{0,35}}?\band\s+"
    rf"(?:subsequent(?:ly)?\s+)?(?:"
    rf"(?:risk|incidence|odds|rates?|hazard)\s+(?:of|for)\s+"
    rf"{_VACCINE_GENERIC_CONDITION}\b|"
    rf"{_VACCINE_GENERIC_CONDITION}\s+(?:risk|incidence|odds|rates?|hazard)\b|"
    rf"{_VACCINE_GENERIC_CONDITION}\b)",
    re.IGNORECASE,
)
_VACCINE_STATUS_COMPARISON = re.compile(
    rf"(?:\b{_VACCINE_GENERIC_CONDITION}\b[^.;:\n]{{0,35}}?"
    rf"\bamong\s+vaccinated\b[^.;:\n]{{0,35}}?"
    rf"\b(?:versus|vs\.?|compared with)\s+unvaccinated\b)|"
    rf"(?:\b{_VACCINE_GENERIC_CONDITION}\b[^.;:\n]{{0,35}}?"
    rf"\bby\s+(?:vaccination|immuni[sz]ation)\s+status\b)",
    re.IGNORECASE,
)
_VACCINE_BEHAVIOR_GENERIC_RELATION = re.compile(
    rf"\b{_VACCINE_MEASURED_EXPOSURE_FORM}\b{_NO_VACCINE_ATTITUDE}{{0,35}}?"
    rf"\b(?:associated with|followed by)\b{_NO_VACCINE_ATTITUDE}{{0,45}}?"
    rf"(?:\b(?:risk|incidence|odds|rates?|hazard)\s+(?:of|for)\s+)?"
    rf"(?:[A-Za-z0-9-]+\s+){{0,3}}{_VACCINE_GENERIC_CONDITION}\b",
    re.IGNORECASE,
)
_VACCINE_STRUCTURED_RELATION = re.compile(
    rf"\bexposures?\s*:\s*[^;.\n]{{0,60}}?\b{_VACCINE_EXPOSURE_FORM}\b"
    rf"\s*[;.]\s*(?:(?:main\s+)?outcomes?(?:\s+and\s+measures)?|"
    rf"primary\s+outcome)\s*:\s*[^;.\n]{{0,30}}?"
    rf"\b(?:{_VACCINE_ENDPOINT}|{_VACCINE_GENERIC_CONDITION})\b",
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
    population_facet: PopulationFacet
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


def classify_population(text: str) -> PopulationFacet:
    pediatric = bool(_PEDIATRIC.search(text))
    adult = bool(_ADULT.search(text))
    if pediatric and adult:
        return "mixed"
    if pediatric:
        return "pediatric"
    if adult:
        return "adult"
    return "unclear"


def classify_scope(text: str, query_arms: Iterable[str] = ()) -> ScopeClassification:
    """Classify scope using record text plus the arms that retrieved it."""
    tokens = _arm_tokens(query_arms)
    if _CORE.search(text) or "core" in tokens:
        return "core-exposomics"
    if _VACCINE.search(text) or "vaccine" in tokens:
        return "vaccine-exposure"
    if _MIXTURES.search(text) or {"mixture", "mixtures", "operational"} & tokens:
        return "operational-mixtures"
    if {"single", "adjacent", "ehr"} & tokens:
        return "adjacent-single-exposure"
    return "unclear"


def has_explicit_vaccine_outcome_relation(text: str) -> bool:
    """Return whether text explicitly links vaccination to a health outcome."""
    if _VACCINE_STRUCTURED_RELATION.search(text):
        return True
    for clause in re.split(r"[.;\n]+", text):
        if not clause.strip():
            continue
        if _VACCINE_UPTAKE.search(clause):
            # Within a behavioral/survey clause, a loose "vaccination and
            # disease" pairing is ambiguous.  Require a measured exposure form
            # (receipt/status/dose/schedule/uptake/coverage) or a directional
            # relation rather than arbitrary co-occurrence.
            if (
                _VACCINE_BEHAVIOR_FORWARD_RELATION.search(clause)
                or _VACCINE_BEHAVIOR_GENERIC_RELATION.search(clause)
            ):
                return True
            continue
        if (
            _VACCINE_FORWARD_RELATION.search(clause)
            or _VACCINE_REVERSE_RELATION.search(clause)
            or _VACCINE_INTRINSIC_OUTCOME.search(clause)
            or _VACCINE_GENERIC_RELATION.search(clause)
            or _VACCINE_FORWARD_GENERIC_PAIRED.search(clause)
            or _VACCINE_STATUS_COMPARISON.search(clause)
        ):
            return True
    return False


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

    non_primary_type = sorted(types & _NON_PRIMARY_TYPES)
    if non_primary_type:
        primary: bool | None = False
        reasons.append("non-primary publication type: " + ", ".join(non_primary_type))
    elif _NON_PRIMARY_TITLE.search(title):
        primary = False
        reasons.append("title identifies a review/protocol/correction or conference item")
    elif types & _PRIMARY_TYPES:
        primary = True
        evidence.append("primary-study-compatible publication type: " + ", ".join(sorted(types & _PRIMARY_TYPES)))
    elif _PRIMARY_DESIGN.search(text):
        primary = True
        evidence.append("primary study design/population language in title or abstract")
    else:
        primary = None

    if _NONHUMAN_LAB.search(title) and not _HUMAN_PARTICIPANT.search(title):
        human: bool | None = False
        reasons.append("title identifies an in-vitro, cell-line, or organoid-only study")
    elif "humans" in mesh or "human" in mesh:
        human: bool | None = True
        evidence.append("PubMed/PMC subject metadata includes Humans")
    elif "animals" in mesh and not ({"humans", "human"} & mesh):
        human = False
        reasons.append("subject metadata identifies an animal-only study")
    elif _ANIMAL_ONLY.search(title) and not _HUMAN_PARTICIPANT.search(title):
        human = False
        reasons.append("title identifies an animal-only or in-vitro study")
    elif _HUMAN.search(text):
        human = True
        evidence.append("human population language in title or abstract")
    else:
        human = None

    scope = classify_scope(text, query_arms)
    population = classify_population(text)
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

    arm_tokens = _arm_tokens(query_arms)
    vaccine_candidate = bool(_VACCINE.search(text) or "vaccine" in arm_tokens)
    vaccine_gate_ok = True
    if vaccine_candidate:
        explicit_vaccine_outcome = has_explicit_vaccine_outcome_relation(text)
        if explicit_vaccine_outcome:
            evidence.append("explicit vaccine exposure-to-health/biological-outcome relation")
        else:
            vaccine_gate_ok = False
            if _VACCINE_UPTAKE.search(text):
                reasons.append("vaccine uptake/attitude record has no exposure-outcome design")
            else:
                evidence.append(
                    "manual review required; vaccine exposure-outcome relation is not explicit"
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
        and scope not in {"unclear", "out-of-scope"}
        and vaccine_gate_ok
        and metadata_complete is not False
        and bool(provenance)
    ):
        decision = "included"
    else:
        decision = "pending"

    reviewer = ""
    reviewed_at = ""
    method = "automated-metadata-v1"
    if override:
        requested = str(override.get("decision", "")).strip().lower()
        override_scope = str(override.get("scope_classification", "")).strip()
        if override_scope in {
            "core-exposomics", "operational-mixtures", "vaccine-exposure",
            "adjacent-single-exposure", "out-of-scope", "unclear",
        }:
            scope = override_scope  # type: ignore[assignment]
        override_population = str(override.get("population_facet", "")).strip()
        if override_population in {"adult", "pediatric", "mixed", "unclear"}:
            population = override_population  # type: ignore[assignment]
        evidence.extend(_strings(override.get("eligibility_evidence")))
        reasons.extend(_strings(override.get("exclusion_reasons")))
        reviewer = str(override.get("reviewer", "") or "")
        reviewed_at = str(override.get("reviewed_at", "") or "")
        if isinstance(override.get("human_study"), bool):
            human = bool(override["human_study"])
        if isinstance(override.get("primary_study"), bool):
            primary = bool(override["primary_study"])
        if requested in {"included", "excluded", "pending"}:
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

    return ScreeningDecision(
        pmcid=uid,
        title=title,
        decision=decision,
        scope_classification=scope,
        population_facet=population,
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
