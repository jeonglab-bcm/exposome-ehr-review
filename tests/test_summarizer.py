"""Tests for the summarizer pipeline (no live API calls)."""
import json
import sys
from pathlib import Path

# allow `pytest` from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from summarizer.schema import ManuscriptChecklist
from summarizer.llm_client import extract_json_object


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
