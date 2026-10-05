"""Vocabulary integrity. These run with no network and guard the curated files."""

import pytest

from exposome_ehr.config import CONCEPT_VOCABULARIES, Vocabulary, _find_cycles


def test_vocabulary_is_clean(vocab):
    assert vocab.validate() == []


def test_facet_ids_are_unique_and_snake_case(vocab):
    ids = vocab.facet_ids
    assert len(ids) == len(set(ids))
    for facet_id in ids:
        assert facet_id == facet_id.lower()
        assert " " not in facet_id


def test_all_six_facet_groups_are_populated(vocab):
    counts: dict[str, int] = {}
    for facet in vocab.facet_list:
        counts[facet["group"]] = counts.get(facet["group"], 0) + 1
    assert set(counts) == {"exposure", "outcome", "data_source", "assessment", "design", "population"}
    assert all(n > 0 for n in counts.values())


def test_age_strata_are_tags_with_nlm_check_tags(vocab):
    """Pediatric is a tag, so each age facet must carry NLM's age check tags."""
    population = {f["id"]: f for f in vocab.facet_list if f["group"] == "population"}
    assert set(population) == {"prenatal_window", "infant", "child", "adolescent", "adult"}
    assert "Child" in population["child"]["mesh_terms"]
    assert "Infant" in population["infant"]["mesh_terms"]
    assert "Adolescent" in population["adolescent"]["mesh_terms"]


def test_every_facet_has_a_taxonomy_parent(vocab):
    parents = {s for s, p, _o, _prov in vocab.edges if p == "is_a"}
    assert set(vocab.facet_ids) <= parents


def test_literature_predicates_define_their_questions(vocab):
    """judge_paragraphs.py reads domain, range, option and statement from here."""
    groups = set(vocab.groups)
    for name, spec in vocab.predicates.items():
        if name == "is_a":
            continue
        assert set(spec["domain"]) <= groups, name
        assert set(spec["range"]) <= groups, name
        assert spec["option"].strip(), name
        assert "{a}" in spec["statement"] and "{b}" in spec["statement"], name


def test_no_concept_codes_are_hand_asserted(vocab):
    for facet in vocab.facet_list:
        for concept in facet["concepts"]:
            assert set(concept) <= {"vocabulary", "search_term", "fallback_gap"}


def test_concept_vocabularies_are_known(vocab):
    for facet in vocab.facet_list:
        for concept in facet["concepts"]:
            assert concept["vocabulary"] in CONCEPT_VOCABULARIES


def test_every_facet_has_a_query_and_mesh_terms(vocab):
    for facet in vocab.facet_list:
        assert facet["pubmed_query"].strip()
        assert facet["mesh_terms"]
        assert facet["concepts"]


def test_query_profiles_compose(vocab):
    core = vocab.corpus_query("core")
    assert '"Exposome"[MeSH Terms]' in core
    # Every clause is parenthesised and the clauses are OR-ed together, so each
    # one appears verbatim inside the composed query.
    for block in vocab.profile_blocks("core"):
        assert f"({block['query']})" in core


def test_no_retrieval_filters(vocab):
    """Retrieve everything, decide later: no pediatric, OA or review filter."""
    assert vocab.query["filters"] == []
    core = vocab.corpus_query("core")
    for banned in ("open access[filter]", "Review[Publication Type]", '"pediatric"[Title/Abstract]'):
        assert banned not in core


def test_core_is_the_only_profile(vocab):
    # Narrower corpora are derived downstream from article_query_block, not
    # from a second query, so the file carries a single profile.
    assert list(vocab.query["profiles"]) == ["core"]
    ids = [b["id"] for b in vocab.profile_blocks("core")]
    assert ids == ["exposome", "ewas", "environment_in_health_records", "vaccine_as_exposure"]
    assert len(ids) == len(set(ids))


def test_facet_query_is_corpus_and_facet(vocab):
    query = vocab.facet_query("particulate_matter", "core")
    assert query.startswith("((")
    assert ") AND (" in query
    assert vocab.facet("particulate_matter")["pubmed_query"] in query


def test_unknown_profile_and_facet_raise(vocab):
    with pytest.raises(KeyError):
        vocab.profile("nope")
    with pytest.raises(KeyError):
        vocab.facet("nope")


def test_nodes_cover_every_edge_endpoint(vocab):
    nodes = set(vocab.nodes)
    for subject, _predicate, obj, _prov in vocab.edges:
        assert subject in nodes
        assert obj in nodes


def test_transitive_predicates_are_acyclic(vocab):
    transitive = {p for p, spec in vocab.predicates.items() if spec.get("transitive")}
    edges = [(s, o) for s, p, o, _ in vocab.edges if p in transitive]
    assert _find_cycles(edges) == []


def test_every_edge_predicate_is_declared(vocab):
    used = {p for _s, p, _o, _prov in vocab.edges}
    assert used <= set(vocab.predicates)


def test_cycle_detection_finds_a_planted_cycle():
    assert _find_cycles([("a", "b"), ("b", "c"), ("c", "a")])


def test_validate_reports_a_dangling_edge(vocab):
    broken = Vocabulary(
        query=vocab.query,
        facets=vocab.facets,
        relations={
            **vocab.relations,
            "relations": vocab.relations["relations"] + [["ghost", "is_a", "exposome_axis", "test"]],
        },
    )
    problems = broken.validate()
    assert any("ghost" in problem for problem in problems)


def test_validate_reports_an_unknown_predicate(vocab):
    broken = Vocabulary(
        query=vocab.query,
        facets=vocab.facets,
        relations={
            **vocab.relations,
            "relations": vocab.relations["relations"] + [["lead", "causes", "asthma_wheeze", "test"]],
        },
    )
    assert any("causes" in problem for problem in broken.validate())


# --- regression: findings from review on #7 ---------------------------------
def _with_relations(vocab, rows):
    return Vocabulary(
        query=vocab.query,
        facets=vocab.facets,
        relations={**vocab.relations, "relations": vocab.relations["relations"] + rows},
    )


def _with_concept(vocab, **overrides):
    import copy

    facets = copy.deepcopy(vocab.facets)
    facets["facets"][0]["concepts"][0].update(overrides)
    return Vocabulary(query=vocab.query, facets=facets, relations=vocab.relations)


def test_validate_reports_a_short_edge_row(vocab):
    """A forgotten provenance field must be reported, not raised."""
    problems = _with_relations(vocab, [["lead", "risk_factor_for", "neurodevelopment"]]).validate()
    assert any("expected 4" in p and "3 elements" in p for p in problems)


def test_validate_reports_a_long_edge_row(vocab):
    """An unquoted comma in provenance splits the flow sequence into five."""
    import yaml

    row = yaml.safe_load(
        "r:\n  - [particulate_matter, risk_factor_for, asthma_wheeze,"
        " hu2023: EHR, linked to census data]\n"
    )["r"][0]
    assert len(row) == 5, "the unquoted-comma case must really produce five elements"
    problems = _with_relations(vocab, [row]).validate()
    assert any("expected 4" in p and "5 elements" in p for p in problems)


def test_edges_skips_malformed_rows_instead_of_raising(vocab):
    broken = _with_relations(vocab, [["a", "is_a"], ["b", "is_a", "c", "prov", "extra"]])
    assert len(broken.edges) == len(vocab.edges)
    assert [index for index, _row in broken.malformed_edges] == [
        len(vocab.relations["relations"]),
        len(vocab.relations["relations"]) + 1,
    ]


def test_validate_rejects_an_unknown_vocabulary(vocab):
    """The runtime gate must be as strict as the test suite: SNMED is a typo."""
    problems = _with_concept(vocab, vocabulary="SNMED").validate()
    assert any("unknown vocabulary" in p and "SNMED" in p for p in problems)


def test_validate_accepts_every_allowed_vocabulary(vocab):
    for name in CONCEPT_VOCABULARIES:
        assert _with_concept(vocab, vocabulary=name).validate() == []
