"""Tests for the summarizer pipeline (no live API calls)."""
import json
import sys
from pathlib import Path

# allow `pytest` from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from summarizer.schema import ManuscriptChecklist
from summarizer.llm_client import extract_json_object, _merge_partials
from summarizer.run import load_all_summaries


# ── schema ─────────────────────────────────────────────────────────────────
def test_schema_validates_minimal_checklist():
    c = ManuscriptChecklist(
        pmcid="7145790", title="Childhood T1DM EWAS", year="2020",
        ehr_used=True, ehr_evidence="Hospital Episode Statistics",
        summary="EWAS of childhood T1DM across England.",
        key_findings=["15 environmental factors associated with T1DM."],
        captured_features=["HES ICD codes"],
        pathologies_diseases=["type 1 diabetes"],
        study_design="ecological EWAS",
        data_source_type="EHR",
        population="children 0-9 yrs across England",
        exposure_domain="air pollution",
        limitations=["ecological design"],
        confidence="medium",
    )
    assert c.pmcid == "PMC7145790"  # normalized
    assert c.ehr_used is True
    assert c.model_dump_json(indent=2)


def test_schema_requires_ehr_fields():
    """ehr_used + ehr_evidence are required even when empty-ish."""
    c = ManuscriptChecklist(
        pmcid="PMC1", title="t", year="2020",
        ehr_used=False, ehr_evidence="n/a", summary="s",
    )
    assert c.captured_features == []
    assert c.pathologies_diseases == []
    assert c.confidence == "unclear"


# ── JSON extraction (the chatty-model handler) ──────────────────────────────
CHATTY = """Let me think about this.
The study uses EHR so ehr_used is true.

```json
{"ehr_used": true, "summary": "ok", "key_findings": ["x"]}
```
Done."""


def test_extract_json_from_fenced_block():
    obj = extract_json_object(CHATTY)
    assert obj == {"ehr_used": True, "summary": "ok", "key_findings": ["x"]}


def test_extract_json_plain_object():
    raw = 'noise {"ehr_used": false, "summary": "s"} trailing'
    obj = extract_json_object(raw)
    assert obj["ehr_used"] is False


def test_extract_json_none_on_garbage():
    assert extract_json_object("no json here at all") is None


def test_extract_json_picks_largest_balanced():
    raw = '{"a": 1} then {"b": 2, "c": {"d": 3}}'
    obj = extract_json_object(raw)
    assert obj["b"] == 2
    assert obj["c"]["d"] == 3


def test_extract_json_takes_last_of_several_fenced_blocks():
    """A model that revises itself emits several blocks; the last is the answer."""
    raw = (
        'First attempt:\n```json\n{"ehr_used": false, "summary": "draft"}\n```\n'
        'On reflection:\n```json\n{"ehr_used": true, "summary": "final"}\n```\n'
    )
    obj = extract_json_object(raw)
    assert obj == {"ehr_used": True, "summary": "final"}


def test_extract_json_repairs_truncated_response():
    """Output cut at max_tokens: close the object without eating the last value."""
    assert extract_json_object('{"summary": "ok", "study_design": "cohort"') == {
        "summary": "ok", "study_design": "cohort",
    }
    # cut mid-string
    assert extract_json_object('{"summary": "ok", "population": "children ag')[
        "population"
    ].startswith("children ag")
    # cut just after a comma
    assert extract_json_object('{"summary": "ok",') == {"summary": "ok"}


# ── chunked merge ───────────────────────────────────────────────────────────
def test_merge_partials_evidence_only_from_ehr_chunks():
    """Chunks that saw no EHR must not contribute evidence for ehr_used=True."""
    merged = _merge_partials([
        {"ehr_used": False, "ehr_evidence": "n/a", "summary": "intro",
         "key_findings": ["a"], "confidence": "low"},
        {"ehr_used": True, "ehr_evidence": "We used Hospital Episode Statistics.",
         "key_findings": ["b"], "confidence": "high"},
    ])
    assert merged["ehr_used"] is True
    assert merged["ehr_evidence"] == "We used Hospital Episode Statistics."
    assert merged["summary"] == "intro"      # first non-empty wins
    assert merged["key_findings"] == ["a", "b"]
    assert merged["confidence"] == "high"    # highest across chunks


def test_merge_partials_dedupes_overlapping_evidence():
    """Chunks overlap, so the same sentence arrives twice — join it once."""
    sentence = "Diagnoses were drawn from the EHR problem list."
    merged = _merge_partials([
        {"ehr_used": True, "ehr_evidence": sentence},
        {"ehr_used": True, "ehr_evidence": sentence},
    ])
    assert merged["ehr_evidence"] == sentence


def test_merge_partials_no_ehr_anywhere():
    merged = _merge_partials([{"ehr_used": False, "ehr_evidence": "n/a"}])
    assert merged["ehr_used"] is False
    assert merged["ehr_evidence"] == "n/a"


# ── combined-file assembly ──────────────────────────────────────────────────
def test_load_all_summaries_reads_whole_dir(tmp_path):
    """The combined batch covers the corpus, not just the papers a run touched."""
    for pmcid, year in (("PMC1", "2019"), ("PMC2", "2021")):
        c = ManuscriptChecklist(
            pmcid=pmcid, title="t", year=year, ehr_used=True,
            ehr_evidence="e", summary="s",
        )
        (tmp_path / f"{pmcid}.json").write_text(c.model_dump_json(indent=2))
    (tmp_path / "broken.json").write_text("{not json")

    loaded = load_all_summaries(tmp_path)
    assert sorted(c.pmcid for c in loaded) == ["PMC1", "PMC2"]  # invalid skipped


# ── source budget ───────────────────────────────────────────────────────────
def test_summarize_sends_the_whole_paper_not_just_the_intro():
    """A 6k budget showed the model the abstract and intro and nothing else, so
    it reported the aim as the finding and the truncation as a limitation
    (issues #32-#39). The Results/Discussion must reach the model."""
    from unittest.mock import MagicMock
    from summarizer.llm_client import summarize_text, SOURCE_CHAR_BUDGET

    body = "Methods. " + ("filler text " * 4000)          # ~48k chars
    paper = body + "\nDISCUSSION_MARKER: the association was significant."
    assert len(paper) < SOURCE_CHAR_BUDGET               # fits in one shot now

    client = MagicMock()
    client.chat.completions.create.return_value.choices = [
        MagicMock(message=MagicMock(content='{"ehr_used": true, "ehr_evidence": "e", '
                                            '"summary": "s", "confidence": "high"}'))
    ]
    summarize_text(text=paper, pmcid="PMC1", title="t", year="2020",
                   source_format="pdf", client=client, model="m")

    sent = client.chat.completions.create.call_args.kwargs["messages"][-1]["content"]
    assert "DISCUSSION_MARKER" in sent


def test_request_timeout_is_sized_for_the_budget():
    """A 100K-char prompt takes minutes to prefill. At the old 120s every call
    timed out, burned its retries, and fell through to chunked recovery whose
    calls timed out too — 62 minutes of no progress on a 10-paper run."""
    from unittest.mock import MagicMock
    from summarizer.llm_client import summarize_text, REQUEST_TIMEOUT

    assert REQUEST_TIMEOUT >= 600

    client = MagicMock()
    client.chat.completions.create.return_value.choices = [
        MagicMock(message=MagicMock(content='{"ehr_used": false, "ehr_evidence": "n/a", '
                                            '"summary": "s"}'))
    ]
    summarize_text(text="paper", pmcid="PMC1", title="t", year="2020",
                   source_format="pdf", client=client, model="m")
    assert client.chat.completions.create.call_args.kwargs["timeout"] == REQUEST_TIMEOUT


def test_chunking_scales_with_the_budget():
    """Chunks sized for a 6k budget would dissect a full paper into ~30 calls."""
    from summarizer.llm_client import _chunk_text, SOURCE_CHAR_BUDGET
    assert len(_chunk_text("x" * SOURCE_CHAR_BUDGET)) <= 6


# ── extraction (PDF/XML) ────────────────────────────────────────────────────
def test_pmcid_from_filename():
    from summarizer.extract import pmcid_from_filename
    assert pmcid_from_filename(Path("2020_PMC7145790_Childhood_type1.pdf")) == "PMC7145790"
    assert pmcid_from_filename(Path("2024_PMC10312866_BMI.xml")) == "PMC10312866"


def test_extract_jats_xml_abstract_only():
    """Abstract-only JATS records still return title+abstract text."""
    from summarizer.extract import extract_jats_xml
    import tempfile
    xml = (
        '<article article-type="abstract"><front>'
        '<article-meta><title-group><article-title>My Poster</article-title>'
        '</title-group></article-meta></front><abstract>p abstract</abstract></article>'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as f:
        f.write(xml)
        f.flush()
        text = extract_jats_xml(Path(f.name))
    assert "My Poster" in text
    assert "p abstract" in text


# ── chunked recovery helpers ────────────────────────────────────────────────
def test_chunk_text_short_returns_single():
    from summarizer.llm_client import _chunk_text
    assert _chunk_text("short text") == ["short text"]


def test_chunk_text_splits_with_overlap():
    from summarizer.llm_client import _chunk_text
    text = "x" * 9000
    chunks = _chunk_text(text, size=4000, overlap=600)
    assert len(chunks) >= 2
    # overlap: start of chunk 2 should be within chunk 1's tail
    assert chunks[1][:10] == chunks[0][3400:3410]


def test_merge_partials_unions_and_or_ehr():
    from summarizer.llm_client import _merge_partials
    partials = [
        {"ehr_used": False, "ehr_evidence": "", "summary": "part A",
         "key_findings": ["A1"], "captured_features": ["BMI"],
         "pathologies_diseases": ["obesity"], "confidence": "medium"},
        {"ehr_used": True, "ehr_evidence": "used HES", "summary": "part B",
         "key_findings": ["A1", "B2"], "captured_features": ["HES codes"],
         "pathologies_diseases": ["T1DM"], "confidence": "high"},
    ]
    m = _merge_partials(partials)
    assert m["ehr_used"] is True           # OR across chunks
    assert "HES" in m["ehr_evidence"]
    assert m["key_findings"] == ["A1", "B2"]  # union, dedup
    assert set(m["captured_features"]) == {"BMI", "HES codes"}
    assert set(m["pathologies_diseases"]) == {"obesity", "T1DM"}
    assert m["confidence"] == "high"         # highest across chunks
