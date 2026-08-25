"""Tests for the deterministic candidate screen.

Two layers, kept separate on purpose:

* ``unit:``        one classifier, one rule, one assertion each
* ``end-to-end:``  screen_candidate() folding all axes into a decision
"""
from screening import (
    _classify_human_subject,
    _classify_primary_study,
    load_screening_overrides,
    partition_screening,
    screen_candidate,
)


def _article(pmcid: str, title: str, **extra):
    """Build a candidate record shaped like one PubMed/PMC ESummary entry.

    ``screen_candidate`` reads a plain mapping, so tests don't need the network.
    The three keys set here are the minimum it needs to do anything:

    * ``uid``     -- the bare numeric id; the screen re-prefixes it to PMCxxx
    * ``title``   -- most regexes run against title (+ abstract, when present)
    * ``pubtype`` -- publication types, checked against the primary/non-primary
                     sets. "Journal Article" is a neutral default that neither
                     qualifies nor disqualifies a record.

    ``**extra`` merges in any other field the real record would carry, so each
    test names only what it is actually exercising. The two that show up most:

    * ``mesh_terms=["Humans"]`` -- NLM-indexed MeSH headings. The screen trusts
      these over title wording, because they are curated rather than inferred.
      Also accepted as ``mesh`` or ``meshheadings`` (see ``_mesh_terms``).
    * ``metadata_complete=True`` -- did the PubMed enrichment step actually
      finish for this record. ``False`` blocks automatic inclusion outright:
      the screen refuses to judge a record whose metadata it never fetched.
      Both are absent by default, which is what keeps the "ambiguous" tests
      ambiguous.
    """
    item = {
        "uid": pmcid.removeprefix("PMC"),
        "title": title,
        "pubtype": ["Journal Article"],
    }
    item.update(extra)
    return item


# ── unit: the primary-study axis ─────────────────────────────────────────────

def test_unit_non_primary_publication_type_disqualifies():
    verdict, _evidence, reasons = _classify_primary_study({"review"}, "Some title", "")
    assert verdict is False
    assert "non-primary publication type" in reasons[0]


def test_unit_primary_type_qualifies():
    verdict, evidence, reasons = _classify_primary_study(
        {"journal article"}, "A cohort study of prenatal metals", ""
    )
    assert verdict is True and reasons == []
    assert "primary-study-compatible" in evidence[0]


def test_unit_design_language_qualifies_when_type_is_uninformative():
    verdict, evidence, _ = _classify_primary_study(
        set(), "Untyped record", "we followed a birth cohort of 500 participants"
    )
    assert verdict is True
    assert "primary study design" in evidence[0]


def test_unit_primary_study_is_undetermined_not_false_when_silent():
    """Undetermined must block inclusion without causing exclusion."""
    verdict, evidence, reasons = _classify_primary_study(set(), "Metals and health", "")
    assert verdict is None
    assert evidence == [] and reasons == []


# ── unit: the human-subject axis ─────────────────────────────────────────────

def test_unit_indexed_humans_mesh_outranks_title_wording():
    verdict, evidence, _ = _classify_human_subject(
        "Effects in mice", "", {"humans"}
    )
    assert verdict is True
    assert "subject metadata includes Humans" in evidence[0]


def test_unit_animal_mesh_without_humans_disqualifies():
    verdict, _evidence, reasons = _classify_human_subject("Some title", "", {"animals"})
    assert verdict is False
    assert "animal-only" in reasons[0]


def test_unit_in_vitro_title_disqualifies_even_with_human_cells():
    verdict, _evidence, reasons = _classify_human_subject(
        "In vitro effects in human cell lines", "", set()
    )
    assert verdict is False
    assert "in-vitro" in reasons[0]


def test_unit_human_subject_is_undetermined_not_false_when_silent():
    verdict, evidence, reasons = _classify_human_subject("Metals and health", "", set())
    assert verdict is None
    assert evidence == [] and reasons == []


# ── end-to-end: screen_candidate ─────────────────────────────────────────────


def test_known_positive_core_and_mixture_seeds_are_included():
    seeds = [
        (_article("PMC9678903", "Multi-omics signatures of the human early-life exposome",
                  mesh_terms=["Humans"], metadata_complete=True), "core"),
        (_article("PMC6144482", "The Human Early-Life Exposome (HELIX) cohort",
                  mesh_terms=["Humans"], metadata_complete=True), "core"),
        (_article("PMC11117089", "Prenatal chemical mixtures and metabolic-syndrome risk in children",
                  mesh_terms=["Humans"], metadata_complete=True), "operational_mixtures"),
        (_article("PMC10099694", "Prenatal exposures and childhood health outcomes in a cohort",
                  mesh_terms=["Humans"], metadata_complete=True), "operational_mixtures"),
        (_article("PMC13099396", "A large-scale study of the human exposome in adults",
                  mesh_terms=["Humans"], metadata_complete=True), "core"),
    ]

    decisions = [
        screen_candidate(item, query_names=[f"q-{arm}"], query_arms=[arm])
        for item, arm in seeds
    ]

    assert {decision.pmcid for decision in decisions} == {
        "PMC9678903", "PMC6144482", "PMC11117089", "PMC10099694", "PMC13099396"
    }
    assert all(decision.decision == "included" for decision in decisions)


def test_genetics_only_and_human_cell_in_vitro_records_are_excluded():
    genetics = screen_candidate(
        _article("PMC46", "Human genome-wide association results in an exposome cohort"),
        query_arms=["core"],
    )
    cells = screen_candidate(
        _article("PMC47", "In vitro effects of vaccine exposure in human cell lines"),
        query_arms=["operational_vaccine"],
    )

    assert genetics.decision == "excluded"
    assert genetics.scope_classification == "out-of-scope"
    assert "genetics-only" in genetics.exclusion_reasons[-1]
    assert cells.decision == "excluded"
    assert cells.human_study is False


def test_review_animal_and_correction_negative_seeds_are_excluded():
    review = screen_candidate(
        _article("PMC1", "The human exposome: a systematic review", pubtype=["Review"]),
        query_arms=["core"],
    )
    animal = screen_candidate(
        _article("PMC2", "Prenatal mixture exposure in rat brain"),
        query_arms=["operational_mixtures"],
    )
    correction = screen_candidate(
        _article("PMC3", "Correction: human exposome cohort", pubtype=["Published Erratum"]),
        query_arms=["core"],
    )

    assert {review.decision, animal.decision, correction.decision} == {"excluded"}
    assert animal.human_study is False
    assert review.primary_study is False


def test_ambiguous_candidate_is_pending_not_silently_included():
    decision = screen_candidate(
        {"uid": "9", "title": "Integrated exposome signatures", "pubtype": []},
        query_names=["core-exposome"],
        query_arms=["core"],
    )

    assert decision.decision == "pending"
    assert decision.human_study is None
    assert decision.primary_study is None
    assert decision.query_provenance == ("core-exposome",)


def test_missing_pubmed_enrichment_is_pending_even_with_title_heuristics():
    decision = screen_candidate(
        _article(
            "PMC90",
            "Human exposome cohort",
            metadata_complete=False,
            metadata_missing=["pmid"],
        ),
        query_arms=["core"],
    )
    assert decision.human_study is True
    assert decision.primary_study is True
    assert decision.decision == "pending"
    assert decision.metadata_complete is False


def test_manual_override_records_reviewer_and_evidence():
    decision = screen_candidate(
        {"uid": "9", "title": "Integrated exposome signatures", "pubtype": []},
        query_names=["core_exposome"],
        query_arms=["core"],
        override={
            "decision": "included",
            "reviewer": "AB",
            "reviewed_at": "2026-08-21T12:00:00Z",
            "eligibility_evidence": ["Full text reports a prospective human cohort."],
            "human_study": True,
            "primary_study": True,
        },
    )

    assert decision.decision == "included"
    assert decision.screening_method == "manual-override"
    assert decision.reviewer == "AB"
    assert "prospective human cohort" in decision.eligibility_evidence[-1]


def test_incomplete_manual_inclusion_remains_pending():
    decision = screen_candidate(
        {"uid": "10", "title": "Integrated exposome signatures", "pubtype": []},
        query_arms=["core"],
        override={"decision": "included", "reviewer": "AB"},
    )

    assert decision.decision == "pending"
    assert "manual inclusion requires" in decision.exclusion_reasons[-1]


def test_durable_override_loader_requires_reviewer_timestamp_and_evidence(tmp_path):
    valid = tmp_path / "valid.json"
    valid.write_text("""{
      "records": {
        "9": {
          "decision": "included",
          "reviewer": "AB",
          "reviewed_at": "2026-08-21T12:00:00Z",
          "eligibility_evidence": ["Verified prospective human cohort."],
          "human_study": true,
          "primary_study": true
        }
      }
    }""")
    loaded = load_screening_overrides(valid)
    assert list(loaded) == ["PMC9"]
    assert loaded["PMC9"]["reviewer"] == "AB"

    invalid = tmp_path / "invalid.json"
    invalid.write_text("""{
      "PMC9": {"decision": "included", "reviewer": "AB",
               "eligibility_evidence": ["evidence"]}
    }""")
    import pytest
    with pytest.raises(ValueError, match="reviewed_at"):
        load_screening_overrides(invalid)


def test_partition_keeps_candidates_separate_from_included_records():
    included = screen_candidate(
        _article("PMC1", "Human exposome cohort"),
        query_names=["core_exposome"], query_arms=["core"],
    )
    pending = screen_candidate(
        {"uid": "2", "title": "Exposome signatures", "pubtype": []},
        query_arms=["core"],
    )

    split = partition_screening([pending, included])

    assert [row["pmcid"] for row in split["included"]] == ["PMC1"]
    assert [row["pmcid"] for row in split["pending"]] == ["PMC2"]
    assert split["excluded"] == []


def test_age_is_not_an_inclusion_rule():
    adult = screen_candidate(
        _article("PMC7", "Adult workers in an environment-wide association study"),
        query_names=["core_exposome"],
        query_arms=["core"],
    )
    pediatric = screen_candidate(
        _article("PMC8", "Childhood environment-wide association cohort study"),
        query_names=["core_exposome"],
        query_arms=["core"],
    )

    assert adult.decision == pediatric.decision == "included"


def test_vaccine_scoped_records_defer_to_a_human_until_the_gate_lands():
    """Vaccination is only an exposure when a later outcome is measured.

    Deciding that needs the dedicated exposure-outcome gate, which is not in
    this module yet. Until it is, a vaccine-scoped record must reach a human
    rather than being auto-included on the strength of the other axes alone --
    otherwise uptake and hesitancy papers would sail through.
    """
    record = screen_candidate(
        _article(
            "PMC99", "Influenza vaccination and subsequent cardiovascular events",
            mesh_terms=["Humans"], metadata_complete=True,
        ),
        query_names=["q-vaccine"], query_arms=["vaccine"],
    )

    assert record.scope_classification == "vaccine-exposure"
    assert record.decision == "pending"
    assert record.human_study is True and record.primary_study is True
    assert any("not automated" in e for e in record.eligibility_evidence)
