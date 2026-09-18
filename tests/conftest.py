import shutil
from pathlib import Path

import pytest

FIXTURE_CRATE = Path(__file__).parent / "data" / "mini-crate"


@pytest.fixture
def crate_dir(tmp_path):
    """A writable copy of the fixture crate, so tests can create receipts freely."""
    destination = tmp_path / "mini-crate"
    shutil.copytree(FIXTURE_CRATE, destination)
    return destination


@pytest.fixture
def crate(crate_dir):
    from fairscape_publish.crate import load_crate

    return load_crate(crate_dir, validate=False)


@pytest.fixture
def payload(crate):
    from fairscape_publish.crate import discover_files
    from fairscape_publish.mapping.common import build_payload

    files, remote = discover_files(crate)
    return build_payload(crate, files=files, remote_entities=remote)
