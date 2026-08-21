#!/usr/bin/env python3
"""Fail closed when retrieval, state, or generated publication artifacts drift.

The checker is deliberately read-only.  It validates the persisted search and
screening audit trail, the PMCID manifest and summary caches, every structured
record, and deterministic rebuilds of the Markdown/site outputs.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import build_results
import build_site
import build_summary
from fetch_pmc_papers import (
    NCBI_ESUMMARY_PAGE_LIMIT,
    NCBI_HISTORY_ID_LIMIT,
    PMC_SNAPSHOT_START,
    SEARCH_QUERY_SPECS,
    validate_pmc_query,
    validate_query_translation,
)
from output_provenance import dominant_model
from paper_manifest import (
    CACHE_SCHEMA_VERSION,
    included_paper_files,
    summary_cache_path,
    validate_fulltext_file,
)
from summarizer.run import PROCESSING_CHECKSUM, PROMPT_CHECKSUM, SCHEMA_CHECKSUM
from summarizer.schema import ManuscriptChecklist, SummaryBatch


PMCID_RE = re.compile(r"PMC(\d+)", re.IGNORECASE)
SHA256_RE = re.compile(r"^sha256:([0-9a-f]{64})$", re.IGNORECASE)
LOCAL_STATUSES = {"downloaded", "summarized"}
KNOWN_STATUSES = {
    "discovered", "excluded", "failed", "missing", "invalid", *LOCAL_STATUSES,
}
EXPECTED_QUERY_RUNS = 20
DECISIONS = {"included", "excluded", "pending"}
SCOPES = {
    "core-exposomics", "operational-mixtures", "vaccine-exposure",
    "adjacent-single-exposure", "out-of-scope", "unclear",
}
POPULATIONS = {"adult", "pediatric", "mixed", "unclear"}


class ArtifactConsistencyError(RuntimeError):
    """Raised with all detected cross-artifact inconsistencies."""


@dataclass(frozen=True)
class ArtifactPaths:
    root: Path
    manifest: Path
    download_log: Path
    papers_dir: Path
    summary_dir: Path
    db: Path
    combined: Path
    results_combined: Path
    paper_summary: Path
    results_summary: Path
    checklist: Path
    site: Path

    @classmethod
    def from_root(cls, root: str | Path = ".") -> "ArtifactPaths":
        root_path = Path(root).resolve()
        papers = root_path / "papers"
        results = root_path / "results"
        return cls(
            root=root_path,
            manifest=papers / "manifest.json",
            download_log=papers / "download_log.json",
            papers_dir=papers,
            summary_dir=papers / "summaries",
            db=papers / "db.json",
            combined=papers / "manuscript_summaries.json",
            results_combined=results / "manuscript_summaries.json",
            paper_summary=root_path / "paper_summary.md",
            results_summary=results / "SUMMARY.md",
            checklist=results / "checklist.md",
            site=root_path / "docs" / "index.html",
        )


def _pmcid(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if text.isdigit():
        return f"PMC{text}"
    match = PMCID_RE.fullmatch(text)
    return f"PMC{match.group(1)}" if match else None


def _pmcid_from_name(path: Path) -> str | None:
    match = PMCID_RE.search(path.name)
    return f"PMC{match.group(1)}" if match else None


def _load_json(path: Path, label: str, errors: list[str]) -> Any | None:
    if not path.is_file():
        errors.append(f"{label}: missing file {path}")
        return None
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        errors.append(f"{label}: invalid JSON ({type(exc).__name__}: {exc})")
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_iso_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return "T" in value and parsed.tzinfo is not None


def _parse_iso_timestamp(value: Any) -> datetime | None:
    if not _is_iso_timestamp(value):
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _resolve_path(raw_path: str, paths: ArtifactPaths, *, relative_to: Path | None = None) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path.resolve()
    candidates = [paths.root / path]
    if relative_to is not None:
        candidates.append(relative_to / path)
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve()


def _canonical_checklist(raw: Any, label: str, errors: list[str]) -> dict | None:
    if not isinstance(raw, dict):
        errors.append(f"{label}: record must be an object")
        return None
    try:
        canonical = ManuscriptChecklist.model_validate(raw).model_dump(mode="json")
    except Exception as exc:
        first = str(exc).splitlines()[0]
        errors.append(f"{label}: fails ManuscriptChecklist validation ({first})")
        return None
    if canonical != raw:
        errors.append(f"{label}: record is not canonical Pydantic output")
    return canonical


def _manifest_state(
    paths: ArtifactPaths, errors: list[str],
) -> tuple[set[str] | None, dict[str, dict], dict[str, Path]]:
    data = _load_json(paths.manifest, "manifest", errors)
    if data is None:
        return None, {}, {}
    if not isinstance(data, dict):
        errors.append("manifest: top level must be an object")
        return None, {}, {}
    for field in ("schema_version", "updated_at", "records"):
        if field not in data:
            errors.append(f"manifest: missing top-level {field!r}")
    if data.get("schema_version") != 1:
        errors.append(f"manifest: unsupported schema_version {data.get('schema_version')!r}")
    if not _is_iso_timestamp(data.get("updated_at")):
        errors.append("manifest: updated_at must be an ISO-8601 timestamp")
    raw_records = data.get("records")
    if not isinstance(raw_records, dict):
        errors.append("manifest: 'records' must be keyed by PMCID")
        return None, {}, {}

    records: dict[str, dict] = {}
    exact_queries = {spec.query for spec in SEARCH_QUERY_SPECS}
    for key, raw_record in raw_records.items():
        pmcid = _pmcid(key)
        if pmcid is None or pmcid != key:
            errors.append(f"manifest: invalid or noncanonical PMCID key {key!r}")
            continue
        if not isinstance(raw_record, dict):
            errors.append(f"manifest[{pmcid}]: record must be an object")
            continue
        record = dict(raw_record)
        records[pmcid] = record
        for field in ("status", "path", "checksum", "query_provenance", "timestamps"):
            if field not in record:
                errors.append(f"manifest[{pmcid}]: missing {field!r}")
        status = record.get("status")
        if status not in KNOWN_STATUSES:
            errors.append(f"manifest[{pmcid}]: unknown status {status!r}")
        queries = record.get("query_provenance")
        if not isinstance(queries, list) or any(not isinstance(q, str) or not q for q in queries):
            errors.append(f"manifest[{pmcid}]: query_provenance must be a string list")
            queries = []
        elif len(queries) != len(set(queries)):
            errors.append(f"manifest[{pmcid}]: duplicate query provenance")
        unknown_queries = set(queries) - exact_queries
        if unknown_queries:
            errors.append(f"manifest[{pmcid}]: provenance contains unknown exact queries")
        timestamps = record.get("timestamps")
        if not isinstance(timestamps, dict):
            errors.append(f"manifest[{pmcid}]: timestamps must be an object")
        else:
            for name, value in timestamps.items():
                if not _is_iso_timestamp(value):
                    errors.append(f"manifest[{pmcid}]: timestamp {name!r} is not timezone-explicit ISO-8601")

        raw_path = record.get("path")
        if not raw_path:
            if status in LOCAL_STATUSES:
                errors.append(f"manifest[{pmcid}]: status {status!r} requires a path")
            if record.get("checksum"):
                errors.append(f"manifest[{pmcid}]: checksum present without a path")
            continue

        source = _resolve_path(str(raw_path), paths, relative_to=paths.manifest.parent)
        if not source.is_file():
            errors.append(f"manifest[{pmcid}]: recorded path does not exist: {raw_path}")
            continue
        if _pmcid_from_name(source) != pmcid:
            errors.append(f"manifest[{pmcid}]: recorded path names a different PMCID")
        if not validate_fulltext_file(source):
            errors.append(f"manifest[{pmcid}]: recorded source is not validated full text")
        checksum = str(record.get("checksum") or "")
        match = SHA256_RE.fullmatch(checksum)
        if match is None:
            errors.append(f"manifest[{pmcid}]: checksum must be sha256:<64 hex chars>")
        elif _sha256(source) != match.group(1).lower():
            errors.append(f"manifest[{pmcid}]: checksum does not match {raw_path}")

    # Publication is the explicit included-study projection, not every valid
    # physical file retained for excluded/pending-record auditability.
    try:
        sources = included_paper_files(paths.manifest, paths.papers_dir, validate=True)
    except Exception as exc:
        errors.append(f"manifest publication projection: {type(exc).__name__}: {exc}")
        return set(), records, {}
    local_ids = set(sources)
    for pmcid in sorted(local_ids):
        record = records.get(pmcid, {})
        if record.get("status") != "summarized":
            errors.append(f"manifest[{pmcid}]: published local paper must have status 'summarized'")
        if not record.get("query_provenance"):
            errors.append(f"manifest[{pmcid}]: published local paper needs exact-query provenance")
    return local_ids, records, sources


def _valid_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _snapshot_query(query: str, lower: date, upper: date) -> str:
    return (
        f'({query}) AND ("{lower:%Y/%m/%d}"[PMC Live Date] : '
        f'"{upper:%Y/%m/%d}"[PMC Live Date])'
    )


def _audit_search_run(
    raw: Mapping[str, Any], spec: Any, label: str, errors: list[str],
) -> str | None:
    """Audit the complete frozen-history/date-shard proof for one query."""
    if raw.get("database") != "pmc":
        errors.append(f"{label}: database must be 'pmc'")
    try:
        validate_pmc_query(str(raw.get("query") or ""))
    except Exception as exc:
        errors.append(f"{label}: query contract failed ({exc})")

    snapshot_raw = raw.get("snapshot_date")
    snapshot = _parse_iso_date(snapshot_raw)
    if snapshot is None:
        errors.append(f"{label}: snapshot_date must be YYYY-MM-DD")
    retrieved_at = _parse_iso_timestamp(raw.get("retrieved_at"))
    if retrieved_at is None:
        errors.append(f"{label}: retrieved_at must be a timezone-explicit ISO-8601 timestamp")
    elif snapshot is not None and snapshot >= retrieved_at.astimezone(timezone.utc).date():
        errors.append(f"{label}: snapshot_date must be the last completed UTC day or earlier")

    count = raw.get("ncbi_count")
    retrieved = raw.get("retrieved_count")
    pages = raw.get("pages")
    starts = raw.get("page_starts")
    counts = raw.get("page_counts")
    if not _valid_int(count) or not _valid_int(retrieved) or not _valid_int(pages):
        errors.append(f"{label}: counts/pages must be nonnegative integers")
    if _valid_int(count) and _valid_int(retrieved) and count != retrieved:
        errors.append(f"{label}: retrieved_count does not reconcile with ncbi_count")
    if not isinstance(starts, list) or any(not _valid_int(v) for v in starts):
        errors.append(f"{label}: page_starts must be nonnegative integers")
        starts = []
    if not isinstance(counts, list) or any(not _valid_int(v) for v in counts):
        errors.append(f"{label}: page_counts must be nonnegative integers")
        counts = []
    if _valid_int(pages) and (pages != len(starts) or pages != len(counts)):
        errors.append(f"{label}: pages does not match page_starts/page_counts lengths")
    if _valid_int(retrieved) and sum(counts) != retrieved:
        errors.append(f"{label}: page totals do not match retrieved_count")

    effective_query = raw.get("effective_query")
    if not isinstance(effective_query, str) or not effective_query:
        errors.append(f"{label}: effective_query is missing")
    translation = raw.get("query_translation")
    if not isinstance(translation, str) or not translation.strip():
        errors.append(f"{label}: query_translation is missing")
    else:
        try:
            validate_query_translation(spec.query, translation)
        except Exception as exc:
            errors.append(f"{label}: query translation contract failed ({exc})")

    shards = raw.get("shards")
    if not isinstance(shards, list) or not shards:
        errors.append(f"{label}: shards must contain the frozen search tree")
        return str(snapshot_raw) if snapshot is not None else None

    ranges: dict[tuple[date, date], Mapping[str, Any]] = {}
    leaves: list[Mapping[str, Any]] = []
    leaf_starts: list[int] = []
    leaf_counts: list[int] = []
    for index, shard in enumerate(shards):
        shard_label = f"{label} shards[{index}]"
        if not isinstance(shard, Mapping):
            errors.append(f"{shard_label}: shard must be an object")
            continue
        lower = _parse_iso_date(shard.get("date_from"))
        upper = _parse_iso_date(shard.get("date_to"))
        if lower is None or upper is None or lower > upper:
            errors.append(f"{shard_label}: invalid date range")
            continue
        if (lower, upper) in ranges:
            errors.append(f"{shard_label}: duplicate date range")
        ranges[(lower, upper)] = shard
        expected_query = _snapshot_query(spec.query, lower, upper)
        if shard.get("query") != expected_query:
            errors.append(f"{shard_label}: query does not match its exact date shard")
        shard_translation = shard.get("query_translation")
        if not isinstance(shard_translation, str) or not shard_translation.strip():
            errors.append(f"{shard_label}: query_translation is missing")
        else:
            try:
                validate_query_translation(expected_query, shard_translation)
            except Exception as exc:
                errors.append(f"{shard_label}: query translation contract failed ({exc})")
        if not _is_iso_timestamp(shard.get("retrieved_at")):
            errors.append(f"{shard_label}: retrieved_at must be timezone-explicit ISO-8601")
        shard_count = shard.get("count")
        shard_pages = shard.get("pages")
        shard_starts = shard.get("page_starts")
        shard_counts = shard.get("page_counts")
        is_leaf = shard.get("is_leaf")
        if not _valid_int(shard_count) or not _valid_int(shard_pages):
            errors.append(f"{shard_label}: count/pages must be nonnegative integers")
        if not isinstance(shard_starts, list) or any(not _valid_int(v) for v in shard_starts):
            errors.append(f"{shard_label}: page_starts must be nonnegative integers")
            shard_starts = []
        if not isinstance(shard_counts, list) or any(not _valid_int(v) for v in shard_counts):
            errors.append(f"{shard_label}: page_counts must be nonnegative integers")
            shard_counts = []
        if is_leaf is True:
            leaves.append(shard)
            leaf_starts.extend(shard_starts)
            leaf_counts.extend(shard_counts)
            if _valid_int(shard_count) and shard_count > NCBI_HISTORY_ID_LIMIT:
                errors.append(f"{shard_label}: leaf exceeds the NCBI history ID limit")
            if _valid_int(shard_pages) and (
                shard_pages != len(shard_starts) or shard_pages != len(shard_counts)
            ):
                errors.append(f"{shard_label}: pages does not match its page arrays")
            if _valid_int(shard_count) and sum(shard_counts) != shard_count:
                errors.append(f"{shard_label}: page totals do not match shard count")
            if any(value <= 0 or value > NCBI_ESUMMARY_PAGE_LIMIT for value in shard_counts):
                errors.append(f"{shard_label}: page count is outside the ESummary limit")
            expected_starts = []
            offset = 0
            for value in shard_counts:
                expected_starts.append(offset)
                offset += value
            if shard_starts != expected_starts:
                errors.append(f"{shard_label}: page_starts do not prove contiguous retrieval")
        elif is_leaf is False:
            if _valid_int(shard_count) and shard_count <= NCBI_HISTORY_ID_LIMIT:
                errors.append(f"{shard_label}: non-leaf did not require partitioning")
            if shard_pages != 0 or shard_starts or shard_counts:
                errors.append(f"{shard_label}: non-leaf cannot claim retrieved pages")
        else:
            errors.append(f"{shard_label}: is_leaf must be a boolean")

    if snapshot is not None:
        root_key = (PMC_SNAPSHOT_START, snapshot)
        root = ranges.get(root_key)
        if root is None:
            errors.append(f"{label}: shard tree does not contain the full snapshot root")
        else:
            if root.get("query") != effective_query:
                errors.append(f"{label}: effective_query does not match the root shard")
            if _valid_int(count) and root.get("count") != count:
                errors.append(f"{label}: root shard count does not match ncbi_count")
            if root.get("query_translation") != translation:
                errors.append(f"{label}: root and run query translations differ")

    for (lower, upper), shard in ranges.items():
        if shard.get("is_leaf") is not False:
            continue
        midpoint = lower + timedelta(days=(upper - lower).days // 2)
        left = ranges.get((lower, midpoint))
        right = ranges.get((midpoint + timedelta(days=1), upper))
        if left is None or right is None:
            errors.append(f"{label}: non-leaf shard is missing an exact child range")
        elif all(_valid_int(item.get("count")) for item in (shard, left, right)) and (
            left.get("count") + right.get("count") != shard.get("count")
        ):
            errors.append(f"{label}: child shard counts do not reconcile with their parent")

    ordered_leaves = sorted(
        (
            (_parse_iso_date(shard.get("date_from")), _parse_iso_date(shard.get("date_to")), shard)
            for shard in leaves
        ),
        key=lambda item: item[0] or date.max,
    )
    if ordered_leaves and snapshot is not None:
        if ordered_leaves[0][0] != PMC_SNAPSHOT_START or ordered_leaves[-1][1] != snapshot:
            errors.append(f"{label}: leaf shards do not cover the complete snapshot interval")
        for previous, current in zip(ordered_leaves, ordered_leaves[1:]):
            if previous[1] is None or current[0] != previous[1] + timedelta(days=1):
                errors.append(f"{label}: leaf shard date ranges overlap or leave a gap")
                break
    if _valid_int(count) and sum(
        shard.get("count") for shard in leaves if _valid_int(shard.get("count"))
    ) != count:
        errors.append(f"{label}: leaf shard counts do not reconcile with ncbi_count")
    if starts != leaf_starts or counts != leaf_counts:
        errors.append(f"{label}: run page arrays do not equal the leaf-shard page arrays")
    return str(snapshot_raw) if snapshot is not None else None


def _audit_screening(
    pmcid: str,
    raw: Any,
    membership_names: set[str],
    errors: list[str],
) -> str | None:
    label = f"download log screening[{pmcid}]"
    if not isinstance(raw, dict):
        errors.append(f"{label}: decision must be an object")
        return None
    if raw.get("pmcid") != pmcid:
        errors.append(f"{label}: PMCID does not match candidate")
    decision = raw.get("decision")
    if decision not in DECISIONS:
        errors.append(f"{label}: invalid decision {decision!r}")
        return None
    if raw.get("scope_classification") not in SCOPES:
        errors.append(f"{label}: invalid scope classification")
    if raw.get("population_facet") not in POPULATIONS:
        errors.append(f"{label}: invalid population facet")
    for field in ("human_study", "primary_study", "ehr_facet"):
        value = raw.get(field)
        if value is not True and value is not False and value is not None:
            errors.append(f"{label}: {field} must be true, false, or null")
    provenance = raw.get("query_provenance")
    if not isinstance(provenance, list) or any(not isinstance(v, str) or not v for v in provenance):
        errors.append(f"{label}: query_provenance must be a string list")
        provenance = []
    if set(provenance) != membership_names:
        errors.append(f"{label}: query provenance does not match query membership")
    evidence = raw.get("eligibility_evidence")
    reasons = raw.get("exclusion_reasons")
    if not isinstance(evidence, list) or any(
        not isinstance(v, str) or not v.strip() for v in evidence
    ):
        errors.append(f"{label}: eligibility_evidence must be a nonblank string list")
        evidence = []
    if not isinstance(reasons, list) or any(
        not isinstance(v, str) or not v.strip() for v in reasons
    ):
        errors.append(f"{label}: exclusion_reasons must be a string list")
        reasons = []
    if decision == "included":
        if raw.get("human_study") is not True or raw.get("primary_study") is not True:
            errors.append(f"{label}: included study needs affirmative human and primary evidence")
        if raw.get("scope_classification") in {"unclear", "out-of-scope"}:
            errors.append(f"{label}: included study needs an in-scope classification")
        if not evidence:
            errors.append(f"{label}: included study needs eligibility evidence")
        if reasons:
            errors.append(f"{label}: included study cannot have exclusion reasons")
    elif decision == "excluded" and not reasons:
        errors.append(f"{label}: excluded study needs an exclusion reason")
    method = raw.get("screening_method")
    if method not in {"automated-metadata-v1", "manual-override"}:
        errors.append(f"{label}: unsupported screening_method {method!r}")
    if method == "manual-override":
        if not str(raw.get("reviewer") or "").strip():
            errors.append(f"{label}: manual override needs a reviewer")
        if not _is_iso_timestamp(raw.get("reviewed_at")):
            errors.append(f"{label}: manual override needs an ISO reviewed_at timestamp")
    return str(decision)


def _download_audit(
    paths: ArtifactPaths, errors: list[str],
) -> tuple[set[str] | None, dict[str, dict], set[str], dict[str, list[dict]]]:
    data = _load_json(paths.download_log, "download log", errors)
    if data is None:
        return None, {}, set(), {}
    if not isinstance(data, dict):
        errors.append("download log: top level must be an object")
        return None, {}, set(), {}

    downloaded = data.get("downloaded")
    if not isinstance(downloaded, list):
        errors.append("download log: 'downloaded' must be a list")
        downloaded = []
    download_ids: set[str] = set()
    for value in downloaded:
        pmcid = _pmcid(value)
        if pmcid is None:
            errors.append(f"download log: invalid downloaded PMCID {value!r}")
        else:
            download_ids.add(pmcid)
    if len(download_ids) != len(downloaded):
        errors.append(f"download log: {len(downloaded)} entries but {len(download_ids)} unique valid PMCIDs")

    if data.get("search_database") != "pmc":
        errors.append("download log: search_database must be 'pmc'")
    if len(SEARCH_QUERY_SPECS) != EXPECTED_QUERY_RUNS:
        errors.append(
            f"executable query registry: expected {EXPECTED_QUERY_RUNS}, found {len(SEARCH_QUERY_SPECS)}"
        )
    expected = {spec.name: spec for spec in SEARCH_QUERY_SPECS}
    runs = data.get("search_runs")
    if not isinstance(runs, list):
        errors.append("download log: search_runs must be a list")
        runs = []
    if len(runs) != EXPECTED_QUERY_RUNS:
        errors.append(f"download log: expected {EXPECTED_QUERY_RUNS} exact query runs, found {len(runs)}")
    run_by_name: dict[str, dict] = {}
    snapshots: set[str] = set()
    for index, raw in enumerate(runs):
        label = f"download log search_runs[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label}: run must be an object")
            continue
        name = raw.get("query_name")
        if name in run_by_name:
            errors.append(f"{label}: duplicate query_name {name!r}")
        if not isinstance(name, str) or name not in expected:
            errors.append(f"{label}: unknown query_name {name!r}")
            continue
        run_by_name[name] = raw
        spec = expected[name]
        for field, expected_value in (
            ("arm", spec.arm), ("query", spec.query), ("facets", dict(spec.facets)),
        ):
            if raw.get(field) != expected_value:
                errors.append(f"{label}: {field} does not match executable registry")
        if snapshot := _audit_search_run(raw, spec, label, errors):
            snapshots.add(snapshot)
    if len(snapshots) != 1:
        errors.append(f"download log: all exact query runs must share one snapshot_date, found {sorted(snapshots)}")
    if set(run_by_name) != set(expected):
        missing = sorted(set(expected) - set(run_by_name))
        errors.append(f"download log: exact query run names do not match registry; missing={missing}")

    membership_raw = data.get("query_membership")
    if not isinstance(membership_raw, dict):
        errors.append("download log: query_membership must be keyed by PMCID")
        membership_raw = {}
    membership: dict[str, list[dict]] = {}
    member_counts = {name: 0 for name in expected}
    for raw_pmcid, raw_entries in membership_raw.items():
        pmcid = _pmcid(raw_pmcid)
        if pmcid is None or pmcid != raw_pmcid:
            errors.append(f"download log: invalid query_membership PMCID {raw_pmcid!r}")
            continue
        if not isinstance(raw_entries, list) or not raw_entries:
            errors.append(f"download log query_membership[{pmcid}]: must be a nonempty list")
            continue
        entries: list[dict] = []
        seen_names: set[str] = set()
        for entry in raw_entries:
            if not isinstance(entry, dict):
                errors.append(f"download log query_membership[{pmcid}]: entry must be an object")
                continue
            name = entry.get("name")
            if not isinstance(name, str) or name not in expected:
                errors.append(f"download log query_membership[{pmcid}]: unknown query name {name!r}")
                continue
            if name in seen_names:
                errors.append(f"download log query_membership[{pmcid}]: duplicate query {name}")
                continue
            seen_names.add(name)
            spec = expected[name]
            if entry.get("arm") != spec.arm or entry.get("query") != spec.query or entry.get("facets") != dict(spec.facets):
                errors.append(f"download log query_membership[{pmcid}]: {name} differs from registry")
            run = run_by_name.get(name, {})
            if entry.get("snapshot_date") != run.get("snapshot_date"):
                errors.append(f"download log query_membership[{pmcid}]: {name} snapshot differs from run")
            if entry.get("effective_query") != run.get("effective_query"):
                errors.append(f"download log query_membership[{pmcid}]: {name} effective query differs from run")
            entries.append(entry)
            member_counts[name] += 1
        membership[pmcid] = entries
    for name, run in run_by_name.items():
        if _valid_int(run.get("retrieved_count")) and member_counts[name] != run["retrieved_count"]:
            errors.append(
                f"download log: membership count for {name} is {member_counts[name]}, "
                f"expected {run['retrieved_count']}"
            )

    candidates_raw = data.get("candidates")
    if not isinstance(candidates_raw, list):
        errors.append("download log: candidates must be a list")
        candidates_raw = []
    candidates: dict[str, dict] = {}
    decisions: dict[str, str] = {}
    for index, raw in enumerate(candidates_raw):
        if not isinstance(raw, dict):
            errors.append(f"download log candidates[{index}]: candidate must be an object")
            continue
        pmcid = _pmcid(raw.get("pmcid"))
        if pmcid is None or raw.get("pmcid") != pmcid:
            errors.append(f"download log candidates[{index}]: invalid PMCID")
            continue
        if pmcid in candidates:
            errors.append(f"download log: duplicate candidate {pmcid}")
        candidates[pmcid] = raw
        if not str(raw.get("title") or "").strip():
            errors.append(f"download log candidates[{pmcid}]: title is missing")
        if not str(raw.get("metadata_source") or "").strip():
            errors.append(f"download log candidates[{pmcid}]: metadata_source is missing")
        if raw.get("metadata_complete") is not True and raw.get("metadata_complete") is not False:
            errors.append(f"download log candidates[{pmcid}]: metadata_complete must be boolean")
        metadata_missing = raw.get("metadata_missing")
        if not isinstance(metadata_missing, list) or any(
            not isinstance(value, str) or not value for value in metadata_missing
        ):
            errors.append(f"download log candidates[{pmcid}]: metadata_missing must be a string list")
        elif raw.get("metadata_complete") is True and metadata_missing:
            errors.append(f"download log candidates[{pmcid}]: complete metadata cannot list missing fields")
        candidate_entries = raw.get("query_membership")
        if candidate_entries != membership.get(pmcid):
            errors.append(f"download log candidates[{pmcid}]: query membership differs from registry map")
        names = {entry.get("name") for entry in membership.get(pmcid, [])}
        decision = _audit_screening(pmcid, raw.get("screening"), names, errors)
        if isinstance(raw.get("screening"), dict) and raw["screening"].get("title") != raw.get("title"):
            errors.append(f"download log screening[{pmcid}]: title differs from candidate metadata")
        if decision is not None:
            decisions[pmcid] = decision
    if set(candidates) != set(membership):
        errors.append(
            f"download log: candidate and query-membership PMCID sets differ "
            f"({_set_diff(set(membership), set(candidates))})"
        )

    included_ids = {pmcid for pmcid, decision in decisions.items() if decision == "included"}
    screening = data.get("screening")
    if not isinstance(screening, dict):
        errors.append("download log: screening partitions must be an object")
        screening = {}
    for decision in sorted(DECISIONS):
        values = screening.get(decision)
        if not isinstance(values, list):
            errors.append(f"download log: screening.{decision} must be a list")
            continue
        partition: dict[str, dict] = {}
        for value in values:
            if not isinstance(value, dict) or (pmcid := _pmcid(value.get("pmcid"))) is None:
                errors.append(f"download log: screening.{decision} contains an invalid record")
                continue
            partition[pmcid] = value
        expected_partition = {p for p, d in decisions.items() if d == decision}
        if set(partition) != expected_partition:
            errors.append(f"download log: screening.{decision} partition does not match candidates")
        for pmcid, value in partition.items():
            if candidates.get(pmcid, {}).get("screening") != value:
                errors.append(f"download log: screening.{decision}[{pmcid}] payload differs from candidate")

    def _ids_from_records(field: str) -> set[str]:
        values = data.get(field)
        if not isinstance(values, list):
            errors.append(f"download log: {field} must be a list")
            return set()
        result: set[str] = set()
        for value in values:
            pmcid = _pmcid(value.get("pmcid") if isinstance(value, dict) else value)
            if pmcid is None:
                errors.append(f"download log: {field} contains an invalid PMCID")
            else:
                result.add(pmcid)
        return result

    paper_ids = _ids_from_records("papers")
    if paper_ids != included_ids:
        errors.append("download log: papers must contain exactly the included candidates")
    if _ids_from_records("excluded") != {p for p, d in decisions.items() if d == "excluded"}:
        errors.append("download log: excluded list does not match screening decisions")
    if _ids_from_records("pending") != {p for p, d in decisions.items() if d == "pending"}:
        errors.append("download log: pending list does not match screening decisions")
    if not download_ids <= included_ids:
        errors.append("download log: downloaded full text includes non-included candidates")

    override_count = data.get("screening_override_count")
    if not _valid_int(override_count):
        errors.append("download log: screening_override_count must be a nonnegative integer")
        override_count = 0
    override_file = data.get("screening_overrides_file")
    if not isinstance(override_file, str) or not override_file:
        errors.append("download log: screening_overrides_file is missing")
        overrides: dict[str, Any] = {}
    else:
        override_path = _resolve_path(override_file, paths)
        if override_path.is_file():
            loaded_overrides = _load_json(override_path, "screening overrides", errors)
            overrides = loaded_overrides if isinstance(loaded_overrides, dict) else {}
            if loaded_overrides is not None and not isinstance(loaded_overrides, dict):
                errors.append("screening overrides: top level must be keyed by PMCID")
        else:
            overrides = {}
            if override_count:
                errors.append(f"screening overrides: missing file {override_path}")
    if len(overrides) != override_count:
        errors.append(
            f"screening overrides: file has {len(overrides)} records, log declares {override_count}"
        )
    manual_ids = {
        pmcid for pmcid, candidate in candidates.items()
        if candidate.get("screening", {}).get("screening_method") == "manual-override"
    }
    if not manual_ids <= set(overrides):
        errors.append("screening overrides: manual decisions are not durable in the override file")
    return download_ids, candidates, included_ids, membership


def _source_file_ids(paths: ArtifactPaths, errors: list[str]) -> set[str] | None:
    try:
        return set(included_paper_files(paths.manifest, paths.papers_dir, validate=True))
    except Exception as exc:
        errors.append(f"source files: included projection failed ({type(exc).__name__}: {exc})")
        return None


def _records_from_rows(
    rows: Any, label: str, errors: list[str], *, pydantic_records: bool = True,
) -> tuple[set[str] | None, dict[str, dict]]:
    if not isinstance(rows, list):
        errors.append(f"{label}: summaries must be a list")
        return None, {}
    ids: set[str] = set()
    records: dict[str, dict] = {}
    for index, row in enumerate(rows):
        canonical = (
            _canonical_checklist(row, f"{label} record {index}", errors)
            if pydantic_records else row if isinstance(row, dict) else None
        )
        if canonical is None:
            if not pydantic_records and not isinstance(row, dict):
                errors.append(f"{label}: record {index} must be an object")
            continue
        pmcid = _pmcid(canonical.get("pmcid"))
        if pmcid is None or canonical.get("pmcid") != pmcid:
            errors.append(f"{label}: record {index} has invalid PMCID")
            continue
        if pmcid in ids:
            errors.append(f"{label}: duplicate record {pmcid}")
        ids.add(pmcid)
        records[pmcid] = canonical
    return ids, records


def _per_paper_records(
    paths: ArtifactPaths, errors: list[str], *, allowed_ids: set[str] | None = None,
) -> tuple[set[str] | None, dict[str, dict]]:
    if not paths.summary_dir.is_dir():
        errors.append(f"per-paper summaries: missing directory {paths.summary_dir}")
        return None, {}
    ids: set[str] = set()
    records: dict[str, dict] = {}
    for summary in sorted(paths.summary_dir.glob("*.json")):
        file_pmcid = _pmcid_from_name(summary)
        if allowed_ids is not None and file_pmcid not in allowed_ids:
            # Excluded/pending summaries may be retained for audit, but they
            # are not members of the explicit publication projection.
            continue
        data = _load_json(summary, f"per-paper summary {summary.name}", errors)
        canonical = _canonical_checklist(data, f"per-paper summary {summary.name}", errors)
        if canonical is None:
            continue
        pmcid = _pmcid(canonical.get("pmcid"))
        if pmcid is None:
            continue
        if file_pmcid != pmcid:
            errors.append(f"per-paper summary {summary.name}: filename PMCID {file_pmcid} != {pmcid}")
        if pmcid in ids:
            errors.append(f"per-paper summaries: duplicate record {pmcid}")
        ids.add(pmcid)
        records[pmcid] = canonical
    return ids, records


def _db_records(paths: ArtifactPaths, errors: list[str]) -> tuple[set[str] | None, dict[str, dict]]:
    data = _load_json(paths.db, "TinyDB", errors)
    if data is None:
        return None, {}
    table = data.get("_default") if isinstance(data, dict) else None
    if isinstance(table, dict):
        rows = list(table.values())
    elif isinstance(table, list):
        rows = table
    else:
        errors.append("TinyDB: '_default' table must be an object or list")
        return None, {}
    return _records_from_rows(rows, "TinyDB", errors)


def _combined_records(
    path: Path, label: str, errors: list[str],
) -> tuple[set[str] | None, dict[str, dict], dict | None]:
    data = _load_json(path, label, errors)
    if data is None:
        return None, {}, None
    if not isinstance(data, dict):
        errors.append(f"{label}: top level must be an object")
        return None, {}, None
    try:
        canonical_batch = SummaryBatch.model_validate(data).model_dump(mode="json")
    except Exception as exc:
        errors.append(f"{label}: fails SummaryBatch validation ({str(exc).splitlines()[0]})")
        canonical_batch = None
    if canonical_batch is not None and canonical_batch != data:
        errors.append(f"{label}: batch is not canonical Pydantic output")
    rows = data.get("summaries")
    ids, records = _records_from_rows(rows, label, errors)
    if isinstance(rows, list) and data.get("n") != len(rows):
        errors.append(f"{label}: n={data.get('n')!r}, summaries={len(rows)}")
    if isinstance(rows, list):
        expected_model = dominant_model(rows)
        if str(data.get("model") or "") != expected_model:
            errors.append(f"{label}: batch model {data.get('model')!r} != record-derived {expected_model!r}")
    return ids, records, data


def _audit_summary_caches(
    paths: ArtifactPaths,
    manifest_ids: set[str] | None,
    manifest_records: Mapping[str, dict],
    sources: Mapping[str, Path],
    summary_records: Mapping[str, dict],
    errors: list[str],
) -> None:
    if manifest_ids is None:
        return
    for pmcid in sorted(manifest_ids):
        label = f"summary cache[{pmcid}]"
        summary_path = paths.summary_dir / f"{pmcid}.json"
        cache_path = summary_cache_path(summary_path)
        data = _load_json(cache_path, label, errors)
        if not isinstance(data, dict):
            if data is not None:
                errors.append(f"{label}: metadata must be an object")
            continue
        source = sources.get(pmcid)
        record = summary_records.get(pmcid)
        manifest = manifest_records.get(pmcid, {})
        expected = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "pmcid": pmcid,
            "source_checksum": manifest.get("checksum"),
            "prompt_checksum": PROMPT_CHECKSUM,
            "schema_checksum": SCHEMA_CHECKSUM,
            "model": record.get("model") if record else None,
            "title": record.get("title") if record else None,
            "year": record.get("year") if record else None,
            "processing_checksum": PROCESSING_CHECKSUM,
        }
        for field, value in expected.items():
            if data.get(field) != value:
                errors.append(f"{label}: {field} does not match current source/summary/code")
        if not str(data.get("model") or "").strip():
            errors.append(f"{label}: model provenance is blank")
        if not _is_iso_timestamp(data.get("created_at")):
            errors.append(f"{label}: created_at must be an ISO-8601 timestamp")
        if data.get("summarization_mode") not in {
            "single", "chunked", "single-with-chunked-recovery",
        }:
            errors.append(f"{label}: summarization_mode is not a recognized execution mode")
        raw_source_path = data.get("source_path")
        if not isinstance(raw_source_path, str) or not raw_source_path:
            errors.append(f"{label}: source_path is missing")
        elif source is not None and _resolve_path(raw_source_path, paths) != source.resolve():
            errors.append(f"{label}: source_path does not match manifest")
        if record is not None and source is not None:
            if record.get("source_format") != source.suffix.lower().lstrip("."):
                errors.append(f"{label}: checklist source_format does not match source file")


def _markdown_ids(
    path: Path, label: str, errors: list[str], *, require_ids: bool,
) -> tuple[set[str] | None, int | None]:
    if not path.is_file():
        errors.append(f"{label}: missing file {path}")
        return None, None
    text = path.read_text()
    marker = re.search(r"<!--\s*artifact-count:\s*(\d+)\s*-->", text)
    if marker is None:
        errors.append(f"{label}: missing artifact-count marker")
        declared = None
    else:
        declared = int(marker.group(1))
    ids = {f"PMC{number}" for number in PMCID_RE.findall(text)} if require_ids else set()
    if require_ids and declared is not None and declared != len(ids):
        errors.append(f"{label}: declares {declared}, contains {len(ids)} unique PMCIDs")
    return ids if require_ids else None, declared


def _site_ids(paths: ArtifactPaths, errors: list[str]) -> tuple[set[str] | None, int | None]:
    if not paths.site.is_file():
        errors.append(f"site: missing file {paths.site}")
        return None, None
    text = paths.site.read_text()
    count_match = re.search(r'<meta\s+name="exposome-record-count"\s+content="(\d+)"\s*/?>', text)
    if count_match is None:
        errors.append("site: missing exposome-record-count metadata")
        declared = None
    else:
        declared = int(count_match.group(1))
    data_match = re.search(r'<script\s+id="data"\s+type="application/json">(.*?)</script>', text, re.DOTALL)
    if data_match is None:
        errors.append("site: missing embedded JSON inventory")
        return None, declared
    try:
        rows = json.loads(data_match.group(1))
    except Exception as exc:
        errors.append(f"site: invalid embedded JSON ({type(exc).__name__}: {exc})")
        return None, declared
    ids, _ = _records_from_rows(rows, "site", errors, pydantic_records=False)
    if ids is not None and declared is not None and declared != len(ids):
        errors.append(f"site: declares {declared}, embeds {len(ids)} unique PMCIDs")
    return ids, declared


def _set_diff(reference: set[str], actual: set[str]) -> str:
    missing = sorted(reference - actual)
    extra = sorted(actual - reference)
    parts = []
    if missing:
        parts.append(f"missing {missing[:8]}{' ...' if len(missing) > 8 else ''}")
    if extra:
        parts.append(f"extra {extra[:8]}{' ...' if len(extra) > 8 else ''}")
    return "; ".join(parts)


def _compare_records(
    reference: Mapping[str, dict], actual: Mapping[str, dict], label: str, errors: list[str],
) -> None:
    if set(reference) != set(actual):
        return
    changed = sorted(pmcid for pmcid in reference if reference[pmcid] != actual[pmcid])
    if changed:
        errors.append(f"{label}: record payloads differ for {changed[:8]}{' ...' if len(changed) > 8 else ''}")


def _compare_generated_outputs(paths: ArtifactPaths, errors: list[str]) -> None:
    required = (paths.download_log, paths.combined)
    if not all(path.is_file() for path in required):
        return
    try:
        with tempfile.TemporaryDirectory(prefix="exposome-artifacts-") as raw_tmp:
            tmp = Path(raw_tmp)
            expected_summary = tmp / "paper_summary.md"
            expected_results = tmp / "results"
            expected_site = tmp / "docs" / "index.html"
            with contextlib.redirect_stdout(io.StringIO()):
                build_summary.main(log_path=paths.download_log, out_path=expected_summary)
                build_results.main(combined=paths.combined, out_dir=expected_results)
                build_site.main(
                    combined=expected_results / "manuscript_summaries.json",
                    out=expected_site,
                )
            comparisons = (
                (paths.paper_summary, expected_summary, "paper_summary.md"),
                (paths.results_summary, expected_results / "SUMMARY.md", "results/SUMMARY.md"),
                (paths.checklist, expected_results / "checklist.md", "results/checklist.md"),
                (paths.results_combined, expected_results / "manuscript_summaries.json", "results combined JSON"),
                (paths.site, expected_site, "site"),
            )
            for actual, expected, label in comparisons:
                if actual.is_file() and actual.read_bytes() != expected.read_bytes():
                    errors.append(f"{label}: content differs from a deterministic rebuild")
    except Exception as exc:
        errors.append(f"generated outputs: deterministic rebuild failed ({type(exc).__name__}: {exc})")


def check_artifacts(
    root: str | Path = ".", *, paths: ArtifactPaths | None = None,
) -> dict[str, int]:
    """Validate all standard artifacts and return their reconciled counts."""
    artifact_paths = paths or ArtifactPaths.from_root(root)
    errors: list[str] = []

    manifest_ids, manifest_records, sources = _manifest_state(artifact_paths, errors)
    download_ids, candidates, included_ids, membership = _download_audit(artifact_paths, errors)
    source_ids = _source_file_ids(artifact_paths, errors)
    summary_ids, summary_records = _per_paper_records(
        artifact_paths, errors, allowed_ids=manifest_ids,
    )
    db_ids, db_records = _db_records(artifact_paths, errors)
    combined_ids, combined_records, combined_data = _combined_records(
        artifact_paths.combined, "combined JSON", errors
    )
    results_ids, results_records, results_data = _combined_records(
        artifact_paths.results_combined, "results combined JSON", errors
    )
    paper_ids, paper_count = _markdown_ids(
        artifact_paths.paper_summary, "paper_summary.md", errors, require_ids=True
    )
    _, result_count = _markdown_ids(
        artifact_paths.results_summary, "results/SUMMARY.md", errors, require_ids=False
    )
    checklist_ids, checklist_count = _markdown_ids(
        artifact_paths.checklist, "results/checklist.md", errors, require_ids=True
    )
    site_ids, site_count = _site_ids(artifact_paths, errors)

    id_sets = {
        "manifest": manifest_ids,
        "download_log": download_ids,
        "source_files": source_ids,
        "per_paper_summaries": summary_ids,
        "db": db_ids,
        "combined": combined_ids,
        "results_combined": results_ids,
        "paper_summary": paper_ids,
        "checklist": checklist_ids,
        "site": site_ids,
    }
    if manifest_ids is not None:
        for label, ids in id_sets.items():
            if label != "manifest" and ids is not None and ids != manifest_ids:
                errors.append(
                    f"{label}: {len(ids)} PMCIDs != manifest {len(manifest_ids)} "
                    f"({_set_diff(manifest_ids, ids)})"
                )
        for label, declared in (
            ("paper_summary.md", paper_count),
            ("results/SUMMARY.md", result_count),
            ("results/checklist.md", checklist_count),
            ("site", site_count),
        ):
            if declared is not None and declared != len(manifest_ids):
                errors.append(f"{label}: declared count {declared} != manifest {len(manifest_ids)}")
        if not manifest_ids <= included_ids:
            errors.append("manifest: published local papers are not all included screening decisions")

    for pmcid, candidate in candidates.items():
        manifest = manifest_records.get(pmcid)
        if manifest is None:
            errors.append(f"manifest: missing candidate record {pmcid}")
            continue
        if manifest.get("screening") != candidate.get("screening"):
            errors.append(f"manifest[{pmcid}]: screening payload differs from download log")
        expected_eligible = candidate.get("screening", {}).get("decision") == "included"
        if manifest.get("publication_eligible") is not expected_eligible:
            errors.append(
                f"manifest[{pmcid}]: publication_eligible does not match screening decision"
            )
        for field in ("title", "journal", "year", "authors"):
            if manifest.get(field) != candidate.get(field):
                errors.append(f"manifest[{pmcid}]: {field} differs from candidate metadata")
        expected_queries = {entry.get("query") for entry in membership.get(pmcid, [])}
        if set(manifest.get("query_provenance") or []) != expected_queries:
            errors.append(f"manifest[{pmcid}]: exact-query provenance differs from download log")
        if pmcid in (manifest_ids or set()) and (summary := summary_records.get(pmcid)):
            for field in ("title", "year"):
                if summary.get(field) != candidate.get(field):
                    errors.append(f"per-paper summary[{pmcid}]: {field} differs from retrieval metadata")

    _audit_summary_caches(
        artifact_paths, manifest_ids, manifest_records, sources, summary_records, errors
    )
    _compare_records(summary_records, db_records, "TinyDB", errors)
    _compare_records(summary_records, combined_records, "combined JSON", errors)
    _compare_records(summary_records, results_records, "results combined JSON", errors)
    if combined_data is not None and results_data is not None and combined_data != results_data:
        errors.append("results combined JSON: payload differs from papers combined JSON")
    _compare_generated_outputs(artifact_paths, errors)

    if errors:
        raise ArtifactConsistencyError(
            "artifact consistency check failed:\n- " + "\n- ".join(errors)
        )
    return {label: len(ids) for label, ids in id_sets.items() if ids is not None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    args = parser.parse_args(argv)
    try:
        counts = check_artifacts(args.root)
    except ArtifactConsistencyError as exc:
        print(exc)
        return 1
    reconciled = counts.get("manifest", 0)
    print(f"✓ artifact consistency: {reconciled} PMCIDs across {len(counts)} artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
