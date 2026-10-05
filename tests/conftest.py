from pathlib import Path

import pytest

from exposome_ehr.config import load_vocabulary
from exposome_ehr.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def vocab():
    return load_vocabulary()


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "data")
    yield s
    s.close()


@pytest.fixture(scope="session")
def sample_xml() -> bytes:
    return (FIXTURES / "pubmed_sample.xml").read_bytes()
