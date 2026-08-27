"""PMCID-keyed state and cache-integrity helpers for the paper pipeline.

``papers/manifest.json`` is the durable source of truth for local full text.
The legacy download log is still accepted as an input during migration, but
reconciliation projects its append-only lists onto one record per PMCID and
checks every claimed local file against the filesystem.

The module deliberately does not delete duplicate or invalid downloads.  It
selects one validated representation for downstream work and reports the
others so an operator can inspect or quarantine them separately.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
DEFAULT_MANIFEST_PATH = Path("papers/manifest.json")
DEFAULT_PAPERS_DIR = Path("papers")
DEFAULT_DOWNLOAD_LOG = DEFAULT_PAPERS_DIR / "download_log.json"

LOCAL_STATUSES = frozenset({"downloaded", "summarized"})
KNOWN_STATUSES = frozenset({
    "discovered", "excluded", "failed", "missing", "invalid",
    *LOCAL_STATUSES,
})

_PMCID_RE = re.compile(r"PMC(\d+)", re.IGNORECASE)
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_UNSET = object()


def utc_now() -> str:
    """Return a compact, timezone-explicit UTC timestamp."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_pmcid(value: str | int) -> str:
    """Normalize a bare numeric identifier or ``PMC...`` string."""
    text = str(value).strip().upper()
    if text.startswith("PMC"):
        text = text[3:]
    if not text.isdigit():
        raise ValueError(f"invalid PMCID: {value!r}")
    return f"PMC{text}"


def pmcid_from_path(path: str | Path) -> str | None:
    """Extract a normalized PMCID from a filename, or return ``None``."""
    match = _PMCID_RE.search(Path(path).stem)
    return normalize_pmcid(match.group(1)) if match else None


def sha256_bytes(data: bytes) -> str:
    """Return a self-describing SHA-256 checksum."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Stream a file into SHA-256 without loading a paper into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def atomic_write_text(path: str | Path, text: str) -> None:
    """Publish text with ``os.replace`` so readers never see a partial file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                   dir=str(target.parent))
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def atomic_write_json(path: str | Path, data: Mapping[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def validate_download_payload(
    data: bytes | bytearray | None,
    source_format: str,
    *,
    min_pdf_bytes: int = 20_000,
    min_xml_body_chars: int = 2_000,
) -> bool:
    """Return whether bytes are a plausible full-text PDF or JATS article.

    HTTP 200 alone is insufficient: direct-PDF endpoints sometimes return an
    HTML error page.  Callers should continue to their next retrieval method
    whenever this function returns ``False``.
    """
    if not isinstance(data, (bytes, bytearray)):
        return False
    payload = bytes(data)
    fmt = source_format.strip().lower().lstrip(".")
    if "pdf" in fmt:
        return len(payload) > min_pdf_bytes and payload.startswith(b"%PDF-")
    if "xml" not in fmt:
        return False
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, ValueError):
        return False
    if _local_name(root.tag) != "article":
        return False
    if root.attrib.get("article-type", "").strip().lower() == "abstract":
        return False
    bodies = [node for node in root.iter() if _local_name(node.tag) == "body"]
    if not bodies:
        return False
    body_text = "".join("".join(node.itertext()) for node in bodies).strip()
    return len(body_text) > min_xml_body_chars


def validate_fulltext_file(path: str | Path) -> bool:
    """Validate a local PDF/XML with the same rules as a response payload."""
    paper = Path(path)
    try:
        if paper.suffix.lower() == ".pdf":
            if paper.stat().st_size <= 20_000:
                return False
            with paper.open("rb") as handle:
                return handle.read(5) == b"%PDF-"
        return validate_download_payload(paper.read_bytes(), paper.suffix)
    except OSError:
        return False


@dataclass(frozen=True)
class PayloadCandidate:
    """One ordered retrieval candidate for fallback selection."""
    source: str
    source_format: str
    data: bytes | None


def select_first_valid_payload(
    candidates: Iterable[PayloadCandidate],
) -> PayloadCandidate | None:
    """Select the first valid candidate, naturally falling through bad PDFs."""
    for candidate in candidates:
        if validate_download_payload(candidate.data, candidate.source_format):
            return candidate
    return None


@dataclass(frozen=True)
class DeduplicationResult:
    """Validated, deterministic one-file-per-PMCID selection."""
    selected: dict[str, Path]
    duplicates: dict[str, tuple[Path, ...]]
    invalid_files: tuple[Path, ...]


def deduplicate_paper_files(
    paths: Iterable[str | Path],
    *,
    validate: bool = False,
) -> DeduplicationResult:
    """Choose one PDF/XML per PMCID, preferring a valid PDF then valid XML."""
    grouped: dict[str, list[Path]] = {}
    invalid: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.suffix.lower() not in {".pdf", ".xml"}:
            continue
        pmcid = pmcid_from_path(path)
        if pmcid is None:
            continue
        grouped.setdefault(pmcid, []).append(path)

    selected: dict[str, Path] = {}
    duplicates: dict[str, tuple[Path, ...]] = {}
    for pmcid, candidates in grouped.items():
        ordered = sorted(
            candidates,
            key=lambda path: (0 if path.suffix.lower() == ".pdf" else 1, str(path)),
        )
        valid: list[Path] = []
        for path in ordered:
            if not validate or validate_fulltext_file(path):
                valid.append(path)
            else:
                invalid.append(path)
        if not valid:
            continue
        selected[pmcid] = valid[0]
        if len(valid) > 1:
            duplicates[pmcid] = tuple(valid[1:])

    return DeduplicationResult(
        selected=dict(sorted(selected.items())),
        duplicates=dict(sorted(duplicates.items())),
        invalid_files=tuple(sorted(invalid, key=str)),
    )


def canonical_manifest_path(
    path: str | Path,
    *,
    papers_dir: str | Path = DEFAULT_PAPERS_DIR,
) -> str:
    """Return a portable repository-relative path for a local paper.

    The manifest is committed and may be produced by either the fetch CLI
    (which uses relative paths) or Dagster (which uses absolute paths).  Always
    storing the path relative to the repository root prevents runner-specific
    absolute paths from leaking into the durable state.
    """
    papers_root = Path(papers_dir).resolve()
    repo_root = papers_root.parent
    source = Path(path)
    resolved = source.resolve() if not source.is_absolute() else source.resolve()
    try:
        relative = resolved.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"paper path is outside repository root: {path}") from exc
    return relative.as_posix()


def resolve_manifest_path(
    raw_path: str | Path,
    *,
    papers_dir: str | Path = DEFAULT_PAPERS_DIR,
) -> Path:
    """Resolve a canonical manifest path while accepting legacy path shapes."""
    path = Path(raw_path)
    if path.is_absolute():
        return path
    papers_root = Path(papers_dir).resolve()
    repo_root = papers_root.parent
    repo_candidate = repo_root / path
    if repo_candidate.exists() or path.parts[:1] == (papers_root.name,):
        return repo_candidate
    return papers_root / path


def screening_decision(record: Mapping[str, Any]) -> str:
    """Return the normalized persisted eligibility decision, if any."""
    screening = record.get("screening")
    if not isinstance(screening, Mapping):
        return ""
    decision = str(screening.get("decision") or "").strip().lower()
    return decision if decision in {"included", "excluded", "pending"} else ""


def record_is_included(record: Mapping[str, Any]) -> bool:
    """Only an explicit screening inclusion is publishable downstream."""
    return screening_decision(record) == "included"


class PaperManifest:
    """In-memory editor for the canonical PMCID-keyed manifest."""

    def __init__(self, path: str | Path = DEFAULT_MANIFEST_PATH) -> None:
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": SCHEMA_VERSION, "updated_at": None, "records": {}}
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"unreadable manifest {self.path}: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("records"), dict):
            raise ValueError(f"invalid manifest structure: {self.path}")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported manifest schema_version {raw.get('schema_version')!r}"
            )
        normalized: dict[str, dict[str, Any]] = {}
        for key, record in raw["records"].items():
            if not isinstance(record, dict):
                raise ValueError(f"manifest record {key!r} must be an object")
            pmcid = normalize_pmcid(key)
            normalized[pmcid] = self._normalize_record(pmcid, record)
        raw["records"] = normalized
        raw.setdefault("updated_at", None)
        return raw

    @staticmethod
    def _normalize_record(pmcid: str, record: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(record)
        status = str(out.get("status", "discovered"))
        if status not in KNOWN_STATUSES:
            raise ValueError(f"unknown manifest status for {pmcid}: {status!r}")
        path = out.get("path")
        checksum = out.get("checksum")
        if checksum is not None and not _SHA256_RE.fullmatch(str(checksum)):
            raise ValueError(f"invalid checksum for {pmcid}: {checksum!r}")
        queries = out.get("query_provenance", [])
        if not isinstance(queries, list):
            raise ValueError(f"query_provenance for {pmcid} must be a list")
        timestamps = out.get("timestamps", {})
        if not isinstance(timestamps, dict):
            raise ValueError(f"timestamps for {pmcid} must be an object")
        out.update({
            "status": status,
            "path": str(path) if path is not None else None,
            "checksum": str(checksum) if checksum is not None else None,
            "query_provenance": list(dict.fromkeys(
                str(query) for query in queries if str(query).strip()
            )),
            "timestamps": dict(timestamps),
        })
        return out

    @property
    def records(self) -> dict[str, dict[str, Any]]:
        return self.data["records"]

    def get(self, pmcid: str | int) -> dict[str, Any] | None:
        record = self.records.get(normalize_pmcid(pmcid))
        return dict(record) if record is not None else None

    def upsert(
        self,
        pmcid: str | int,
        *,
        status: str,
        path: str | Path | None | object = _UNSET,
        checksum: str | None | object = _UNSET,
        query_provenance: Sequence[str] = (),
        screening: Mapping[str, Any] | None | object = _UNSET,
        timestamp: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert/update one record while preserving provenance and first-seen time."""
        key = normalize_pmcid(pmcid)
        if status not in KNOWN_STATUSES:
            raise ValueError(f"unknown manifest status: {status!r}")
        now = timestamp or utc_now()
        old = self.records.get(key, {})
        record = dict(old)
        if metadata:
            record.update(dict(metadata))
        old_queries = old.get("query_provenance", [])
        queries = list(dict.fromkeys([
            *(str(query) for query in old_queries if str(query).strip()),
            *(str(query) for query in query_provenance if str(query).strip()),
        ]))
        timestamps = dict(old.get("timestamps", {}))
        timestamps.setdefault("first_seen_at", now)
        timestamps["updated_at"] = now
        if status in LOCAL_STATUSES:
            timestamps.setdefault("downloaded_at", now)
            timestamps["validated_at"] = now
        elif status == "missing":
            timestamps["missing_at"] = now
        elif status == "invalid":
            timestamps["invalid_at"] = now

        record.update({
            "status": status,
            "path": old.get("path") if path is _UNSET else (
                str(path) if path is not None else None
            ),
            "checksum": old.get("checksum") if checksum is _UNSET else checksum,
            "query_provenance": queries,
            "timestamps": timestamps,
        })
        if screening is not _UNSET:
            if screening is None:
                record.pop("screening", None)
            else:
                record["screening"] = dict(screening)
        normalized = self._normalize_record(key, record)
        if normalized["status"] in LOCAL_STATUSES:
            if not normalized["path"] or not normalized["checksum"]:
                raise ValueError(f"local manifest record {key} needs path and checksum")
        elif normalized["path"] is not None or normalized["checksum"] is not None:
            raise ValueError(f"non-local manifest record {key} cannot claim a local file")
        self.records[key] = normalized
        self.data["updated_at"] = now
        return dict(normalized)

    def save(self, *, timestamp: str | None = None) -> None:
        self.data["updated_at"] = timestamp or self.data.get("updated_at") or utc_now()
        self.data["records"] = dict(sorted(self.records.items()))
        atomic_write_json(self.path, self.data)


def _included_paper_files(
    manifest: PaperManifest,
    papers_dir: str | Path = DEFAULT_PAPERS_DIR,
    *,
    validate: bool = True,
    statuses: frozenset[str] = LOCAL_STATUSES,
) -> dict[str, Path]:
    selected: dict[str, Path] = {}
    for pmcid, record in sorted(manifest.records.items()):
        if not record_is_included(record):
            continue
        if record.get("status") not in statuses or not record.get("path"):
            continue
        source = resolve_manifest_path(record["path"], papers_dir=papers_dir)
        if not source.is_file():
            raise ValueError(f"included manifest source is missing for {pmcid}: {record['path']}")
        if validate and not validate_fulltext_file(source):
            raise ValueError(f"included manifest source is invalid for {pmcid}: {record['path']}")
        expected_checksum = str(record.get("checksum") or "")
        if expected_checksum and sha256_file(source) != expected_checksum:
            raise ValueError(f"included manifest checksum mismatch for {pmcid}: {record['path']}")
        path_pmcid = pmcid_from_path(source)
        if path_pmcid != pmcid:
            raise ValueError(
                f"included manifest path identity mismatch for {pmcid}: {record['path']}"
            )
        selected[pmcid] = source
    return selected


def included_paper_files(
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
    papers_dir: str | Path = DEFAULT_PAPERS_DIR,
    *,
    validate: bool = True,
) -> dict[str, Path]:
    """Return included local sources eligible for summarization.

    Physical files for excluded, pending, or as-yet-unreviewed records may stay
    on disk for auditability, but they are deliberately absent from this map.
    Both ``downloaded`` and ``summarized`` sources are returned so stale or
    missing summaries can be refreshed.
    """
    source = Path(manifest_path)
    if not source.is_file():
        raise FileNotFoundError(
            f"paper manifest is missing: {source}; run download/reconciliation first"
        )
    return _included_paper_files(
        PaperManifest(source), papers_dir, validate=validate, statuses=LOCAL_STATUSES,
    )


def summarized_paper_files(
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
    papers_dir: str | Path = DEFAULT_PAPERS_DIR,
    *,
    validate: bool = True,
) -> dict[str, Path]:
    """Return only included sources with a current summarized manifest status."""
    source = Path(manifest_path)
    if not source.is_file():
        raise FileNotFoundError(
            f"paper manifest is missing: {source}; run download/reconciliation first"
        )
    return _included_paper_files(
        PaperManifest(source), papers_dir,
        validate=validate, statuses=frozenset({"summarized"}),
    )


def publication_ready_paper_files(
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
    papers_dir: str | Path = DEFAULT_PAPERS_DIR,
    *,
    validate: bool = True,
) -> dict[str, Path]:
    """Return the complete included projection, or fail if any study is unready.

    Unlike ``summarized_paper_files``, this is a publication gate: every record
    explicitly screened as included must have current ``summarized`` state and
    a validated local source.  It therefore cannot silently turn an incomplete
    summarization run into a smaller published evidence set.
    """
    source = Path(manifest_path)
    if not source.is_file():
        raise FileNotFoundError(
            f"paper manifest is missing: {source}; run download/reconciliation first"
        )
    manifest = PaperManifest(source)
    included_ids = {
        pmcid for pmcid, record in manifest.records.items()
        if record_is_included(record)
    }
    ready = _included_paper_files(
        manifest, papers_dir, validate=validate,
        statuses=frozenset({"summarized"}),
    )
    missing = sorted(included_ids - set(ready))
    if missing:
        states = ", ".join(
            f"{pmcid}={manifest.records[pmcid].get('status', 'unknown')}"
            for pmcid in missing
        )
        raise ValueError(
            f"included studies are not publication-ready: {states}; run make summarize"
        )
    return ready


def discover_included_papers(
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
    papers_dir: str | Path = DEFAULT_PAPERS_DIR,
    *,
    validate: bool = True,
) -> list[Path]:
    """Return every included full text, failing when retrieval is incomplete."""
    selected = included_paper_files(manifest_path, papers_dir, validate=validate)
    manifest = PaperManifest(manifest_path)
    included_ids = {
        pmcid for pmcid, record in manifest.records.items()
        if record_is_included(record)
    }
    missing = sorted(included_ids - set(selected))
    if missing:
        states = ", ".join(
            f"{pmcid}={manifest.records[pmcid].get('status', 'unknown')}"
            for pmcid in missing
        )
        raise ValueError(
            f"included studies have no validated local full text: {states}; "
            "rerun make download"
        )
    return [selected[pmcid] for pmcid in sorted(selected)]


def discover_publication_ready_papers(
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
    papers_dir: str | Path = DEFAULT_PAPERS_DIR,
    *,
    validate: bool = True,
) -> list[Path]:
    """Return all included sources only when the publication set is complete."""
    selected = publication_ready_paper_files(
        manifest_path, papers_dir, validate=validate,
    )
    return [selected[pmcid] for pmcid in sorted(selected)]


def _iter_log_entries(value: Any) -> Iterable[Mapping[str, Any] | str | int]:
    if isinstance(value, list):
        yield from value
    elif value is not None:
        yield value


def _entry_pmcid(entry: Mapping[str, Any] | str | int) -> str | None:
    try:
        if isinstance(entry, Mapping):
            value = entry.get("pmcid", entry.get("uid", entry.get("id")))
            return normalize_pmcid(value) if value is not None else None
        return normalize_pmcid(entry)
    except ValueError:
        return None


def _search_runs(log: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    runs = log.get("search_runs", [])
    if isinstance(runs, Mapping):
        return [value for value in runs.values() if isinstance(value, Mapping)]
    if isinstance(runs, list):
        return [value for value in runs if isinstance(value, Mapping)]
    return []


def _exact_query(value: Any, runs: Sequence[Mapping[str, Any]]) -> list[str]:
    """Resolve query strings from current and transitional log shapes."""
    if value is None:
        return []
    if isinstance(value, str):
        # Some transitional logs persisted stable query names rather than the
        # exact string.  Resolve them through search_runs when possible.
        for run in runs:
            if value in {run.get("query_name"), run.get("name")}:
                resolved = _exact_query(run, runs)
                if resolved:
                    return resolved
        return [value] if value.strip() else []
    if isinstance(value, Mapping):
        for key in ("query", "exact_query", "term"):
            if isinstance(value.get(key), str) and value[key].strip():
                return [value[key]]
        return []
    if isinstance(value, int):
        for run in runs:
            if value in {run.get("query_index"), run.get("index")}:
                return _exact_query(run, runs)
        if 0 <= value < len(runs):
            return _exact_query(runs[value], runs)
        return []
    if isinstance(value, Sequence):
        out: list[str] = []
        for item in value:
            out.extend(_exact_query(item, runs))
        return list(dict.fromkeys(out))
    return []


def _queries_for_entry(
    entry: Mapping[str, Any], runs: Sequence[Mapping[str, Any]],
) -> list[str]:
    out: list[str] = []
    for key in ("query_provenance", "queries", "query_membership"):
        out.extend(_exact_query(entry.get(key), runs))
    return list(dict.fromkeys(out))


@dataclass(frozen=True)
class ReconciliationResult:
    """Actions and anomalies found while reconciling persisted state."""
    # All validated physical files, including retained excluded/pending records.
    selected_files: dict[str, Path]
    # The only files authorized to feed summaries and publication artifacts.
    included_files: dict[str, Path]
    redownload: tuple[str, ...]
    registered: tuple[str, ...]
    duplicates: dict[str, tuple[Path, ...]]
    invalid_files: tuple[Path, ...]


def _state_screening(
    pmcid: str,
    *,
    decision: str = "pending",
    title: str = "",
    queries: Sequence[str] = (),
    reason: str = "record has not received an explicit eligibility decision",
) -> dict[str, Any]:
    """Build a complete fail-closed screening record for migrated state."""
    return {
        "pmcid": pmcid,
        "title": title,
        "decision": decision,
        "scope_classification": "out-of-scope" if decision == "excluded" else "unclear",
        "population_facet": "unclear",
        "human_study": None,
        "primary_study": None,
        "ehr_facet": None,
        "query_provenance": list(queries),
        "eligibility_evidence": [],
        "exclusion_reasons": [reason] if reason else [],
        "screening_method": "state-reconciliation",
        "reviewer": "",
        "reviewed_at": "",
    }


def reconcile_manifest(
    *,
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
    papers_dir: str | Path = DEFAULT_PAPERS_DIR,
    download_log_path: str | Path = DEFAULT_DOWNLOAD_LOG,
    timestamp: str | None = None,
) -> ReconciliationResult:
    """Reconcile manifest, legacy log, and validated files on disk.

    Logged/manifest downloads whose files vanished are marked ``missing`` and
    returned in ``redownload``.  Valid unlogged files are registered.  A bad
    PDF does not mask a valid XML for the same PMCID.
    """
    now = timestamp or utc_now()
    manifest = PaperManifest(manifest_path)
    log_path = Path(download_log_path)
    if log_path.exists():
        try:
            raw_log = json.loads(log_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"unreadable download log {log_path}: {exc}") from exc
        if not isinstance(raw_log, dict):
            raise ValueError(f"download log {log_path} must contain an object")
        log: Mapping[str, Any] = raw_log
    else:
        log = {}

    runs = _search_runs(log)
    queries_by_pmcid: dict[str, list[str]] = {}
    metadata_by_pmcid: dict[str, dict[str, Any]] = {}
    screening_by_pmcid: dict[str, Mapping[str, Any]] = {}
    discovered: set[str] = set()

    # Read all candidate partitions.  In particular, pending and excluded
    # records must retain their decisions even when a full text remains on disk.
    for log_key in ("candidates", "papers", "pending", "excluded", "failed", "abstract_only"):
        for entry in _iter_log_entries(log.get(log_key, [])):
            pmcid = _entry_pmcid(entry)
            if pmcid is None:
                continue
            discovered.add(pmcid)
            if isinstance(entry, Mapping):
                queries_by_pmcid.setdefault(pmcid, []).extend(
                    _queries_for_entry(entry, runs)
                )
                metadata = metadata_by_pmcid.setdefault(pmcid, {})
                for key in ("title", "journal", "year", "authors", "population", "reason"):
                    if key in entry:
                        metadata[key] = entry[key]
                if isinstance(entry.get("screening"), Mapping):
                    screening_by_pmcid[pmcid] = entry["screening"]
                elif log_key in {"excluded", "pending"}:
                    decision = "excluded" if log_key == "excluded" else "pending"
                    screening_by_pmcid[pmcid] = _state_screening(
                        pmcid,
                        decision=decision,
                        title=str(entry.get("title") or ""),
                        queries=queries_by_pmcid.get(pmcid, []),
                        reason=str(entry.get("reason") or (
                            "legacy exclusion record" if decision == "excluded"
                            else "record is pending eligibility review"
                        )),
                    )

    # Also accept the normalized top-level screening partitions.  This makes a
    # migrated log sufficient even if its candidate records are sparse.
    screening_partitions = log.get("screening")
    if isinstance(screening_partitions, Mapping):
        for decision in ("included", "excluded", "pending"):
            for entry in _iter_log_entries(screening_partitions.get(decision, [])):
                pmcid = _entry_pmcid(entry)
                if pmcid is None or not isinstance(entry, Mapping):
                    continue
                discovered.add(pmcid)
                normalized_screening = dict(entry)
                normalized_screening.setdefault("decision", decision)
                screening_by_pmcid[pmcid] = normalized_screening

    membership = log.get("query_membership")
    if isinstance(membership, Mapping):
        for raw_pmcid, values in membership.items():
            try:
                pmcid = normalize_pmcid(raw_pmcid)
            except ValueError:
                continue
            discovered.add(pmcid)
            queries_by_pmcid.setdefault(pmcid, []).extend(_exact_query(values, runs))

    # Search-run membership is accepted when a run records its retrieved IDs.
    for run in runs:
        query = _exact_query(run, runs)
        ids = run.get(
            "pmcids", run.get("ids", run.get("retrieved_ids", run.get("id_list", [])))
        )
        for entry in _iter_log_entries(ids):
            pmcid = _entry_pmcid(entry)
            if pmcid is not None:
                discovered.add(pmcid)
                queries_by_pmcid.setdefault(pmcid, []).extend(query)

    def log_set(key: str) -> set[str]:
        return {
            pmcid for entry in _iter_log_entries(log.get(key, []))
            if (pmcid := _entry_pmcid(entry)) is not None
        }

    logged_downloaded = log_set("downloaded") | log_set("xml_only")
    logged_failed = log_set("failed")
    logged_invalid = log_set("abstract_only")
    logged_excluded = log_set("excluded")

    papers_root = Path(papers_dir)
    deduped = deduplicate_paper_files(
        [*papers_root.glob("*.pdf"), *papers_root.glob("*.xml")], validate=True,
    )
    invalid_by_pmcid = {
        pmcid for path in deduped.invalid_files if (pmcid := pmcid_from_path(path))
    }
    previous_records = {key: dict(value) for key, value in manifest.records.items()}
    expected_local = logged_downloaded | {
        pmcid for pmcid, record in previous_records.items()
        if record.get("status") in (LOCAL_STATUSES | {"missing", "invalid"})
        or record.get("path")
    }
    universe = (
        set(previous_records) | discovered | logged_downloaded | logged_failed |
        logged_invalid | logged_excluded | set(deduped.selected) | invalid_by_pmcid
    )

    registered: list[str] = []
    redownload: list[str] = []
    for pmcid in sorted(universe):
        selected = deduped.selected.get(pmcid)
        previous = previous_records.get(pmcid, {})
        queries = list(dict.fromkeys(queries_by_pmcid.get(pmcid, [])))
        metadata = metadata_by_pmcid.get(pmcid, {})
        screening = screening_by_pmcid.get(pmcid, previous.get("screening"))
        if not isinstance(screening, Mapping) or screening_decision({"screening": screening}) == "":
            # Unlogged/unreviewed local files are retained for audit, but a
            # fail-closed pending decision prevents silent publication.
            screening = _state_screening(
                pmcid,
                title=str(metadata.get("title") or previous.get("title") or ""),
                queries=queries,
            )
        decision = screening_decision({"screening": screening})
        if selected is not None:
            checksum = sha256_file(selected)
            canonical_path = canonical_manifest_path(selected, papers_dir=papers_root)
            unchanged_summary = (
                previous.get("status") == "summarized"
                and previous.get("checksum") == checksum
                and previous.get("path") == canonical_path
            )
            status = "summarized" if unchanged_summary else "downloaded"
            manifest.upsert(
                pmcid, status=status, path=canonical_path, checksum=checksum,
                query_provenance=queries, screening=screening,
                timestamp=now, metadata=metadata,
            )
            if pmcid not in previous_records and pmcid not in logged_downloaded:
                registered.append(pmcid)
            continue

        if decision == "excluded":
            status = "excluded"
        elif pmcid in invalid_by_pmcid or pmcid in logged_invalid:
            status = "invalid"
            if decision == "included":
                redownload.append(pmcid)
        elif pmcid in expected_local:
            status = "missing"
            if decision == "included":
                redownload.append(pmcid)
        elif pmcid in logged_failed:
            status = "failed"
        elif pmcid in logged_excluded:
            status = "excluded"
        else:
            status = "discovered"
        manifest.upsert(
            pmcid, status=status, path=None, checksum=None,
            query_provenance=queries, screening=screening,
            timestamp=now, metadata=metadata,
        )

    included_files = {
        pmcid: path for pmcid, path in deduped.selected.items()
        if record_is_included(manifest.records[pmcid])
    }
    included_record_ids = {
        pmcid for pmcid, record in manifest.records.items()
        if record_is_included(record)
    }

    # Publication-facing legacy lists are an included-only projection.  Keep a
    # separate physical inventory so retained excluded/pending files remain
    # visible to fetch/resume logic without becoming publishable records.
    reconciled_log = dict(log)
    reconciled_log["downloaded"] = sorted(included_files)
    reconciled_log["xml_only"] = sorted(
        pmcid for pmcid, path in included_files.items()
        if path.suffix.lower() == ".xml"
    )
    reconciled_log["local_files"] = sorted(deduped.selected)
    reconciled_log["local_xml_only"] = sorted(
        pmcid for pmcid, path in deduped.selected.items()
        if path.suffix.lower() == ".xml"
    )
    reconciled_log["papers"] = [
        entry for entry in _iter_log_entries(log.get("papers", []))
        if isinstance(entry, Mapping)
        and (pmcid := _entry_pmcid(entry)) is not None
        and pmcid in included_record_ids
    ]
    reconciled_log["reconciled_at"] = now
    atomic_write_json(log_path, reconciled_log)
    manifest.save(timestamp=now)
    return ReconciliationResult(
        selected_files=deduped.selected,
        included_files=included_files,
        redownload=tuple(sorted(set(redownload))),
        registered=tuple(registered),
        duplicates=deduped.duplicates,
        invalid_files=deduped.invalid_files,
    )


CACHE_SCHEMA_VERSION = 2


def summary_cache_path(summary_path: str | Path) -> Path:
    """Keep cache metadata out of the directory's ``*.json`` summary glob."""
    summary = Path(summary_path)
    return summary.parent / ".cache" / summary.name


def build_summary_cache_metadata(
    *,
    pmcid: str | int,
    source_path: str | Path,
    model: str,
    prompt_checksum: str,
    schema_checksum: str,
    timestamp: str | None = None,
    source_checksum: str | None = None,
    source_identity_path: str | Path | None = None,
    title: str = "",
    year: str = "",
    processing_checksum: str = "",
    summarization_mode: str = "",
) -> dict[str, Any]:
    """Build the complete identity needed to trust a cached summary."""
    source = Path(source_path)
    identity_path = Path(source_identity_path or source_path).as_posix()
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "pmcid": normalize_pmcid(pmcid),
        "source_path": identity_path,
        "source_checksum": source_checksum or sha256_file(source),
        "prompt_checksum": prompt_checksum,
        "schema_checksum": schema_checksum,
        "model": model,
        "title": str(title or "").strip(),
        "year": str(year or "").strip(),
        "processing_checksum": processing_checksum,
        "summarization_mode": summarization_mode,
        "created_at": timestamp or utc_now(),
    }


def write_summary_cache_metadata(
    summary_path: str | Path,
    **kwargs: Any,
) -> dict[str, Any]:
    metadata = build_summary_cache_metadata(**kwargs)
    atomic_write_json(summary_cache_path(summary_path), metadata)
    return metadata


def summary_cache_matches(
    summary_path: str | Path,
    *,
    pmcid: str | int,
    source_path: str | Path,
    model: str,
    prompt_checksum: str,
    schema_checksum: str,
    source_checksum: str | None = None,
    source_identity_path: str | Path | None = None,
    title: str = "",
    year: str = "",
    processing_checksum: str = "",
    summarization_mode: str | None = "",
) -> bool:
    """Return false when any source, metadata, prompt, or processing input changed."""
    cache_path = summary_cache_path(summary_path)
    if not Path(summary_path).is_file() or not cache_path.is_file():
        return False
    try:
        metadata = json.loads(cache_path.read_text())
        expected_source_checksum = source_checksum or sha256_file(source_path)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(metadata, Mapping):
        return False
    expected = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "pmcid": normalize_pmcid(pmcid),
        "source_path": Path(source_identity_path or source_path).as_posix(),
        "source_checksum": expected_source_checksum,
        "prompt_checksum": prompt_checksum,
        "schema_checksum": schema_checksum,
        "model": model,
        "title": str(title or "").strip(),
        "year": str(year or "").strip(),
        "processing_checksum": processing_checksum,
    }
    if summarization_mode is not None:
        expected["summarization_mode"] = summarization_mode
    return all(metadata.get(key) == value for key, value in expected.items())
