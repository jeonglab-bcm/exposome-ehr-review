"""CLI dispatch and exit codes. Offline commands only — nothing hits the network."""

import pytest

from exposome_ehr.cli import main
from exposome_ehr.parse import parse_efetch_response
from exposome_ehr.store import Store


@pytest.fixture()
def populated_data_dir(tmp_path, vocab, sample_xml):
    """A data dir with a vocabulary snapshot and the sample corpus loaded."""
    store = Store(tmp_path / "data")
    store.upsert_vocabulary(vocab)
    store.upsert_articles(parse_efetch_response(sample_xml), "run_test")
    store.close()
    return tmp_path / "data"


def test_validate_vocab_returns_zero(capsys):
    assert main(["validate-vocab"]) == 0
    assert "vocabulary clean" in capsys.readouterr().out


def test_show_query_prints_the_corpus_query(capsys):
    assert main(["show-query", "--profile", "core"]) == 0
    assert "[MeSH Terms]" in capsys.readouterr().out


def test_graph_unknown_node_exits_nonzero(populated_data_dir, capsys):
    rc = main(["--data-dir", str(populated_data_dir), "graph", "--node", "nonesuch"])
    assert rc == 1
    assert "unknown node" in capsys.readouterr().err


def test_graph_known_node_prints_edges_with_provenance(populated_data_dir, capsys):
    rc = main(["--data-dir", str(populated_data_dir), "graph", "--node", "particulate_matter"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "particulate_matter" in out
    assert "is_a" in out and "air_pollution" in out
    assert "curated:" in out


def test_rebuild_reloads_from_jsonl(tmp_path, vocab, sample_xml, capsys):
    # Seed only the JSONL layer, then rebuild the database from it, no network.
    store = Store(tmp_path / "data")
    store.append_jsonl(parse_efetch_response(sample_xml))
    store.close()
    rc = main(["--data-dir", str(tmp_path / "data"), "rebuild"])
    assert rc == 0
    assert "rebuilt 2 articles" in capsys.readouterr().out


def test_unknown_command_is_rejected():
    with pytest.raises(SystemExit):
        main(["no-such-command"])
