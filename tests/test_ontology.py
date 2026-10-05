"""Concept resolution. Offline: the fixture bundle stands in for ATHENA."""

from pathlib import Path

import pytest

from exposome_ehr.ontology import (
    AthenaBundleResolver,
    Candidate,
    OntologyBuilder,
    classify_label_match,
)

BUNDLE = Path(__file__).parent / "fixtures" / "athena"


# ------------------------------------------------------------- match quality
@pytest.mark.parametrize(
    "term,name,expected",
    [
        ("tilt table test", "Tilt table test", "exact"),
        # SNOMED's parenthesised semantic tag is not a difference.
        ("gastroparesis", "Gastroparesis (disorder)", "exact"),
        # British spelling is not a difference.
        ("small fibre neuropathy", "Small fiber neuropathy (disorder)", "exact"),
        ("hypovolaemia", "Hypovolemia (finding)", "exact"),
        ("Valsalva manoeuvre", "Valsalva maneuver (qualifier value)", "exact"),
        # LOINC interleaves units and specimen; every search word is still there.
        ("norepinephrine plasma", "Norepinephrine [Moles/volume] in Plasma --supine", "contains"),
        ("metanephrine plasma", "Metanephrines [Mass/volume] in Serum or Plasma", "contains"),
        ("acetylcholine receptor antibody", "Acetylcholine receptor Ab [Presence] in Serum", "contains"),
        # A qualifier the search never asked for still counts as contains, which
        # is why `contains` is not a licence to skip review.
        ("heart rate variability", "Fetal heart rate variability", "contains"),
        # "therapy" and "value" carry real meaning, so they are NOT dropped as
        # noise — a qualifier the concept name adds must not collapse to `exact`.
        ("sweat measure", "Sweat therapy measure value", "contains"),
        # A genuinely different concept.
        ("epinephrine plasma", "Catecholamines [Mass/volume] in Plasma", "loose"),
        ("immune globulin", "immunoglobulin G, human", "loose"),
        ("intestinal dysmotility", "Congenital dysmotility of small intestine", "loose"),
        ("anything", None, "unknown"),
    ],
)
def test_classify_label_match(term, name, expected):
    assert classify_label_match(term, name) == expected


# ------------------------------------------------------------- athena bundle
def test_bundle_availability():
    assert AthenaBundleResolver(BUNDLE).available()
    assert not AthenaBundleResolver(BUNDLE / "missing").available()


def test_bundle_exact_match_ranks_first():
    resolver = AthenaBundleResolver(BUNDLE)
    candidates = resolver.search("SNOMED", "Tachycardia", limit=5)
    assert candidates[0].concept_name == "Tachycardia"
    assert candidates[0].concept_code == "3424008"
    assert candidates[0].concept_id == 314665
    assert candidates[0].rank == 1


def test_bundle_returns_real_concept_ids_and_domains():
    resolver = AthenaBundleResolver(BUNDLE)
    candidate = resolver.search("LOINC", "Norepinephrine", limit=1)[0]
    assert candidate.concept_id == 3013707
    assert candidate.domain_id == "Measurement"
    assert candidate.vocabulary_id == "LOINC"


def test_bundle_filters_by_vocabulary():
    resolver = AthenaBundleResolver(BUNDLE)
    assert resolver.search("RxNorm", "midodrine", limit=5)
    assert resolver.search("SNOMED", "midodrine", limit=5) == []


def test_bundle_surfaces_invalid_reason():
    resolver = AthenaBundleResolver(BUNDLE)
    candidate = resolver.search("SNOMED", "Deprecated autonomic concept", limit=1)[0]
    assert candidate.invalid_reason == "D"
    assert candidate.standard_concept is None


def test_bundle_parents_include_is_a_and_part_of():
    resolver = AthenaBundleResolver(BUNDLE)
    pots = resolver.search("SNOMED", "Postural orthostatic tachycardia syndrome", limit=1)[0]
    edges = resolver.parents(pots)
    by_rel = {(e.relationship_id, e.concept_code_2) for e in edges}
    assert ("Is a", "870368003") in by_rel
    assert ("Is a", "3424008") in by_rel

    tilt = resolver.search("SNOMED", "Tilt table test", limit=1)[0]
    tilt_edges = {(e.relationship_id, e.concept_code_2) for e in resolver.parents(tilt)}
    assert ("Part of", "371073003") in tilt_edges


def test_bundle_ignores_unrelated_relationship_types():
    resolver = AthenaBundleResolver(BUNDLE)
    oi = resolver.search("SNOMED", "Orthostatic intolerance", limit=1)[0]
    assert {e.relationship_id for e in resolver.parents(oi)} == {"Subsumes"}


# ------------------------------------------------------------------ pipeline
class _StubResolver:
    """Answers for SNOMED only, so LOINC and RxNorm terms fall through."""

    name = "stub"
    vocabularies = frozenset({"SNOMED"})

    def available(self):
        return True

    def search(self, vocabulary_id, term, limit=5):
        return [
            Candidate(
                vocabulary_id="SNOMED",
                concept_code="123",
                concept_name=term,
                concept_id=999,
                domain_id="Condition",
                rank=1,
            )
        ]

    def parents(self, candidate):
        return []

    def ancestors(self, candidate):
        return []


def test_builder_writes_unreviewed_mappings_and_records_misses(store, vocab):
    builder = OntologyBuilder(store, vocab, use_fallbacks=False, try_athena_api=False)
    builder.resolvers = [_StubResolver()]
    report = builder.run()

    assert report.resolvers_used == ["stub"]
    assert report.facet_mappings > 0
    # Every SNOMED term resolved; every other vocabulary had no resolver at all.
    declared = {c["vocabulary"] for f in vocab.facet_list for c in f["concepts"]}
    assert {vocabulary for _f, vocabulary, _t in report.unresolved} == declared - {"SNOMED"}

    statuses = {
        row["mapping_status"] for row in store.rows("SELECT mapping_status FROM facet_concept")
    }
    assert statuses == {"unreviewed"}
    assert store.scalar(
        "SELECT COUNT(*) FROM resolver_attempt WHERE resolver = 'stub' AND status = 'ok'"
    ) > 0


def test_builder_leaves_the_curated_graph_untouched(store, vocab):
    builder = OntologyBuilder(store, vocab, use_fallbacks=False, try_athena_api=False)
    builder.resolvers = [_StubResolver()]
    builder.run()
    sources = {row["source"] for row in store.rows("SELECT DISTINCT source FROM ontology_edge")}
    assert sources == {"curated"}
