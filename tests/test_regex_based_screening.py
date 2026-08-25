"""Tests for the deterministic candidate screen.

Two layers, kept separate on purpose:

* ``unit:``        one classifier, one rule, one assertion each
* ``end-to-end:``  screen_candidate() folding all axes into a decision
"""
from regex_based_screening import (
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
    """A `Review` pubtype is positive disqualifying evidence, so verdict is False.

    False, not None: we *know* this is not a primary study rather than being
    unsure. That distinction is what lets the screen exclude instead of defer.
    """
    verdict, _evidence, reasons = _classify_primary_study({"review"}, "Some title", "")
    assert verdict is False
    assert "non-primary publication type" in reasons[0]


def test_unit_primary_type_qualifies():
    """`Journal Article` is in the primary set, so the type alone settles the axis.

    True, with the type named in the evidence trail and `reasons` empty --
    nothing here disqualifies, so there is nothing to report against it.
    """
    verdict, evidence, reasons = _classify_primary_study(
        {"journal article"}, "A cohort study of prenatal metals", ""
    )
    assert verdict is True and reasons == []
    assert "primary-study-compatible" in evidence[0]


def test_unit_design_language_qualifies_when_type_is_uninformative():
    """With no pubtype at all, design wording in the text carries the axis.

    "we followed a birth cohort of 500 participants" matches the primary-design
    pattern, so an untyped record can still qualify. This is the fallback path,
    one step weaker than an NLM-indexed publication type.
    """
    verdict, evidence, _ = _classify_primary_study(
        set(), "Untyped record", "we followed a birth cohort of 500 participants"
    )
    assert verdict is True
    assert "primary study design" in evidence[0]


def test_unit_primary_study_is_undetermined_not_false_when_silent():
    """Undetermined must block inclusion without causing exclusion.

    A bare title, no pubtype, no design language: the screen knows nothing. The
    verdict is None and *both* lists stay empty -- an empty `reasons` is
    precisely what stops `screen_candidate` from excluding the record. This is
    the invariant the whole screen rests on: absence of evidence is not
    evidence.
    """
    verdict, evidence, reasons = _classify_primary_study(set(), "Metals and health", "")
    assert verdict is None
    assert evidence == [] and reasons == []


# ── unit: the human-subject axis ─────────────────────────────────────────────

def test_unit_indexed_humans_mesh_outranks_title_wording():
    """Curated MeSH beats title wording, deliberately.

    The title says "Effects in mice", but NLM indexed the paper `Humans`. MeSH
    headings are assigned by curators; titles are written to attract readers.
    So the verdict is True on the metadata and the misleading title gets no
    vote. Reversing this precedence would misclassify real human studies.
    """
    verdict, evidence, _ = _classify_human_subject(
        "Effects in mice", "", {"humans"}
    )
    assert verdict is True
    assert "subject metadata includes Humans" in evidence[0]


def test_unit_animal_mesh_without_humans_disqualifies():
    """`Animals` indexed with no `Humans` disqualifies rather than defers.

    False, so `screen_candidate` can exclude outright. Note the asymmetry with
    the test above: `Humans` is checked *first*, so a paper indexed both ways
    counts as human -- co-indexing usually means a human study with an animal
    component, not the reverse.
    """
    verdict, _evidence, reasons = _classify_human_subject("Some title", "", {"animals"})
    assert verdict is False
    assert "animal-only" in reasons[0]


def test_unit_in_vitro_title_disqualifies_even_with_human_cells():
    """Human *cells* are not human *participants*.

    "human cell lines" contains the word human, so a naive substring check
    would wave this through. The in-vitro pattern is tested first and wins,
    giving False. This exact case is why `_classify_human_subject` checks in
    the order it does.
    """
    verdict, _evidence, reasons = _classify_human_subject(
        "In vitro effects in human cell lines", "", set()
    )
    assert verdict is False
    assert "in-vitro" in reasons[0]


def test_unit_human_subject_is_undetermined_not_false_when_silent():
    """The human axis defers exactly the way the primary-study axis does.

    No in-vitro wording, no MeSH, no human-population language: None with both
    lists empty, so the record goes `pending` rather than `excluded`. Asserted
    separately from the primary-study case because the two axes are independent
    and either one regressing alone would be a real bug.
    """
    verdict, evidence, reasons = _classify_human_subject("Metals and health", "", set())
    assert verdict is None
    assert evidence == [] and reasons == []


# ── end-to-end: screen_candidate ─────────────────────────────────────────────


def test_known_positive_core_and_mixture_seeds_are_included():
    """Five real PMCIDs that must survive the screen -- the regression floor.

    Each has affirmative evidence on every axis: `Humans` MeSH, complete
    metadata, a named query arm, and exposome or mixture language in the title.
    `included` is therefore the only defensible answer. If a future regex
    tightening silently drops one of these from the corpus, this test is what
    catches it.
    """
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
    """Two records that pass a keyword match but are genuinely out of scope.

    The GWAS paper says "exposome cohort" yet analyses genotypes rather than
    exposures, so the genetics rule forces `out-of-scope`. The second says
    "human" but studies cell lines. Both must be `excluded`, not `pending`:
    there is positive evidence *against* them, not merely a lack of evidence
    for them.
    """
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
    """The three commonest false positives, each caught on a different axis.

    A systematic review fails primary-study on its pubtype; a rat study fails
    the human axis on its title; an erratum fails primary-study on `Published
    Erratum`. All three exclude -- and the specific axis that did the work is
    asserted, so a failure points at the rule that broke rather than just
    "something changed".
    """
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
    """The core safety property: relevant but unproven means a human looks.

    Empty pubtype and no MeSH leave both content axes at None, while the title
    and query arm make the record look plausibly in scope. That combination is
    the dangerous one, because it is the most tempting to auto-include. It must
    land on `pending`, and the query provenance must survive onto the record so
    the reviewer can see which arm retrieved it.
    """
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
    """A record whose metadata was never fetched cannot be auto-included.

    Both content axes come back True from the title alone -- so *without* this
    rule the record would be `included`. `metadata_complete=False` overrides
    that: the screen refuses to judge on data it knows is partial, and defers.
    Asserting the axes are True is the point; it proves the deferral came from
    the completeness rule and not from weak content evidence.
    """
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
    """A reviewer can include what automation left pending -- with receipts.

    The record is automatically undecidable (empty pubtype). A *complete*
    override -- decision, reviewer, timestamp, evidence, both axes -- promotes
    it to `included`, flips `screening_method` to `manual-override` so the
    provenance is visible, and preserves the reviewer's stated evidence in the
    trail rather than replacing the automated findings.
    """
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
    """An override cannot launder a decision past the evidence bar.

    This one asks for `included` but supplies only a reviewer name: no
    timestamp, no evidence, no axis verdicts. It falls back to `pending`
    instead of being honoured. Manual inclusion meets exactly the same bar as
    automatic inclusion -- otherwise the override file becomes a way to publish
    anything.
    """
    decision = screen_candidate(
        {"uid": "10", "title": "Integrated exposome signatures", "pubtype": []},
        query_arms=["core"],
        override={"decision": "included", "reviewer": "AB"},
    )

    assert decision.decision == "pending"
    assert "manual inclusion requires" in decision.exclusion_reasons[-1]


def test_durable_override_loader_requires_reviewer_timestamp_and_evidence(tmp_path):
    """The override file is validated at load time, not at use time.

    The valid file's bare numeric key is normalised to `PMC9`. The invalid one
    omits `reviewed_at`, and loading *raises* rather than quietly accepting an
    unattributable decision. Failing loudly at startup is the point: a
    malformed override file must not silently reshape the corpus later.
    """
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
    """Publication reads only the `included` bucket, so the split must be clean.

    One included and one pending record go in; each must come out in its own
    bucket with `excluded` empty. The input order is deliberately reversed
    (pending first) to show the partition keys off the decision rather than off
    arrival order.
    """
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
    """Age is a facet, never a gate -- the property #52 established.

    An adult study and a childhood study, identical on every other axis, must
    reach the same decision. If the screen ever starts preferring one age
    stratum, this fails. Cohort composition is reported per paper via
    `cohort_type` from full text, and is not decided here.
    """
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
