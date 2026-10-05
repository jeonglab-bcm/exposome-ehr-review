"""The decision-model pipeline end to end, offline: paragraphs -> questions -> candidates -> excerpts.

A fake decision model stands in for Ollaya / Jev / the lab server: it answers
every choice question by picking, in order, the options listed in YES, and
"none" once those run out, so the four-stage cascade can be checked exactly.
"""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from exposome_ehr import decision as D
from exposome_ehr import paragraphs as P

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(ROOT / "scripts"))

YES = {"particulate_matter", "asthma_wheeze", "ehr", "risk_factor_for", "ascertained_from",
       "particulate_matter|risk_factor_for|asthma_wheeze", "asthma_wheeze|ascertained_from|ehr",
       "cohort", "child"}


def fake_model(p_yes=0.9, noul=0.8, yes=YES):
    asked = []

    def transport(url, body, headers):
        req = json.loads(body)
        (name, q), = req["questions"].items()
        asked.append(q)
        if q["type"] == "noul":
            return {"model": "fake-1", "answers": {name: {"noul": noul}}}
        opts = list(q["criteria"])
        pick = next((o for o in opts if o in yes), D.NONE)
        probs = {o: (p_yes if o == pick else 0.0) for o in opts}
        probs[D.NONE] = round(1 - p_yes, 4) if pick != D.NONE else 1.0
        return {"model": "fake-1", "answers": {name: {"probabilities": probs}}, "usage": {"input_tokens": 10}}

    return D.Backend("fake", "http://fake", transport=transport), asked


# ------------------------------------------------------------------ paragraphs

@pytest.fixture(scope="module")
def jats_paper():
    record = {"pmid": "99900001", "pmc": "PMC1", "title": "Synthetic test article"}
    return P.build(record, FIXTURES / "jats_sample.xml")


def test_jats_paragraphs_keep_running_text_only(jats_paper):
    texts = [p["normalized_text"] for p in jats_paper["paragraphs"]]
    assert jats_paper["source"] == "jats"
    assert not any("graphical" in t or "table caption" in t or "reference that" in t for t in texts)
    assert any("A nested list item stays in its parent." in t for t in texts)
    assert sum("nested list item" in t for t in texts) == 1


def test_citations_are_normalised_and_ids_are_stable(jats_paper):
    ehr = next(p for p in jats_paper["paragraphs"] if "electronic health records" in p["normalized_text"])
    assert ehr["normalized_text"] == "Asthma diagnoses were taken from electronic health records [CITATION]."
    assert [p["id"] for p in jats_paper["paragraphs"]][:2] == ["99900001:p001", "99900001:p002"]


def test_paragraph_kinds_follow_the_top_level_heading(jats_paper):
    kinds = {p["normalized_text"][:20]: p["kind"] for p in jats_paper["paragraphs"]}
    assert kinds["Air pollution may af"] == "abstract"
    assert kinds["Asthma diagnoses wer"] == "methods"
    assert kinds["Each 5 ug/m3 of PM2."] == "results"      # under Results > Sensitivity analysis
    assert kinds["We found a harmful a"] == "discussion"
    assert kinds["Data are available o"] == "availability"


def test_licence_decides_what_a_hosted_model_may_read(jats_paper):
    assert P.is_cc_by(jats_paper["license"])
    assert not P.is_cc_by("https://creativecommons.org/licenses/by-nc/4.0/")
    assert not P.is_cc_by("This file is available for text mining.")
    assert P.is_cc_by("https://creativecommons.org/publicdomain/zero/1.0/")


def test_manifest_carries_no_text(jats_paper):
    m = P.manifest([jats_paper])
    assert "electronic health records" not in m
    assert m.splitlines()[0] == "id\tsha\tchars\tkind\tsection"


def test_abstract_fallback_uses_structured_sections():
    rec = {"pmid": "5", "title": "t", "abstract_sections": [
        {"label": "BACKGROUND", "text": "b"}, {"label": "RESULTS", "text": "r"}]}
    paper = P.build(rec, None)
    assert paper["source"] == "abstract"
    assert [p["section"] for p in paper["paragraphs"]] == ["Abstract > BACKGROUND", "Abstract > RESULTS"]


# ------------------------------------------------------------------- questions

def test_questions_come_from_config(vocab):
    assert D.link_groups(vocab) == ["exposure", "outcome", "data_source", "assessment"]
    assert set(D.link_predicates(vocab)) == {"risk_factor_for", "protective_for", "no_association_with",
                                             "assessed_by", "ascertained_from", "linked_with"}
    tv = D.terms(vocab)
    # Only pairs the predicate's domain and range allow are offered.
    assert D.pairs("risk_factor_for", ["asthma_wheeze", "particulate_matter", "ehr"], vocab, tv) == [
        ("particulate_matter", "asthma_wheeze")]
    # linked_with is symmetric: one direction only.
    assert D.pairs("linked_with", ["ehr", "health_registry"], vocab, tv) == [("ehr", "health_registry")]


def test_judge_runs_the_four_stages_and_yields_the_links(vocab):
    be, asked = fake_model()
    para = {"id": "1:p001", "sha": "x", "normalized_text": "PM2.5 raised asthma risk in EHR data."}
    rec = D.judge(be, "fake", para, vocab)
    assert D.cascade(rec, vocab) == {("particulate_matter", "risk_factor_for", "asthma_wheeze"),
                                     ("asthma_wheeze", "ascertained_from", "ehr")}
    assert "paragraph" not in json.dumps(rec)  # no text stored, only ids and probabilities
    assert be.answered_by == {"fake-1"}
    # The relation options are the `option` texts from relations.yaml.
    relation_q = next(q for q in asked if q.get("instructions", "").startswith("Which relation"))
    assert relation_q["criteria"]["risk_factor_for"] == vocab.predicates["risk_factor_for"]["option"]


def test_stricter_gate_is_applied_offline(vocab):
    be, _ = fake_model(p_yes=0.6)
    rec = D.judge(be, "fake", {"id": "1:p001", "sha": "x", "normalized_text": "t"}, vocab)
    assert D.cascade(rec, vocab, gate=None)
    assert D.cascade(rec, vocab, gate=0.7) == set()


def test_one_term_and_no_relation_stop_the_cascade(vocab):
    be, asked = fake_model(yes={"particulate_matter"})
    rec = D.judge(be, "fake", {"id": "1:p001", "sha": "x", "normalized_text": "t"}, vocab)
    assert "relational" not in rec and D.cascade(rec, vocab) == set()
    be, _ = fake_model(noul=0.2)
    rec = D.judge(be, "fake", {"id": "1:p001", "sha": "x", "normalized_text": "t"}, vocab)
    assert rec["relational"] == 0.2 and "relations" not in rec


def test_options_over_the_budget_are_asked_in_chunks(vocab):
    be, asked = fake_model()
    be.max_options = 5
    D.rounds(be, "fake", "t", "q", {f"o{i}": f"o{i}" for i in range(10)}, "none")
    assert all(len(q["criteria"]) <= 5 for q in asked)
    assert {k for q in asked for k in q["criteria"]} >= {f"o{i}" for i in range(10)}


# ---------------------------------------------------------------- study coding

def test_study_coding_answers_every_field_from_its_passages(vocab, jats_paper):
    coding = yaml.safe_load((ROOT / "config/study_coding.yaml").read_text())
    be, _ = fake_model(yes=YES | {"on_request"})
    rec = D.code_paper(be, "fake", jats_paper, coding, vocab)
    assert set(rec["fields"]) == set(coding["fields"])
    ans = {f: D.coding_answer(coding["fields"][f], a) for f, a in rec["fields"].items()}
    assert ans["routinely_collected_data"] is True
    assert ans["data_sources"] == ["ehr"]
    assert ans["age_strata"] == ["child"]
    assert ans["study_design"] == "cohort"
    assert ans["data_availability"] == "on_request"
    # data availability was read from the availability statement first
    first = rec["fields"]["data_availability"][0]["id"]
    assert next(p for p in jats_paper["paragraphs"] if p["id"] == first)["kind"] == "availability"


# ------------------------------------------------------------ candidates, judge

def test_candidates_need_two_readers_and_judged_links_reach_the_excerpts(tmp_path, vocab, jats_paper):
    import assemble_links as A

    run = tmp_path / "run"
    para = jats_paper["paragraphs"][3]
    for reader, yes in (("a", YES), ("b", YES), ("c", {"lead", "neurodevelopment"})):
        be, _ = fake_model(yes=yes)
        (run / "runs" / reader).mkdir(parents=True)
        (run / "runs" / reader / "99900001.jsonl").write_text(json.dumps(D.judge(be, "fake", para, vocab)) + "\n")
    (run / "paragraphs.tsv").write_text(P.manifest([jats_paper]))
    readers = {"a": "a", "b": "b", "c": "c"}
    cands = A.candidates(run, readers, 2, vocab)
    assert {tuple(c["edge"]) for c in cands} == {("particulate_matter", "risk_factor_for", "asthma_wheeze"),
                                                 ("asthma_wheeze", "ascertained_from", "ehr")}
    (run / "candidates.json").write_text(json.dumps({"candidates": cands}))
    quote = "Asthma diagnoses were taken from electronic health records 1,2."
    for c in cands:
        ok = c["edge"][1] == "ascertained_from"
        for r in (1, 2, 3):
            d = run / "judge" / f"r{r}"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{c['id']}.json").write_text(json.dumps(
                {"verdict": "supports" if ok else "not_stated", "passage": 1 if ok else 0,
                 "quote": quote if ok else "", "reason": "r"}))
            (d / f"{c['id']}.meta.json").write_text(json.dumps({"quote_check": "exact" if ok else "empty"}))
    text, stats = A.assemble(run, readers, 2, vocab)
    data = yaml.safe_load(text)
    assert [e["edge"] for e in data["excerpts"]] == [["asthma_wheeze", "ascertained_from", "ehr"]]
    assert [e["edge"] for e in data["not_added"]] == [["particulate_matter", "risk_factor_for", "asthma_wheeze"]]
    assert data["excerpts"][0]["evidence"][0]["paper"] == "99900001"
    assert stats == {"kept": 1, "not_confirmed": 1, "links": 1, "review": 1}


def test_quote_matching_tolerates_typography_not_paraphrase():
    from quotes import match, variants

    texts = variants("PM2.5 was associated with an in-\ncreased risk of asthma – in children.")
    assert match("an increased risk of asthma - in children", texts) == "exact"
    assert match("PM2.5 raised asthma risk", texts) is None


def test_relations_block_matches_the_excerpts():
    import subprocess

    rc = subprocess.run([sys.executable, str(ROOT / "scripts/build_relations.py"), "--check"]).returncode
    assert rc == 0
