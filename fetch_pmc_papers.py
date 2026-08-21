#!/usr/bin/env python3
"""
Fetch and download open-access PMC papers for an all-age exposome evidence map.

Pipeline:
  1. Search PMC using named core, operational, and adjacent query arms.
  2. Fetch metadata and persist explicit included/excluded/pending screening.
  3. Resolve a full-text PDF: NCBI PMC OA (PDF or tar.gz) -> Europe PMC fallback.

Europe PMC is used as a fallback for papers that are open access but have no
resolvable PDF link on the NCBI OA service (e.g. project-design / late-deposited
articles such as the EXPOsOMICS project paper, PMC6192011).
"""

import io
import json
import os
import re
import tarfile
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Mapping

import requests
import xml.etree.ElementTree as ET

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from paper_manifest import PaperManifest, reconcile_manifest, validate_download_payload
from screening import load_screening_overrides, partition_screening, screen_candidate

# ── Config ────────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path("papers")
NCBI_BASE  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PMC_OA_API = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi"
EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
DELAY      = 0.4   # seconds between API calls (NCBI limit ≈ 3/sec without key)
NCBI_TOOL  = os.getenv("NCBI_TOOL", "exposome_ehr_review")
NCBI_EMAIL = os.getenv("NCBI_EMAIL", "")
NCBI_API_KEY = os.getenv("NCBI_API_KEY")
ESEARCH_PAGE_SIZE = 200
NCBI_MAX_RETRIES = 4
NCBI_BACKOFF_SECONDS = 1.0
NCBI_HISTORY_ID_LIMIT = 9_999
NCBI_ESUMMARY_PAGE_LIMIT = 500
PMC_SNAPSHOT_START = date(1900, 1, 1)

# ── Search strategy ──────────────────────────────────────────────────────────
# Goal: an all-age evidence map with high-precision core exposomics, an
# operational mixtures arm, vaccine exposure-outcome studies, and separately
# labeled adjacent single-exposure/EHR context.
#
# All terms use [Title/Abstract] so the concepts must appear in the abstract,
# not just in references or acknowledgements.
#
# PMC has no Publication Type search field. Publication-type screening happens
# after discovery using PMID-resolved PubMed metadata. Using PubMed's field tag
# here silently remaps it to All Fields and drops relevant primary studies.
#
_FILTERS = (
    # Discovery stays recall-oriented. Review/article-type and population
    # decisions are explicit record-level screening facets downstream.
    '"open access"[filter]'
)

# EHR synonyms — text terms plus MeSH terms (broadens beyond literal
# "EHR"/"claims data" phrasing in the abstract, since PMC indexing often
# tags a paper with these MeSH terms even when the author never writes
# "EHR" or "administrative data" as a literal phrase).
_EHR_TA = (
    '"electronic health record"[Title/Abstract] OR '
    '"electronic medical record"[Title/Abstract] OR '
    '"EHR"[Title/Abstract] OR '
    '"EMR"[Title/Abstract] OR '
    '"claims data"[Title/Abstract] OR '
    '"administrative health data"[Title/Abstract] OR '
    '"health records"[Title/Abstract] OR '
    '"Electronic Health Records"[MeSH Terms] OR '
    '"Medical Records Systems, Computerized"[MeSH Terms] OR '
    '"Registries"[MeSH Terms]'
)

# Pediatric / childhood population terms are retained only for explicitly
# labeled adjacent arms. Core, mixture, linked-core, and vaccine arms are
# all-age; record-level screening assigns the population facet.
_PED_TERMS = (
    '"pediatric"[Title/Abstract] OR "paediatric"[Title/Abstract] OR '
    '"child"[Title/Abstract] OR "children"[Title/Abstract] OR '
    '"childhood"[Title/Abstract] OR "infant"[Title/Abstract] OR '
    '"newborn"[Title/Abstract] OR "neonatal"[Title/Abstract] OR '
    '"adolescent"[Title/Abstract] OR "adolescence"[Title/Abstract] OR '
    '"youth"[Title/Abstract] OR "early life"[Title/Abstract] OR '
    '"pediatrics"[Title/Abstract]'
)
_PEDIATRIC_TA = f'({_PED_TERMS})'
SEARCH_QUERIES = [
    # ── Core: explicit all-age EWAS / exposome-wide discovery ─────────────
    f'("Exposome"[MeSH Terms] OR exposom*[Title/Abstract] OR ExWAS[Title/Abstract] '
    f'OR "P-ExWAS"[Title/Abstract] OR "environment-wide association"[Title/Abstract] '
    f'OR "exposure-wide association"[Title/Abstract] '
    f'OR "exposome-wide association"[Title/Abstract]) {_FILTERS}',
    f'(exposom*[Title/Abstract] OR "environment-wide association"[Title/Abstract] '
    f'OR "exposure-wide association"[Title/Abstract]) '
    f'(association[Title/Abstract] OR risk[Title/Abstract] OR cohort[Title/Abstract]) {_FILTERS}',

    # ── Operational exposomics arm: mixtures / multiple exposures ───────
    f'((exposure*[Title/Abstract] OR environmental[Title/Abstract] OR '
    f'chemical*[Title/Abstract]) (mixture*[Title/Abstract] OR '
    f'"multiple exposure*"[Title/Abstract] OR "combined exposure*"[Title/Abstract] '
    f'OR "cumulative exposure*"[Title/Abstract])) '
    f'(association[Title/Abstract] OR risk[Title/Abstract] '
    f'OR outcome*[Title/Abstract] OR effect*[Title/Abstract]) {_FILTERS}',

    # ── Adjacent: environmental exposure + EHR + pediatric context ─────────
    f'({ _EHR_TA }) "environmental exposure"[Title/Abstract] {_PEDIATRIC_TA} (association[Title/Abstract] OR health[Title/Abstract]) {_FILTERS}',
    f'({ _EHR_TA }) "air pollution"[Title/Abstract] {_PEDIATRIC_TA} (cohort[Title/Abstract] OR association[Title/Abstract]) {_FILTERS}',
    f'({ _EHR_TA }) "chemical exposure"[Title/Abstract] {_PEDIATRIC_TA} health[Title/Abstract] {_FILTERS}',
    f'({ _EHR_TA }) "neighborhood environment"[Title/Abstract] {_PEDIATRIC_TA} {_FILTERS}',
    f'({ _EHR_TA }) "built environment"[Title/Abstract] {_PEDIATRIC_TA} health[Title/Abstract] {_FILTERS}',
    f'({ _EHR_TA }) "social determinants of health"[Title/Abstract] {_PEDIATRIC_TA} {_FILTERS}',
    f'({ _EHR_TA }) "prenatal"[Title/Abstract] ({_PED_TERMS}) exposure[Title/Abstract] {_FILTERS}',

    # ── Adjacent: geospatial/contextual exposure linked to pediatric EHR ───
    f'({ _EHR_TA }) ("deprivation index"[Title/Abstract] OR "area deprivation"[Title/Abstract]) {_PEDIATRIC_TA} {_FILTERS}',
    f'({ _EHR_TA }) ("geospatial"[Title/Abstract] OR "geocod"[Title/Abstract]) {_PEDIATRIC_TA} exposure[Title/Abstract] {_FILTERS}',

    # ── Linked-core plus adjacent birth-cohort/single-exposure context ──────
    f'(exposom*[Title/Abstract] OR "environment-wide association"[Title/Abstract]) ("birth cohort"[Title/Abstract] OR cohort[Title/Abstract] OR "linked data"[Title/Abstract] OR "administrative data"[Title/Abstract]) {_FILTERS}',
    f'("prenatal exposure"[Title/Abstract] OR "prenatal exposome"[Title/Abstract] OR "early-life exposure"[Title/Abstract] OR "early life exposome"[Title/Abstract]) ({_PED_TERMS}) (association[Title/Abstract] OR risk[Title/Abstract]) {_FILTERS}',
    f'("air pollution"[Title/Abstract] OR "particulate matter"[Title/Abstract] OR PM2.5[Title/Abstract]) ({_PED_TERMS}) ("birth cohort"[Title/Abstract] OR cohort[Title/Abstract] OR "linked data"[Title/Abstract]) (asthma[Title/Abstract] OR respiratory[Title/Abstract] OR birth[Title/Abstract]) {_FILTERS}',
    f'("blood lead"[Title/Abstract] OR "chemical exposure"[Title/Abstract] OR "endocrine disruptor"[Title/Abstract]) ({_PED_TERMS}) (cohort[Title/Abstract] OR "linked data"[Title/Abstract] OR "administrative data"[Title/Abstract]) {_FILTERS}',
    f'({ _EHR_TA }) ("birth cohort"[Title/Abstract] OR "linked data"[Title/Abstract]) ({_PED_TERMS}) exposure[Title/Abstract] {_FILTERS}',

    # ── Operational: all-age vaccine / immunization as the exposure ────────
    # Vaccination is treated as an exposure (vaccine type / schedule / timing)
    # predicting a health/biomarker outcome (safety, febrile seizure,
    # asthma, BMI, infection, fever, neurodevelopment, autoimmune, SIDS).
    # No EHR term required: vaccine studies are often registry/claims/cohort
    # based and don't name 'EHR' in the abstract (same rationale as Tier 4).
    f'(vaccine[Title/Abstract] OR vaccination[Title/Abstract] OR vaccinated[Title/Abstract] '
    f'OR immunization[Title/Abstract] OR immunisation[Title/Abstract]) '
    f'("adverse event"[Title/Abstract] OR "febrile seizure"[Title/Abstract] '
    f'OR "vaccine safety"[Title/Abstract] OR safety[Title/Abstract] '
    f'OR "vaccine-associated"[Title/Abstract] OR reactogenicity[Title/Abstract] '
    f'OR immunogenicity[Title/Abstract] OR "immune response"[Title/Abstract] '
    f'OR antibody[Title/Abstract] OR antibodies[Title/Abstract] '
    f'OR biomarker*[Title/Abstract] OR infection[Title/Abstract] '
    f'OR hospitali*[Title/Abstract] OR effectiveness[Title/Abstract] '
    f'OR mortality[Title/Abstract] OR morbidity[Title/Abstract]) {_FILTERS}',
    f'(MMR[Title] OR DTaP[Title] OR "BCG vaccine"[Title] '
    f'OR "rotavirus vaccine"[Title] OR "HPV vaccine"[Title] '
    f'OR "human papillomavirus vaccine"[Title] '
    f'OR "human papillomavirus vaccination"[Title] '
    f'OR "influenza vaccine"[Title] OR "COVID-19 vaccine"[Title] '
    f'OR "SARS-CoV-2 vaccine"[Title] OR "pneumococcal vaccine"[Title] '
    f'OR "meningococcal vaccine"[Title] OR "hepatitis vaccine"[Title] '
    f'OR "zoster vaccine"[Title] OR "RSV vaccine"[Title]) '
    f'(safety[Title/Abstract] OR reactogenicity[Title/Abstract] '
    f'OR immunogenicity[Title/Abstract] OR "immune response"[Title/Abstract] '
    f'OR antibody[Title/Abstract] OR antibodies[Title/Abstract] '
    f'OR biomarker*[Title/Abstract] OR infection[Title/Abstract] '
    f'OR hospitali*[Title/Abstract] OR effectiveness[Title/Abstract] '
    f'OR "adverse event"[Title/Abstract] OR "febrile seizure"[Title/Abstract] '
    f'OR "following immunization"[Title/Abstract]) {_FILTERS}',
    f'("vaccine safety"[Title/Abstract] OR "vaccine schedule"[Title/Abstract] '
    f'OR "vaccination schedule"[Title/Abstract] OR "vaccine exposure"[Title/Abstract]) '
    f'(cohort[Title/Abstract] OR trial[Title/Abstract] OR longitudinal[Title/Abstract] '
    f'OR "linked data"[Title/Abstract] OR "administrative data"[Title/Abstract] '
    f'OR claims[Title/Abstract]) '
    f'(outcome*[Title/Abstract] OR risk[Title/Abstract] OR safety[Title/Abstract] '
    f'OR effect*[Title/Abstract] OR immunogenicity[Title/Abstract] '
    f'OR "immune response"[Title/Abstract] OR biomarker*[Title/Abstract] '
    f'OR infection[Title/Abstract] OR hospitali*[Title/Abstract]) {_FILTERS}',
]

MAX_PER_QUERY = ESEARCH_PAGE_SIZE  # compatibility alias; no longer a total-result cap


@dataclass(frozen=True)
class QuerySpec:
    """A stable query name, review arm, exact query, and declared facets."""

    name: str
    arm: str
    query: str
    facets: Mapping[str, object]


_QUERY_REGISTRY = [
    ("core_exposome", "core", False, "all_age"),
    ("core_exposome_association", "core", False, "all_age"),
    ("operational_mixtures", "operational", False, "all_age"),
    ("adjacent_environment_ehr", "adjacent_single_exposure", True, "pediatric_or_early_life"),
    ("adjacent_air_pollution_ehr", "adjacent_single_exposure", True, "pediatric_or_early_life"),
    ("adjacent_chemical_ehr", "adjacent_single_exposure", True, "pediatric_or_early_life"),
    ("adjacent_neighborhood_ehr", "adjacent_single_exposure", True, "pediatric_or_early_life"),
    ("adjacent_built_environment_ehr", "adjacent_single_exposure", True, "pediatric_or_early_life"),
    ("adjacent_sdoh_ehr", "adjacent_single_exposure", True, "pediatric_or_early_life"),
    ("adjacent_prenatal_ehr", "adjacent_single_exposure", True, "pediatric_or_early_life"),
    ("adjacent_deprivation_ehr", "adjacent_single_exposure", True, "pediatric_or_early_life"),
    ("adjacent_geospatial_ehr", "adjacent_single_exposure", True, "pediatric_or_early_life"),
    ("core_exposome_linked_data", "core", False, "all_age"),
    ("adjacent_prenatal_early_life", "adjacent_single_exposure", False, "pediatric_or_early_life"),
    ("adjacent_air_pollution_cohort", "adjacent_single_exposure", False, "pediatric_or_early_life"),
    ("adjacent_chemical_cohort", "adjacent_single_exposure", False, "pediatric_or_early_life"),
    ("adjacent_exposure_linked_ehr", "adjacent_single_exposure", True, "pediatric_or_early_life"),
    ("operational_vaccine_adverse_events", "operational_vaccine", False, "all_age"),
    ("operational_named_vaccine_safety", "operational_vaccine", False, "all_age"),
    ("operational_vaccine_linked_data", "operational_vaccine", False, "all_age"),
]

assert len(_QUERY_REGISTRY) == len(SEARCH_QUERIES)
SEARCH_QUERY_SPECS = [
    QuerySpec(
        name=name,
        arm=arm,
        query=query,
        facets={
            "population_scope": population_scope,
            "population_classification": "screen_record_as_adult_pediatric_mixed_or_unclear",
            "scope": arm,
            "ehr_required": ehr_required,
        },
    )
    for (name, arm, ehr_required, population_scope), query
    in zip(_QUERY_REGISTRY, SEARCH_QUERIES)
]

CORE_RECALL_REGRESSION_PMCS = frozenset({
    "PMC9678903",   # human early-life exposome multi-omics
    "PMC6144482",   # HELIX cohort rationale/design
    "PMC11117089",  # prenatal chemical mixtures and child metabolic risk
    "PMC10099694",  # prenatal exposures and childhood outcomes
    "PMC13099396",  # large-scale all-age exposome study
})


@dataclass(frozen=True)
class SearchShard:
    """One date-bounded NCBI history snapshot used for complete retrieval."""

    query: str
    date_from: str
    date_to: str
    count: int
    retrieved_at: str
    is_leaf: bool
    pages: int = 0
    page_starts: tuple[int, ...] = ()
    page_counts: tuple[int, ...] = ()
    query_translation: str = ""


@dataclass(frozen=True)
class SearchResult:
    """A complete, reconciled ESearch result for one exact query."""

    query: str
    count: int
    ids: tuple[str, ...]
    retrieved_at: str
    pages: int
    page_counts: tuple[int, ...]
    query_translation: str
    page_starts: tuple[int, ...] = ()
    snapshot_date: str = ""
    effective_query: str = ""
    shards: tuple[SearchShard, ...] = ()


class NCBIRetrievalError(RuntimeError):
    """Raised when an NCBI stage cannot be proven complete."""


# Fields in the PMC User Guide. Deliberately excludes PubMed-only
# [Publication Type]/[pt], which PMC otherwise remaps without a hard error.
SUPPORTED_PMC_FIELDS = frozenset({
    "ack", "acknowledgements", "ad", "affiliation", "all", "all fields",
    "au", "author", "auid", "author - identifier", "body", "coi", "cois",
    "conflict of interest statement", "cn", "corporate author", "das",
    "data availability", "rn", "ec/rn number", "ed", "editor", "epdat",
    "electronic publication date", "edat", "entry date", "capt",
    "figure/table caption", "filter", "fauth", "first author name", "fau",
    "full author name", "gr", "grant number", "ir", "investigator", "iss",
    "issue", "journal", "ta", "journal title", "la", "language", "lauth",
    "last author name", "lid", "location id", "majr", "mesh major topic",
    "sh", "subheading", "mesh subheading", "mh", "mesh", "mesh terms",
    "meth", "methods", "jid", "nlm unique id", "pg", "pagination", "uid",
    "pmcid", "pmcrdat", "pmc live date", "pmid", "ppdat",
    "print publication date", "dp", "pubdate", "publication date", "refr",
    "reference", "refa", "reference author", "sect", "section title", "nm",
    "supplementary concept", "ti", "title", "tiab", "title/abstract", "vol",
    "volume",
})
_FIELD_TAG_RE = re.compile(r"\[([^\]]+)\]")
_FIELD_ALIASES = {
    "mh": "mesh terms",
    "mesh": "mesh terms",
    "tiab": "title/abstract",
    "ti": "title",
    "all": "all fields",
    "pmcrdat": "pmc live date",
}


def validate_pmc_query(query: str) -> None:
    """Reject empty queries and any field tag not documented for PMC."""
    if not query or not query.strip():
        raise ValueError("PMC query must not be empty")
    unsupported = sorted({
        " ".join(tag.casefold().split())
        for tag in _FIELD_TAG_RE.findall(query)
        if " ".join(tag.casefold().split()) not in SUPPORTED_PMC_FIELDS
    })
    if unsupported:
        raise ValueError(f"Unsupported PMC search field(s): {', '.join(unsupported)}")


def _canonical_field_tags(query: str) -> set[str]:
    return {
        _FIELD_ALIASES.get(tag, tag)
        for raw in _FIELD_TAG_RE.findall(query)
        if (tag := " ".join(raw.casefold().split()))
    }


def validate_query_translation(query: str, translation: str) -> None:
    """Fail when NCBI drops an explicit field or silently maps it to All Fields."""
    if not translation.strip():
        raise NCBIRetrievalError("PMC ESearch returned an empty query translation")
    try:
        validate_pmc_query(translation)
    except ValueError as exc:
        raise NCBIRetrievalError(
            f"PMC ESearch translation contains an unsupported field: {exc}"
        ) from exc
    requested = _canonical_field_tags(query)
    translated = _canonical_field_tags(translation)
    missing = sorted(requested - translated)
    if missing:
        raise NCBIRetrievalError(
            "PMC ESearch translation dropped field(s): " + ", ".join(missing)
        )
    if "all fields" in translated and "all fields" not in requested:
        raise NCBIRetrievalError(
            "PMC ESearch silently remapped an explicitly tagged term to All Fields"
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ── Helpers ───────────────────────────────────────────────────────────────────

def ftp_to_https(url: str) -> str:
    """NCBI FTP and HTTPS share the same path — swap the scheme."""
    return url.replace("ftp://ftp.ncbi.nlm.nih.gov", "https://ftp.ncbi.nlm.nih.gov", 1)


def sanitize(text: str, maxlen: int = 80) -> str:
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'\s+', '_', text.strip())
    return text[:maxlen]


_RETRYABLE_HTTP_STATUSES = frozenset({429, *range(500, 600)})


def _retry_after_seconds(value: str | None, *, now: Callable[[], datetime]) -> float | None:
    """Parse Retry-After as delta-seconds or an HTTP date."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        current = now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - current).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _eutils_identity_params(
    *, tool: str, email: str, api_key: str | None,
) -> dict[str, str]:
    if not tool.strip():
        raise ValueError("NCBI tool must not be empty")
    if not email.strip():
        raise ValueError("NCBI email must not be empty")
    params = {"tool": tool, "email": email}
    if api_key:
        params["api_key"] = api_key
    return params


def _request_with_retry(
    url: str,
    *,
    params: Mapping[str, object],
    timeout: float,
    session=requests,
    max_retries: int = NCBI_MAX_RETRIES,
    backoff_seconds: float = NCBI_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
):
    """GET once plus bounded retries for transport errors, 429, and 5xx."""
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    for attempt in range(max_retries + 1):
        try:
            response = session.get(url, timeout=timeout, params=dict(params))
        except requests.RequestException as exc:
            if attempt == max_retries:
                raise NCBIRetrievalError(
                    f"NCBI request failed after {attempt + 1} attempts: {exc}"
                ) from exc
            sleep(backoff_seconds * (2 ** attempt))
            continue

        status = response.status_code
        if status in _RETRYABLE_HTTP_STATUSES:
            if attempt == max_retries:
                raise NCBIRetrievalError(
                    f"NCBI request failed after {attempt + 1} attempts: HTTP {status}"
                )
            retry_after = _retry_after_seconds(
                response.headers.get("Retry-After"), now=now,
            )
            sleep(retry_after if retry_after is not None
                  else backoff_seconds * (2 ** attempt))
            continue
        if status >= 400:
            raise NCBIRetrievalError(f"NCBI request failed: HTTP {status}")
        return response
    raise AssertionError("unreachable")


def _json_object(response, *, stage: str) -> dict:
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise NCBIRetrievalError(f"{stage} returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise NCBIRetrievalError(f"{stage} returned a non-object JSON payload")
    return payload


@dataclass(frozen=True)
class _HistorySearch:
    count: int
    query_key: str
    webenv: str
    query_translation: str
    retrieved_at: str


def _warning_values(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _validate_esearch_details(data: Mapping[str, object], *, count: int) -> None:
    error = data.get("ERROR") or data.get("error")
    if error:
        raise NCBIRetrievalError(f"PMC ESearch error: {error}")
    errorlist = data.get("errorlist", {})
    if errorlist and not isinstance(errorlist, Mapping):
        raise NCBIRetrievalError("PMC ESearch returned a malformed errorlist")
    if isinstance(errorlist, Mapping):
        errors = [
            value
            for values in errorlist.values()
            for value in _warning_values(values)
        ]
        if errors:
            raise NCBIRetrievalError(
                "PMC ESearch rejected search detail(s): " + "; ".join(errors)
            )

    warninglist = data.get("warninglist", {})
    if warninglist and not isinstance(warninglist, Mapping):
        raise NCBIRetrievalError("PMC ESearch returned a malformed warninglist")
    if isinstance(warninglist, Mapping):
        warnings: list[str] = []
        for key, values in warninglist.items():
            for value in _warning_values(values):
                if count == 0 and key == "outputmessages" and value == "No items found.":
                    continue
                warnings.append(f"{key}: {value}")
        if warnings:
            raise NCBIRetrievalError(
                "PMC ESearch returned unresolved search warning(s): "
                + "; ".join(warnings)
            )


def _snapshot_query(query: str, lower: date, upper: date) -> str:
    return (
        f'({query}) AND ("{lower:%Y/%m/%d}"[PMC Live Date] : '
        f'"{upper:%Y/%m/%d}"[PMC Live Date])'
    )


def _coerce_snapshot_date(value: date | str | None) -> date:
    if value is None:
        # PMC Live Date is day-granular. Using the last completed UTC day keeps
        # every shard immutable while the run is in progress.
        return datetime.now(timezone.utc).date() - timedelta(days=1)
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value.replace("/", "-"))
    except ValueError as exc:
        raise ValueError("snapshot_date must be YYYY-MM-DD or YYYY/MM/DD") from exc


def _open_history_search(
    query: str,
    *,
    identity: Mapping[str, str],
    session,
    max_retries: int,
    backoff_seconds: float,
    sleep: Callable[[float], None],
    timestamp: Callable[[], str],
) -> _HistorySearch:
    response = _request_with_retry(
        f"{NCBI_BASE}/esearch.fcgi",
        params={
            "db": "pmc",
            "term": query,
            "retmax": 0,
            "retmode": "json",
            "sort": "relevance",
            "usehistory": "y",
            **identity,
        },
        timeout=15,
        session=session,
        max_retries=max_retries,
        backoff_seconds=backoff_seconds,
        sleep=sleep,
    )
    payload = _json_object(response, stage="PMC ESearch")
    if payload.get("error"):
        raise NCBIRetrievalError(f"PMC ESearch error: {payload['error']}")
    data = payload.get("esearchresult")
    if not isinstance(data, dict):
        raise NCBIRetrievalError("PMC ESearch response has no esearchresult object")
    try:
        count = int(data["count"])
    except (KeyError, TypeError, ValueError) as exc:
        detail = data.get("ERROR") or data.get("error") or "no valid count"
        raise NCBIRetrievalError(f"PMC ESearch response has {detail}") from exc
    if count < 0:
        raise NCBIRetrievalError("PMC ESearch returned a negative count")
    _validate_esearch_details(data, count=count)
    translation = str(data.get("querytranslation", ""))
    validate_query_translation(query, translation)
    query_key = str(data.get("querykey", "") or "")
    webenv = str(data.get("webenv", "") or "")
    if count and (not query_key or not webenv):
        raise NCBIRetrievalError(
            "PMC ESearch did not create the required frozen history snapshot"
        )
    return _HistorySearch(
        count=count,
        query_key=query_key,
        webenv=webenv,
        query_translation=translation,
        retrieved_at=timestamp(),
    )


def _history_ids(
    history: _HistorySearch,
    *,
    page_size: int,
    identity: Mapping[str, str],
    session,
    max_retries: int,
    backoff_seconds: float,
    sleep: Callable[[float], None],
) -> tuple[list[str], list[int], list[int]]:
    ids: list[str] = []
    seen: set[str] = set()
    page_starts: list[int] = []
    page_counts: list[int] = []
    for retstart in range(0, history.count, page_size):
        response = _request_with_retry(
            f"{NCBI_BASE}/esummary.fcgi",
            params={
                "db": "pmc",
                "query_key": history.query_key,
                "WebEnv": history.webenv,
                "retstart": retstart,
                "retmax": min(page_size, history.count - retstart),
                "retmode": "json",
                **identity,
            },
            timeout=30,
            session=session,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            sleep=sleep,
        )
        payload = _json_object(response, stage="PMC ESummary history page")
        if payload.get("error"):
            raise NCBIRetrievalError(
                f"PMC ESummary history page failed at retstart={retstart}: "
                f"{payload['error']}"
            )
        result = payload.get("result")
        page_ids = result.get("uids") if isinstance(result, dict) else None
        if not isinstance(page_ids, list) or any(
            not isinstance(uid, str) or not uid for uid in page_ids
        ):
            raise NCBIRetrievalError(
                f"PMC ESummary history page at retstart={retstart} has no valid uids"
            )
        expected = min(page_size, history.count - retstart)
        if len(page_ids) != expected:
            raise NCBIRetrievalError(
                f"Incomplete PMC history page at retstart={retstart}: "
                f"expected {expected}, received {len(page_ids)}"
            )
        duplicates = seen.intersection(page_ids)
        if duplicates or len(page_ids) != len(set(page_ids)):
            repeated = duplicates or {
                uid for uid in page_ids if page_ids.count(uid) > 1
            }
            raise NCBIRetrievalError(
                "PMC history returned duplicate IDs: "
                + ", ".join(sorted(repeated))
            )
        missing_records = [uid for uid in page_ids if uid not in result]
        if missing_records:
            raise NCBIRetrievalError(
                "PMC ESummary history page omitted metadata for UID(s): "
                + ", ".join(missing_records)
            )
        ids.extend(page_ids)
        seen.update(page_ids)
        page_starts.append(retstart)
        page_counts.append(len(page_ids))
        sleep(DELAY)
    if len(ids) != history.count:
        raise NCBIRetrievalError(
            f"PMC history reconciliation failed: count={history.count}, ids={len(ids)}"
        )
    return ids, page_starts, page_counts


def search_pmc(
    query: str,
    *,
    page_size: int = ESEARCH_PAGE_SIZE,
    tool: str = NCBI_TOOL,
    email: str = NCBI_EMAIL,
    api_key: str | None = NCBI_API_KEY,
    session=requests,
    max_retries: int = NCBI_MAX_RETRIES,
    backoff_seconds: float = NCBI_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    timestamp: Callable[[], str] = _utc_now,
    snapshot_date: date | str | None = None,
    max_history_ids: int = NCBI_HISTORY_ID_LIMIT,
) -> SearchResult:
    """Retrieve a complete, date-frozen PMC snapshot, partitioning >9,999 hits."""
    validate_pmc_query(query)
    if page_size <= 0 or page_size > NCBI_ESUMMARY_PAGE_LIMIT:
        raise ValueError(
            f"page_size must be between 1 and {NCBI_ESUMMARY_PAGE_LIMIT}"
        )
    if max_history_ids <= 0 or max_history_ids > NCBI_HISTORY_ID_LIMIT:
        raise ValueError(
            f"max_history_ids must be between 1 and {NCBI_HISTORY_ID_LIMIT}"
        )
    cutoff = _coerce_snapshot_date(snapshot_date)
    if cutoff < PMC_SNAPSHOT_START:
        raise ValueError(f"snapshot_date must not precede {PMC_SNAPSHOT_START}")
    identity = _eutils_identity_params(tool=tool, email=email, api_key=api_key)

    def retrieve_range(
        lower: date, upper: date,
    ) -> tuple[list[str], int, list[SearchShard]]:
        effective_query = _snapshot_query(query, lower, upper)
        history = _open_history_search(
            effective_query,
            identity=identity,
            session=session,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            sleep=sleep,
            timestamp=timestamp,
        )
        sleep(DELAY)
        if history.count > max_history_ids:
            if lower == upper:
                raise NCBIRetrievalError(
                    f"PMC shard {lower.isoformat()} still has {history.count} hits, "
                    f"above the {max_history_ids}-ID history limit"
                )
            midpoint = lower + timedelta(days=(upper - lower).days // 2)
            left_ids, left_count, left_shards = retrieve_range(lower, midpoint)
            right_ids, right_count, right_shards = retrieve_range(
                midpoint + timedelta(days=1), upper,
            )
            if left_count + right_count != history.count:
                raise NCBIRetrievalError(
                    "PMC date-shard counts do not reconcile with their parent: "
                    f"{left_count} + {right_count} != {history.count} "
                    f"for {lower.isoformat()}..{upper.isoformat()}"
                )
            shard = SearchShard(
                query=effective_query,
                date_from=lower.isoformat(),
                date_to=upper.isoformat(),
                count=history.count,
                retrieved_at=history.retrieved_at,
                is_leaf=False,
                query_translation=history.query_translation,
            )
            return (
                [*left_ids, *right_ids],
                history.count,
                [shard, *left_shards, *right_shards],
            )

        shard_ids, starts, counts = _history_ids(
            history,
            page_size=page_size,
            identity=identity,
            session=session,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            sleep=sleep,
        )
        shard = SearchShard(
            query=effective_query,
            date_from=lower.isoformat(),
            date_to=upper.isoformat(),
            count=history.count,
            retrieved_at=history.retrieved_at,
            is_leaf=True,
            pages=len(counts),
            page_starts=tuple(starts),
            page_counts=tuple(counts),
            query_translation=history.query_translation,
        )
        return shard_ids, history.count, [shard]

    ids, reported_count, shards = retrieve_range(PMC_SNAPSHOT_START, cutoff)
    seen_ids: set[str] = set()
    duplicates = {uid for uid in ids if uid in seen_ids or seen_ids.add(uid)}
    if duplicates or len(ids) != reported_count:
        detail = f"; duplicate IDs: {', '.join(sorted(duplicates))}" if duplicates else ""
        raise NCBIRetrievalError(
            f"PMC shard reconciliation failed: count={reported_count}, ids={len(ids)}"
            + detail
        )
    leaf_shards = [shard for shard in shards if shard.is_leaf]
    starts = tuple(start for shard in leaf_shards for start in shard.page_starts)
    counts = tuple(count for shard in leaf_shards for count in shard.page_counts)
    print(
        f"  hits: {reported_count:>5}  |  retrieved: {len(ids)}  |  "
        f"shards: {len(leaf_shards)}  |  pages: {len(counts)}"
    )
    return SearchResult(
        query=query,
        count=reported_count,
        ids=tuple(ids),
        retrieved_at=timestamp(),
        pages=len(counts),
        page_counts=counts,
        query_translation=shards[0].query_translation,
        page_starts=starts,
        snapshot_date=cutoff.isoformat(),
        effective_query=shards[0].query,
        shards=tuple(shards),
    )


def fetch_summaries(
    ids: list[str],
    *,
    tool: str = NCBI_TOOL,
    email: str = NCBI_EMAIL,
    api_key: str | None = NCBI_API_KEY,
    session=requests,
    max_retries: int = NCBI_MAX_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    if not ids:
        return {}
    params: dict[str, object] = {
        "db": "pmc",
        "id": ",".join(ids),
        "retmode": "json",
        **_eutils_identity_params(tool=tool, email=email, api_key=api_key),
    }
    response = _request_with_retry(
        f"{NCBI_BASE}/esummary.fcgi",
        timeout=15,
        params=params,
        session=session,
        max_retries=max_retries,
        sleep=sleep,
    )
    payload = _json_object(response, stage="PMC ESummary")
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("uids"), list):
        raise NCBIRetrievalError("PMC ESummary response has no valid result/uids")
    if set(result["uids"]) != set(ids):
        raise NCBIRetrievalError(
            "PMC ESummary did not return metadata for every requested PMCID"
        )
    return result


def _xml_text(node: ET.Element | None) -> str:
    return "" if node is None else "".join(node.itertext()).strip()


def fetch_pubmed_metadata(
    pmids: list[str],
    *,
    tool: str = NCBI_TOOL,
    email: str = NCBI_EMAIL,
    api_key: str | None = NCBI_API_KEY,
    session=requests,
    max_retries: int = NCBI_MAX_RETRIES,
    backoff_seconds: float = NCBI_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, dict]:
    """Fetch screening-grade PubMed metadata, failing if any PMID is omitted."""
    requested = list(dict.fromkeys(str(pmid) for pmid in pmids if str(pmid)))
    if not requested:
        return {}
    response = _request_with_retry(
        f"{NCBI_BASE}/efetch.fcgi",
        params={
            "db": "pubmed",
            "id": ",".join(requested),
            "retmode": "xml",
            **_eutils_identity_params(tool=tool, email=email, api_key=api_key),
        },
        timeout=30,
        session=session,
        max_retries=max_retries,
        backoff_seconds=backoff_seconds,
        sleep=sleep,
    )
    try:
        root = ET.fromstring(response.content)
    except (ET.ParseError, ValueError) as exc:
        raise NCBIRetrievalError("PubMed EFetch returned malformed XML") from exc
    errors = [text for node in root.iter("ERROR") if (text := _xml_text(node))]
    if errors:
        raise NCBIRetrievalError("PubMed EFetch error: " + "; ".join(errors))

    records: dict[str, dict] = {}
    article_nodes = [
        node for node in root.iter()
        if node.tag.rsplit("}", 1)[-1] in {"PubmedArticle", "PubmedBookArticle"}
    ]
    for record in article_nodes:
        pmid_node = record.find("./MedlineCitation/PMID")
        if pmid_node is None:
            pmid_node = record.find("./BookDocument/PMID")
        pmid = _xml_text(pmid_node)
        if not pmid:
            raise NCBIRetrievalError("PubMed EFetch returned a record without a PMID")
        if pmid in records:
            raise NCBIRetrievalError(f"PubMed EFetch returned duplicate PMID {pmid}")

        abstract_parts: list[str] = []
        for node in record.findall(".//Abstract/AbstractText"):
            text = _xml_text(node)
            if not text:
                continue
            label = str(node.attrib.get("Label", "") or "").strip()
            abstract_parts.append(f"{label}: {text}" if label else text)
        publication_types = [
            text for node in record.findall(".//PublicationTypeList/PublicationType")
            if (text := _xml_text(node))
        ]
        mesh_terms = [
            text for node in record.findall(".//MeshHeadingList/MeshHeading/DescriptorName")
            if (text := _xml_text(node))
        ]
        languages = [
            text for node in record.findall(".//Article/Language")
            if (text := _xml_text(node))
        ]
        missing = [
            name for name, values in (
                ("abstract", abstract_parts),
                ("publication_types", publication_types),
                ("mesh_terms", mesh_terms),
            )
            if not values
        ]
        records[pmid] = {
            "pmid": pmid,
            "abstract": "\n".join(abstract_parts),
            "pubtype": publication_types,
            "mesh_terms": mesh_terms,
            "language": languages,
            "metadata_source": "pubmed-efetch",
            "metadata_complete": not missing,
            "metadata_missing": missing,
        }

    returned = set(records)
    expected = set(requested)
    if returned != expected:
        missing = sorted(expected - returned)
        extra = sorted(returned - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise NCBIRetrievalError(
            "PubMed EFetch did not return every requested PMID: " + "; ".join(details)
        )
    return records


def enrich_pmc_summaries(
    result: Mapping[str, object],
    *,
    tool: str = NCBI_TOOL,
    email: str = NCBI_EMAIL,
    api_key: str | None = NCBI_API_KEY,
    session=requests,
    max_retries: int = NCBI_MAX_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Join PMC summaries to PubMed abstracts/types/MeSH through their PMIDs."""
    raw_uids = result.get("uids")
    if not isinstance(raw_uids, list):
        raise NCBIRetrievalError("PMC ESummary enrichment input has no uids")
    pmids_by_uid: dict[str, str] = {}
    for uid in raw_uids:
        item = result.get(uid)
        if not isinstance(item, Mapping):
            raise NCBIRetrievalError(f"PMC ESummary has no metadata object for {uid}")
        article_ids = item.get("articleids", [])
        if isinstance(article_ids, list):
            for identifier in article_ids:
                if (
                    isinstance(identifier, Mapping)
                    and str(identifier.get("idtype", "")).casefold() == "pmid"
                    and str(identifier.get("value", "")).strip()
                ):
                    pmids_by_uid[str(uid)] = str(identifier["value"]).strip()
                    break

    if pmids_by_uid:
        sleep(DELAY)
    pubmed = fetch_pubmed_metadata(
        list(pmids_by_uid.values()),
        tool=tool,
        email=email,
        api_key=api_key,
        session=session,
        max_retries=max_retries,
        sleep=sleep,
    )
    enriched: dict[str, object] = {"uids": list(raw_uids)}
    for raw_uid in raw_uids:
        uid = str(raw_uid)
        item = result[raw_uid]
        assert isinstance(item, Mapping)
        pmid = pmids_by_uid.get(uid)
        if pmid is None:
            existing_abstract = str(item.get("abstract", "") or "")
            existing_types = item.get("pubtype", [])
            existing_mesh = item.get("mesh_terms", item.get("mesh", []))
            missing = ["pmid"]
            if not existing_abstract:
                missing.append("abstract")
            if not existing_types:
                missing.append("publication_types")
            if not existing_mesh:
                missing.append("mesh_terms")
            enriched[uid] = {
                **item,
                "pmid": "",
                "abstract": existing_abstract,
                "pubtype": existing_types,
                "mesh_terms": existing_mesh,
                "metadata_source": "pmc-esummary-no-pmid",
                "metadata_complete": False,
                "metadata_missing": missing,
            }
        else:
            enriched[uid] = {**item, **pubmed[pmid]}
    return enriched


def get_oa_links(pmcid: str) -> tuple:
    """
    Returns (pdf_url, tgz_url) from the PMC OA API.
    Both are converted from ftp:// → https://.
    """
    r = requests.get(PMC_OA_API, params={"id": pmcid}, timeout=15)
    if r.status_code != 200:
        return None, None
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        return None, None

    pdf_url = tgz_url = None
    for link in root.iter("link"):
        fmt  = link.attrib.get("format", "")
        href = ftp_to_https(link.attrib.get("href", ""))
        if fmt == "pdf":
            pdf_url = href
        elif fmt == "tgz":
            tgz_url = href
    return pdf_url, tgz_url


def europepmc_pdf_url(pmcid: str) -> str | None:
    """
    Fallback PDF resolver via Europe PMC.
    Some open-access articles (e.g. late-deposited or project-design papers)
    expose no direct PDF link on the NCBI OA service; Europe PMC indexes the
    same PMC corpus and often carries a resolvable PDF URL. Returns None if no
    PDF-style full-text URL is found.
    """
    try:
        r = requests.get(EPMC_SEARCH, timeout=20, params={
            "query": f"PMCID:{pmcid}",
            "resultType": "core",
            "format": "json",
        })
        r.raise_for_status()
        results = r.json().get("resultList", {}).get("result", [])
        if not results:
            return None
        for entry in results[0].get("fullTextUrlList", {}).get("fullTextUrl", []):
            if entry.get("documentStyle", "").lower() == "pdf":
                return entry.get("url")
    except Exception as e:
        print(f"    Europe PMC lookup error: {e}")
    return None


EPMC_FULLTEXT_XML = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"

def europepmc_fulltext_xml(pmcid: str) -> bytes | None:
    """
    Last-resort full-text retriever: download the JATS XML full text from
    Europe PMC. Used when no PDF is obtainable anywhere (NCBI OA tgz is a
    dead link AND Europe PMC has no resolvable PDF). Saves the complete
    article text + references, which is sufficient for review extraction.
    """
    try:
        r = requests.get(EPMC_FULLTEXT_XML.format(pmcid=pmcid), timeout=30,
                         headers={"User-Agent": "Mozilla/5.0 (academic research)"})
        if r.status_code == 200 and len(r.content) > 5_000:
            return r.content
        print(f"    Europe PMC XML: HTTP {r.status_code}, {len(r.content)} bytes")
    except Exception as e:
        print(f"    Europe PMC XML error: {e}")
    return None


def validate_fulltext(path: Path, is_xml: bool) -> bool:
    """
    Return True only if the saved file is genuine full text.

    - PDF: must start with the %PDF- magic and be non-trivially large.
    - XML: must be a JATS *article* (not an abstract-only record) with a real
      <body>. Conference-supplement records (e.g. Alzheimer's & Dementia,
      J Endocr Soc abstracts) come back as article-type="abstract" with no
      body — those are NOT full papers and are rejected here.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if not is_xml:
        return data[:5] == b"%PDF-" and len(data) > 20_000
    if b'article-type="abstract"' in data[:3000]:
        return False
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return False
    if root.attrib.get("article-type", "") == "abstract":
        return False
    body = root.find(".//body")
    if body is None:
        return False
    text = "".join(body.itertext()).strip()
    return len(text) > 2000


def download_bytes(url: str) -> bytes | None:
    """Download raw bytes from a URL; return None on failure."""
    try:
        r = requests.get(url, timeout=60,
                         headers={"User-Agent": "Mozilla/5.0 (academic research)"})
        if r.status_code == 200 and len(r.content) > 5_000:
            return r.content
        print(f"    Bad response: {r.status_code}, {len(r.content)} bytes")
    except Exception as e:
        print(f"    Download error: {e}")
    return None


SUPP_PATTERN = re.compile(
    r'(suppl?e?m?e?n?t?|supp?\d|_s\d+[\._]|[-_]s\d+\.pdf$'
    r'|app\d+|appendix|fig(ure)?\d*|table\d*)',
    re.IGNORECASE
)

def pdf_from_tgz(data: bytes) -> bytes | None:
    """
    Extract the main article PDF from a tar.gz archive.
    Strategy:
      1. Exclude files whose basenames match supplementary/appendix patterns.
      2. From the remaining candidates, pick the largest (= main article).
      3. If no main candidates exist (archive is supplementary-only), return None.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            pdf_members = [m for m in tf.getmembers() if m.name.endswith(".pdf")]
            if not pdf_members:
                return None

            main_candidates = [m for m in pdf_members
                               if not SUPP_PATTERN.search(os.path.basename(m.name))]

            if not main_candidates:
                names = [os.path.basename(m.name) for m in pdf_members]
                print(f"    Skipped: tarball contains only supplementary/appendix PDFs")
                print(f"    Files: {names}")
                return None

            best = max(main_candidates, key=lambda m: m.size)
            print(f"    Extracted main article: {os.path.basename(best.name)}")
            return tf.extractfile(best).read()
    except Exception as e:
        print(f"    tar.gz extraction error: {e}")
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def _load_log(log_path: Path) -> dict:
    """Load the shared download log with an advisory file lock (fcntl) so parallel
    per-query jobs don't clobber each other. Returns the parsed dict (or {})."""
    import fcntl
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # ponytail: fcntl.LOCK_SH on the log file serializes reads across per-query jobs.
    with open(log_path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_SH)
        fh.seek(0)
        try:
            return json.loads(fh.read() or "{}")
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _save_log(log_path: Path, log: dict) -> None:
    """Write the shared log under an exclusive lock (merges per-query results)."""
    import fcntl
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0); existing = fh.read().strip()
        base = json.loads(existing) if existing else {}
        replace_snapshot = bool(log.get("_replace_candidate_snapshot"))

        # Download-state lists remain unioned for compatibility with concurrent
        # per-query jobs. Canonical state reconciliation is handled separately.
        for k in ("downloaded", "xml_only", "failed", "abstract_only"):
            if k in log:
                merged = []
                seen = set()
                for item in list(base.get(k, [])) + list(log[k] if isinstance(log[k], list) else []):
                    key = json.dumps(item, sort_keys=True)
                    if key not in seen:
                        seen.add(key); merged.append(item)
                base[k] = merged

        # A complete all-query run is an authoritative candidate snapshot.
        # Isolated per-query jobs merge by PMCID/query name under the same lock.
        for key in ("papers", "excluded", "candidates", "pending"):
            if key not in log:
                continue
            if replace_snapshot:
                base[key] = log[key]
                continue
            by_pmcid = {
                item.get("pmcid", json.dumps(item, sort_keys=True)): item
                for item in base.get(key, [])
            }
            for item in log[key]:
                pmcid = item.get("pmcid", json.dumps(item, sort_keys=True))
                previous = by_pmcid.get(pmcid, {})
                combined = {**previous, **item}
                if "query_membership" in previous or "query_membership" in item:
                    memberships = {
                        entry["name"]: entry
                        for entry in previous.get("query_membership", [])
                    }
                    memberships.update({
                        entry["name"]: entry
                        for entry in item.get("query_membership", [])
                    })
                    combined["query_membership"] = list(memberships.values())
                by_pmcid[pmcid] = combined
            base[key] = list(by_pmcid.values())

        if "search_runs" in log:
            runs = {} if replace_snapshot else {
                run["query_name"]: run for run in base.get("search_runs", [])
            }
            runs.update({run["query_name"]: run for run in log["search_runs"]})
            base["search_runs"] = list(runs.values())

        if "query_membership" in log:
            memberships = {} if replace_snapshot else dict(base.get("query_membership", {}))
            for pmcid, entries in log["query_membership"].items():
                by_name = {
                    entry["name"]: entry for entry in memberships.get(pmcid, [])
                }
                by_name.update({entry["name"]: entry for entry in entries})
                memberships[pmcid] = list(by_name.values())
            base["query_membership"] = memberships

        if "screening" in log:
            screening = ({"included": [], "excluded": [], "pending": []}
                         if replace_snapshot else dict(base.get("screening", {})))
            incoming_pmcids = {
                item["pmcid"]
                for values in log["screening"].values()
                for item in values
            }
            for decision in ("included", "excluded", "pending"):
                by_pmcid = {
                    item["pmcid"]: item for item in screening.get(decision, [])
                    if item["pmcid"] not in incoming_pmcids
                }
                by_pmcid.update({
                    item["pmcid"]: item
                    for item in log["screening"].get(decision, [])
                })
                screening[decision] = list(by_pmcid.values())
            base["screening"] = screening

        handled = {
            "downloaded", "xml_only", "failed", "abstract_only", "papers",
            "excluded", "candidates", "pending", "search_runs",
            "query_membership", "screening",
        }
        base.update({
            key: value for key, value in log.items()
            if key not in handled and not key.startswith("_")
        })
        fh.seek(0); fh.truncate(); fh.write(json.dumps(base, indent=2))
        fcntl.flock(fh, fcntl.LOCK_UN)


def _manifest_status_for_screening(
    current: Mapping[str, object] | None,
    decision: str,
) -> str:
    """Apply eligibility while retaining an already-downloaded audit copy."""
    current = current or {}
    status = str(current.get("status", "discovered"))
    if status in {"downloaded", "summarized"} and current.get("path"):
        return status
    if decision == "excluded":
        return "excluded"
    if decision == "pending":
        return "discovered"
    if status in {"failed", "missing", "invalid"}:
        return status
    return "discovered"


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    selector = ap.add_mutually_exclusive_group()
    selector.add_argument("--query-index", type=int, default=None,
                          help="Run only SEARCH_QUERY_SPECS[N] (0-based).")
    selector.add_argument("--query-name", choices=[s.name for s in SEARCH_QUERY_SPECS],
                          help="Run only the named query spec.")
    ap.add_argument("--ncbi-tool", default=NCBI_TOOL,
                    help="Tool name sent with every NCBI E-utilities request.")
    ap.add_argument("--ncbi-email", default=NCBI_EMAIL,
                    help="Contact email sent with every NCBI E-utilities request.")
    ap.add_argument("--ncbi-api-key", default=NCBI_API_KEY,
                    help="Optional NCBI API key (defaults to NCBI_API_KEY).")
    ap.add_argument("--page-size", type=int, default=ESEARCH_PAGE_SIZE,
                    help="ESearch IDs per page; all pages are always retrieved.")
    ap.add_argument("--max-retries", type=int, default=NCBI_MAX_RETRIES,
                    help="Retries after the first attempt for 429/5xx/transport failures.")
    ap.add_argument("--snapshot-date", default=None,
                    help="UTC PMC-live-date cutoff (YYYY-MM-DD); defaults to yesterday.")
    ap.add_argument("--screening-overrides", default=None,
                    help="Durable PMCID-keyed manual review JSON file.")
    args = ap.parse_args(argv)

    if args.query_index is not None:
        if not 0 <= args.query_index < len(SEARCH_QUERY_SPECS):
            ap.error(f"--query-index must be between 0 and {len(SEARCH_QUERY_SPECS) - 1}")
        query_specs = [SEARCH_QUERY_SPECS[args.query_index]]
    elif args.query_name:
        query_specs = [next(s for s in SEARCH_QUERY_SPECS if s.name == args.query_name)]
    else:
        query_specs = SEARCH_QUERY_SPECS
    if not args.ncbi_tool.strip():
        ap.error("--ncbi-tool (or NCBI_TOOL) must not be empty")
    if not args.ncbi_email.strip():
        ap.error("--ncbi-email (or NCBI_EMAIL) is required by NCBI E-utilities")
    if not 0 < args.page_size <= NCBI_ESUMMARY_PAGE_LIMIT:
        ap.error(f"--page-size must be between 1 and {NCBI_ESUMMARY_PAGE_LIMIT}")
    if args.max_retries < 0:
        ap.error("--max-retries must be non-negative")
    try:
        snapshot_cutoff = _coerce_snapshot_date(args.snapshot_date)
    except ValueError as exc:
        ap.error(str(exc))

    OUTPUT_DIR.mkdir(exist_ok=True)
    log_path = OUTPUT_DIR / "download_log.json"
    manifest_path = OUTPUT_DIR / "manifest.json"
    override_path = (
        Path(args.screening_overrides)
        if args.screening_overrides
        else OUTPUT_DIR / "screening_overrides.json"
    )
    try:
        overrides = load_screening_overrides(override_path)
    except ValueError as exc:
        ap.error(str(exc))
    initial_reconciliation = reconcile_manifest(
        manifest_path=manifest_path,
        papers_dir=OUTPUT_DIR,
        download_log_path=log_path,
    )
    log = _load_log(log_path)
    redownload_uids = {pmcid.removeprefix("PMC")
                       for pmcid in initial_reconciliation.redownload}
    valid_existing_pmcs = set(initial_reconciliation.selected_files)
    already_done = {
        str(uid).removeprefix("PMC") for uid in log.get("downloaded", [])
    } - redownload_uids

    # ── 1. Search ──────────────────────────────────────────────────────────
    print("=" * 65)
    print("  SEARCHING PubMed Central (open-access filter)")
    print("=" * 65)
    all_ids: set[str] = set()
    search_runs: list[dict] = []
    query_membership: dict[str, list[dict]] = {}
    for spec in query_specs:
        print(f"\n  Query [{spec.arm}/{spec.name}]: {spec.query!r}")
        result = search_pmc(
            spec.query,
            page_size=args.page_size,
            tool=args.ncbi_tool,
            email=args.ncbi_email,
            api_key=args.ncbi_api_key,
            max_retries=args.max_retries,
            snapshot_date=snapshot_cutoff,
        )
        all_ids.update(result.ids)
        provenance = {
            **asdict(spec),
            "snapshot_date": result.snapshot_date,
            "effective_query": result.effective_query,
        }
        for uid in result.ids:
            query_membership.setdefault(f"PMC{uid}", []).append(provenance)
        search_runs.append({
            "query_name": spec.name,
            "arm": spec.arm,
            "database": "pmc",
            "query": spec.query,
            "effective_query": result.effective_query,
            "snapshot_date": result.snapshot_date,
            "facets": dict(spec.facets),
            "ncbi_count": result.count,
            "retrieved_count": len(result.ids),
            "retrieved_at": result.retrieved_at,
            "pages": result.pages,
            "page_starts": list(result.page_starts),
            "page_counts": list(result.page_counts),
            "query_translation": result.query_translation,
            "shards": [asdict(shard) for shard in result.shards],
        })
        time.sleep(DELAY)
    if len(query_specs) == len(SEARCH_QUERY_SPECS):
        missing_recall = CORE_RECALL_REGRESSION_PMCS - {
            f"PMC{uid}" for uid in all_ids
        }
        if missing_recall:
            raise NCBIRetrievalError(
                "Core search failed recall regression: "
                + ", ".join(sorted(missing_recall))
            )
    old_membership = log.get("query_membership", {})
    all_ids.update(redownload_uids)
    for uid in redownload_uids:
        pmcid = f"PMC{uid}"
        if pmcid not in query_membership and isinstance(old_membership, dict):
            query_membership[pmcid] = list(old_membership.get(pmcid, []))
    log["search_database"] = "pmc"
    log["search_runs"] = search_runs
    log["query_membership"] = query_membership
    log["screening_overrides_file"] = str(override_path)
    log["screening_override_count"] = len(overrides)
    log["_replace_candidate_snapshot"] = len(query_specs) == len(SEARCH_QUERY_SPECS)
    print(f"\nUnique PMC IDs: {len(all_ids)}")

    # ── 2. Metadata ────────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("  FETCHING METADATA")
    print("=" * 65)
    id_list = sorted(all_ids, key=int)
    summaries: dict = {}
    for i in range(0, len(id_list), 20):
        res = fetch_summaries(
            id_list[i:i + 20],
            tool=args.ncbi_tool,
            email=args.ncbi_email,
            api_key=args.ncbi_api_key,
            max_retries=args.max_retries,
        )
        res = enrich_pmc_summaries(
            res,
            tool=args.ncbi_tool,
            email=args.ncbi_email,
            api_key=args.ncbi_api_key,
            max_retries=args.max_retries,
        )
        for uid in res.get("uids", []):
            summaries[uid] = res[uid]
        time.sleep(DELAY)

    # ── 3. Screen candidates ───────────────────────────────────────────────
    print(f"\n{'#':<4} {'PMCID':<14} {'Yr':<5} {'Decision':<10} {'Scope':<25} Title")
    print("-" * 120)
    papers = []
    decisions = []
    candidate_records = []
    screening_by_pmcid = {}
    for i, (uid, item) in enumerate(summaries.items(), 1):
        title   = item.get("title",   "N/A")
        journal = item.get("source",  "N/A")
        year    = item.get("pubdate", "")[:4]
        authors = ", ".join(a.get("name","") for a in item.get("authors", [])[:2])
        membership = query_membership.get(f"PMC{uid}", [])

        pmcid = f"PMC{uid}"
        decision = screen_candidate(
            {**item, "pmcid": pmcid},
            query_names=[entry["name"] for entry in membership],
            query_arms=[entry["arm"] for entry in membership],
            override=overrides.get(pmcid),
        )
        decisions.append(decision)
        screening = decision.to_dict()
        screening_by_pmcid[pmcid] = screening
        candidate_records.append({
            "pmcid": pmcid, "title": title, "journal": journal, "year": year,
            "authors": authors, "query_membership": membership,
            "pmid": item.get("pmid", ""),
            "metadata_source": item.get("metadata_source", ""),
            "metadata_complete": item.get("metadata_complete"),
            "metadata_missing": item.get("metadata_missing", []),
            "screening": screening,
        })
        if decision.decision == "included":
            papers.append((uid, title, journal, year, authors))
        print(f"{i:<4} {pmcid:<14} {year:<5} {decision.decision:<10} "
              f"{decision.scope_classification:<25} {title[:55]}")

    screening_partition = partition_screening(decisions)
    log["screening"] = screening_partition
    log["candidates"] = candidate_records
    log["excluded"] = [
        {**record, "reason": "; ".join(record["screening"]["exclusion_reasons"])}
        for record in candidate_records
        if record["screening"]["decision"] == "excluded"
    ]
    log["pending"] = [
        record for record in candidate_records
        if record["screening"]["decision"] == "pending"
    ]
    print(f"\n  Included : {len(screening_partition['included'])}")
    print(f"  Excluded : {len(screening_partition['excluded'])}")
    print(f"  Pending  : {len(screening_partition['pending'])}")

    # ── 5. Download ────────────────────────────────────────────────────────
    print(f"\n{'=' * 65}")
    print(f"  DOWNLOADING PDFs  →  ./{OUTPUT_DIR}/")
    print("=" * 65 + "\n")

    downloaded = skipped = failed = abstract_only = 0

    def record_success(uid_, pmcid_, out_stem_, data_: bytes, fmt: str, title_: str) -> bool:
        """Write + validate a downloaded file. Returns True if kept as full text."""
        nonlocal downloaded, abstract_only
        is_xml = fmt != "pdf"
        ext = "xml" if is_xml else "pdf"
        path = OUTPUT_DIR / f"{out_stem_}.{ext}"
        path.write_bytes(data_)
        size_kb = path.stat().st_size // 1024
        if not validate_fulltext(path, is_xml=is_xml):
            path.unlink(missing_ok=True)
            print(f"           ✗ {ext.upper()} not full text (abstract-only record) — discarded")
            log.setdefault("abstract_only", []).append({"pmcid": pmcid_, "title": title_})
            abstract_only += 1
            return False
        tag = "" if not is_xml else " (xml)"
        print(f"           ✓{tag} {path.name}  ({size_kb} KB)")
        log.setdefault("downloaded", []).append(uid_)
        if is_xml:
            log.setdefault("xml_only", []).append(pmcid_)
        already_done.add(uid_)
        downloaded += 1
        return True

    for idx, (uid, title, journal, year, authors) in enumerate(papers, 1):
        pmcid    = f"PMC{uid}"
        out_stem = sanitize(f"{year}_{pmcid}_{title}")
        pfx      = f"  [{idx:>2}/{len(papers)}] {pmcid} ({year})"

        if uid in already_done or pmcid in valid_existing_pmcs:
            print(f"{pfx}  [SKIP]")
            skipped += 1
            continue

        print(f"{pfx}  {title[:55]}")

        pdf_url, tgz_url = get_oa_links(pmcid)
        time.sleep(DELAY)

        data: bytes | None = None
        fmt = "pdf"

        # 1. NCBI OA direct PDF.
        if pdf_url:
            print(f"           → PDF: {pdf_url[:78]}")
            candidate = download_bytes(pdf_url)
            if validate_download_payload(candidate, "pdf"):
                data = candidate
            elif candidate is not None:
                print("           → Direct PDF payload invalid; trying fallback")
        # 2. NCBI OA tar.gz → extract main article PDF.
        if data is None and tgz_url:
            print(f"           → TAR: {tgz_url[:78]}")
            raw = download_bytes(tgz_url)
            if raw:
                candidate = pdf_from_tgz(raw)
                if validate_download_payload(candidate, "pdf"):
                    data = candidate
        # 3. Europe PMC PDF (often resolvable where NCBI OA is a dead link).
        if data is None:
            ep_url = europepmc_pdf_url(pmcid)
            if ep_url:
                print(f"           → EuropePMC PDF: {ep_url[:78]}")
                candidate = download_bytes(ep_url)
                if validate_download_payload(candidate, "pdf"):
                    data = candidate
                time.sleep(DELAY)
        # 4. Europe PMC JATS XML full text — last resort to capture full paper.
        if data is None:
            xml_data = europepmc_fulltext_xml(pmcid)
            if validate_download_payload(xml_data, "xml"):
                print(f"           → EuropePMC XML (full text)")
                data = xml_data
                fmt = "xml"

        if data is not None:
            record_success(uid, pmcid, out_stem, data, fmt, title)
        else:
            print(f"           ✗ Could not retrieve full text")
            log.setdefault("failed", []).append({"pmcid": pmcid, "title": title})
            failed += 1

        time.sleep(DELAY)

    # ── 6. Save log ────────────────────────────────────────────────────────
    log["papers"] = [
        {"pmcid": f"PMC{uid}", "title": t, "journal": j, "year": y, "authors": a,
         "pmid": summaries[uid].get("pmid", ""),
         "metadata_source": summaries[uid].get("metadata_source", ""),
         "metadata_complete": summaries[uid].get("metadata_complete"),
         "query_membership": query_membership.get(f"PMC{uid}", []),
         "screening": screening_by_pmcid[f"PMC{uid}"]}
        for uid, t, j, y, a in papers
    ]
    _save_log(log_path, log)

    # The default full run has a single writer, so it can safely reconcile the
    # canonical manifest here. Isolated query workers are reconciled once by
    # the orchestration layer after all workers finish.
    if len(query_specs) == len(SEARCH_QUERY_SPECS):
        reconcile_manifest(
            manifest_path=manifest_path,
            papers_dir=OUTPUT_DIR,
            download_log_path=log_path,
        )
        manifest = PaperManifest(manifest_path)
        for candidate in candidate_records:
            pmcid = candidate["pmcid"]
            current = manifest.get(pmcid)
            decision_name = candidate["screening"]["decision"]
            status = _manifest_status_for_screening(
                current,
                decision_name,
            )
            manifest.upsert(
                pmcid,
                status=status,
                query_provenance=[
                    entry["query"] for entry in candidate["query_membership"]
                ],
                screening=candidate["screening"],
                metadata={
                    **{
                        key: candidate[key]
                        for key in (
                            "title", "journal", "year", "authors", "pmid",
                            "metadata_source", "metadata_complete", "metadata_missing",
                        )
                    },
                    "publication_eligible": decision_name == "included",
                },
            )
        manifest.save()

    print(f"\n{'=' * 50}")
    print(f"  Downloaded    : {downloaded}")
    print(f"  Skipped       : {skipped}  (already on disk)")
    print(f"  Abstract-only : {abstract_only}  (not full text — discarded)")
    print(f"  Failed        : {failed}")
    print(f"  Log           : {log_path}")
    print(f"  Output        : {OUTPUT_DIR.resolve()}")
    print("=" * 50)
    # Search/API reconciliation already raises on incomplete pages.  Full-text
    # retrieval must likewise fail the stage if an included record could not be
    # materialized, so orchestration cannot continue with a partial corpus.
    return 1 if failed or abstract_only else 0


if __name__ == "__main__":
    raise SystemExit(main())
