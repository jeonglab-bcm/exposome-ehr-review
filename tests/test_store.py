"""Storage layer: schema, upserts, the transitive closure and the views."""

import json

from exposome_ehr.parse import parse_efetch_response


def test_vocabulary_snapshot_lands_in_the_database(store, vocab):
    store.upsert_vocabulary(vocab)
    assert store.scalar("SELECT COUNT(*) FROM facet") == len(vocab.facet_ids)
    assert store.scalar("SELECT COUNT(*) FROM facet_group") == len(vocab.groups)
    assert store.scalar("SELECT COUNT(*) FROM ontology_node") == len(vocab.nodes)
    assert store.scalar("SELECT COUNT(*) FROM ontology_edge") == len(vocab.edges)
    assert store.scalar("SELECT COUNT(*) FROM ontology_predicate") == len(vocab.predicates)
    assert store.scalar("SELECT COUNT(*) FROM seed_reference") == len(vocab.seed_references)


def test_vocabulary_snapshot_is_idempotent(store, vocab):
    store.upsert_vocabulary(vocab)
    before = store.scalar("SELECT COUNT(*) FROM ontology_edge")
    store.upsert_vocabulary(vocab)
    assert store.scalar("SELECT COUNT(*) FROM ontology_edge") == before


def test_snapshot_preserves_seed_resolution(store, vocab):
    store.upsert_vocabulary(vocab)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE seed_reference SET resolved_pmid = '111', match_method = 'title_exact'"
            " WHERE ref_id = 'correa2022'"
        )
    store.upsert_vocabulary(vocab)
    row = store.rows("SELECT resolved_pmid, match_method FROM seed_reference WHERE ref_id='correa2022'")[0]
    assert row["resolved_pmid"] == "111"
    assert row["match_method"] == "title_exact"


def test_is_a_closure_is_transitive(store, vocab):
    store.upsert_vocabulary(vocab)
    ancestors = {
        row["ancestor_id"]
        for row in store.rows(
            "SELECT ancestor_id FROM ontology_closure"
            " WHERE predicate = 'is_a' AND descendant_id = 'residential_geocoding'"
        )
    }
    assert {"area_level_assessment", "exposure_assessment", "exposome_axis"} <= ancestors


def test_closure_records_shortest_path_length(store, vocab):
    store.upsert_vocabulary(vocab)
    levels = {
        row["ancestor_id"]: row["min_levels"]
        for row in store.rows(
            "SELECT ancestor_id, min_levels FROM ontology_closure"
            " WHERE predicate = 'is_a' AND descendant_id = 'residential_geocoding'"
        )
    }
    assert levels["area_level_assessment"] == 1
    assert levels["exposure_assessment"] == 2
    assert levels["exposome_axis"] == 3


def _with_part_of(vocab):
    """The live vocabulary has one transitive predicate; add a second to test closure."""
    from exposome_ehr.config import Vocabulary

    relations = dict(vocab.relations)
    relations["predicates"] = {**vocab.predicates,
                               "part_of": {"transitive": True, "subsumption": False, "inverse": "has_part"}}
    relations["relations"] = vocab.relations["relations"] + [
        ["residential_geocoding", "part_of", "spatial_model", "test"],
        ["spatial_model", "part_of", "monitoring_station", "test"],
    ]
    return Vocabulary(query=vocab.query, facets=vocab.facets, relations=relations)


def test_a_second_transitive_predicate_is_closed_separately_from_is_a(store, vocab):
    store.upsert_vocabulary(_with_part_of(vocab))
    part_of = {
        row["descendant_id"]
        for row in store.rows(
            "SELECT descendant_id FROM ontology_closure"
            " WHERE predicate = 'part_of' AND ancestor_id = 'monitoring_station'"
        )
    }
    assert part_of == {"spatial_model", "residential_geocoding"}
    # part_of must not leak into the is_a closure.
    assert not store.rows(
        "SELECT 1 FROM ontology_closure"
        " WHERE predicate = 'is_a' AND ancestor_id = 'monitoring_station'"
    )


def test_non_transitive_predicates_have_no_closure(store, vocab):
    store.upsert_vocabulary(vocab)
    predicates = {
        row["predicate"] for row in store.rows("SELECT DISTINCT predicate FROM ontology_closure")
    }
    assert predicates == {"is_a"}


def test_article_upsert_populates_child_tables(store, vocab, sample_xml):
    store.upsert_vocabulary(vocab)
    records = parse_efetch_response(sample_xml)
    assert store.upsert_articles(records, "run_test") == 2

    assert store.scalar("SELECT COUNT(*) FROM article") == 2
    assert store.scalar("SELECT COUNT(*) FROM article_mesh WHERE pmid = '99900001'") == 4
    assert store.scalar("SELECT COUNT(*) FROM article_author WHERE pmid = '99900001'") == 2
    assert store.scalar("SELECT COUNT(*) FROM article_keyword WHERE pmid = '99900001'") == 2
    assert store.scalar("SELECT COUNT(*) FROM article_grant WHERE pmid = '99900001'") == 1
    assert store.scalar("SELECT COUNT(*) FROM article_reference WHERE pmid = '99900001'") == 1
    assert store.scalar("SELECT COUNT(*) FROM article_abstract_section WHERE pmid = '99900001'") == 2
    assert store.scalar("SELECT COUNT(*) FROM article_language WHERE pmid = '99900002'") == 1


def test_qualifierless_mesh_heading_stores_empty_string_key(store, vocab, sample_xml):
    store.upsert_vocabulary(vocab)
    store.upsert_articles(parse_efetch_response(sample_xml), "run_test")
    row = store.rows(
        "SELECT qualifier_ui, has_qualifier FROM article_mesh"
        " WHERE pmid = '99900001' AND descriptor_name = 'Humans'"
    )[0]
    assert row["qualifier_ui"] == ""
    assert row["has_qualifier"] == 0


def test_reupserting_an_article_does_not_duplicate_children(store, vocab, sample_xml):
    store.upsert_vocabulary(vocab)
    records = parse_efetch_response(sample_xml)
    store.upsert_articles(records, "run_one")
    store.upsert_articles(records, "run_two")
    assert store.scalar("SELECT COUNT(*) FROM article") == 2
    assert store.scalar("SELECT COUNT(*) FROM article_mesh WHERE pmid = '99900001'") == 4
    assert store.scalar("SELECT COUNT(*) FROM article_grant WHERE pmid = '99900001'") == 1
    # first_seen_run is not overwritten by a later run.
    assert store.scalar("SELECT first_seen_run FROM article WHERE pmid = '99900001'") == "run_one"


def test_reupsert_refreshes_scalar_fields_but_keeps_first_seen_run(store, vocab, sample_xml):
    """A re-harvest must update scalar metadata to match the latest fetch.

    PubMed corrects metadata and promotes ahead-of-print records to a real
    pub_year. Since the child tables are fully rebuilt on re-upsert, the scalar
    columns must move with them; only first_seen_run is immutable provenance.
    """
    store.upsert_vocabulary(vocab)
    records = parse_efetch_response(sample_xml)
    store.upsert_articles(records, "run_one")

    corrected = [dict(r) for r in records]
    corrected[0]["pub_year"] = 2020
    corrected[0]["journal_iso"] = "J New"
    corrected[0]["volume"] = "99"
    store.upsert_articles(corrected, "run_two")

    row = store.rows(
        "SELECT pub_year, journal_iso, volume, first_seen_run"
        " FROM article WHERE pmid = '99900001'"
    )[0]
    assert row["pub_year"] == 2020
    assert row["journal_iso"] == "J New"
    assert row["volume"] == "99"
    # Provenance of first sighting is preserved.
    assert row["first_seen_run"] == "run_one"


def test_retraction_flag_is_stored(store, vocab, sample_xml):
    store.upsert_vocabulary(vocab)
    store.upsert_articles(parse_efetch_response(sample_xml), "run_test")
    assert store.scalar("SELECT is_retracted FROM article WHERE pmid = '99900002'") == 1
    assert store.scalar("SELECT is_retracted FROM article WHERE pmid = '99900001'") == 0
    flags = json.loads(
        store.scalar("SELECT integrity_flags FROM article WHERE pmid = '99900001'")
    )
    assert flags == ["has_erratum"]


def test_jsonl_roundtrip(store, vocab, sample_xml):
    records = parse_efetch_response(sample_xml)
    store.append_jsonl(records)
    reloaded = list(store.iter_jsonl())
    assert [r["pmid"] for r in reloaded] == [r["pmid"] for r in records]
    assert reloaded[0]["mesh_headings"] == records[0]["mesh_headings"]


def test_raw_xml_is_cached_gzipped(store, sample_xml):
    import gzip

    path = store.write_raw("run_test", 0, sample_xml)
    assert path.exists()
    with gzip.open(path, "rb") as fh:
        assert fh.read() == sample_xml


def test_facet_summary_view_counts_both_evidence_paths(store, vocab, sample_xml):
    store.upsert_vocabulary(vocab)
    store.upsert_articles(parse_efetch_response(sample_xml), "run_test")
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO article_facet(pmid, facet_id, evidence_source, detail)"
            " VALUES ('99900001', 'particulate_matter', 'pubmed_query', 'run_test')"
        )
        conn.execute(
            "INSERT INTO article_facet(pmid, facet_id, evidence_source, detail)"
            " VALUES ('99900002', 'particulate_matter', 'mesh_term', 'Particulate Matter')"
        )
    row = store.rows("SELECT * FROM v_facet_summary WHERE facet_id = 'particulate_matter'")[0]
    assert row["n_query_hits"] == 1
    assert row["n_mesh_hits"] == 1
    assert row["n_articles_any"] == 2


def test_exposure_outcome_cooccurrence_view(store, vocab, sample_xml):
    store.upsert_vocabulary(vocab)
    store.upsert_articles(parse_efetch_response(sample_xml), "run_test")
    with store.transaction() as conn:
        for pmid, facet in [("99900001", "particulate_matter"), ("99900001", "asthma_wheeze"),
                            ("99900002", "particulate_matter"), ("99900002", "child")]:
            conn.execute(
                "INSERT INTO article_facet(pmid, facet_id, evidence_source) VALUES (?,?, 'pubmed_query')",
                (pmid, facet),
            )
    rows = [tuple(r) for r in store.rows("SELECT * FROM v_exposure_outcome_cooccurrence")]
    # A population tag is neither an exposure nor an outcome.
    assert rows == [("particulate_matter", "asthma_wheeze", 1)]


def test_age_strata_view_derives_pediatric(store, vocab, sample_xml):
    store.upsert_vocabulary(vocab)
    store.upsert_articles(parse_efetch_response(sample_xml), "run_test")
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO article_facet(pmid, facet_id, evidence_source) VALUES ('99900001', 'infant', 'mesh_term')"
        )
    flags = {r["pmid"]: (r["infant"], r["pediatric"]) for r in store.rows("SELECT * FROM v_article_age_strata")}
    assert flags == {"99900001": (1, 1), "99900002": (0, 0)}


def test_finish_run_only_accepts_known_fields(store, vocab):
    store.start_run("run_x", "core", "query", "0.1.0")
    store.finish_run("run_x", articles_stored=5, bogus_column=1)
    row = store.rows("SELECT * FROM harvest_run WHERE run_id = 'run_x'")[0]
    assert row["articles_stored"] == 5
    assert row["finished_at"]


# ------------------------------------------------------- JSONL compaction
def test_compact_deduplicates_by_pmid_keeping_the_last_record(store, vocab, sample_xml):
    from exposome_ehr.parse import parse_efetch_response

    records = parse_efetch_response(sample_xml)
    store.append_jsonl(records)
    revised = [{**records[0], "title": "Revised title"}]
    store.append_jsonl(revised)

    kept, dropped = store.compact_jsonl()
    assert (kept, dropped) == (2, 1)
    reloaded = {r["pmid"]: r for r in store.iter_jsonl()}
    assert reloaded["99900001"]["title"] == "Revised title"


def test_compact_sorts_numerically_by_pmid(store, sample_xml):
    from exposome_ehr.parse import parse_efetch_response

    records = parse_efetch_response(sample_xml)
    # Deliberately out of order, and with a PMID that sorts differently as text.
    store.append_jsonl([{**records[0], "pmid": "9990001"}])
    store.append_jsonl([{**records[0], "pmid": "111"}])
    store.append_jsonl([{**records[0], "pmid": "88800002"}])
    store.compact_jsonl()
    assert [r["pmid"] for r in store.iter_jsonl()] == ["111", "9990001", "88800002"]


def test_compact_is_idempotent(store, sample_xml):
    from exposome_ehr.parse import parse_efetch_response

    store.append_jsonl(parse_efetch_response(sample_xml))
    store.compact_jsonl()
    first = store.corpus_text()
    assert store.compact_jsonl() == (2, 0)
    assert store.corpus_text() == first


def test_compact_leaves_no_temporary_file(store, sample_xml):
    from exposome_ehr.parse import parse_efetch_response

    store.append_jsonl(parse_efetch_response(sample_xml))
    store.compact_jsonl()
    assert not list(store.data_dir.rglob("*.tmp"))
    # The staging file is gone once its records are in the shards.
    assert not store.jsonl_path.exists()
    assert [p.name for p in store.shard_paths()] == ["pmid_99.jsonl"]


def test_compact_on_a_missing_file_is_a_noop(store):
    assert store.compact_jsonl() == (0, 0)
    assert not store.jsonl_path.exists()



def test_an_edge_removed_from_the_config_leaves_the_database(store, vocab):
    from exposome_ehr.config import Vocabulary
    store.upsert_vocabulary(vocab)
    first = vocab.relations["relations"][0]
    smaller = Vocabulary(query=vocab.query, facets=vocab.facets, relations={
        **vocab.relations, "relations": [r for r in vocab.relations["relations"] if r is not first]})
    store.upsert_vocabulary(smaller)
    assert store.rows("SELECT 1 FROM ontology_edge WHERE subject_id = ? AND predicate = ? AND object_id = ?",
                      tuple(first[:3])) == []


def test_shards_split_by_million_pmids_and_drop_stale_files(store, sample_xml):
    from exposome_ehr.parse import parse_efetch_response

    record = parse_efetch_response(sample_xml)[0]
    store.append_jsonl([{**record, "pmid": "111"}, {**record, "pmid": "42000001"}])
    store.compact_jsonl()
    assert [p.name for p in store.shard_paths()] == ["pmid_00.jsonl", "pmid_42.jsonl"]
    (store.shard_dir / "pmid_77.jsonl").write_text("")   # a shard no record maps to any more
    store.compact_jsonl()
    assert [p.name for p in store.shard_paths()] == ["pmid_00.jsonl", "pmid_42.jsonl"]
