import os
from pathlib import Path
import tempfile

import pytest

pytest.importorskip("culsma_opentrons", reason="Install the isolated simulator package to run its tests")

os.environ["OT_API_CONFIG_DIR"] = tempfile.mkdtemp(prefix="culsma-ot-tests-")

from culsma_opentrons.backend import OfficialSimulatorBackend
from culsma_opentrons.config import DeckConfiguration

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config():
    import json
    return DeckConfiguration.from_dict(json.loads((ROOT / "examples/deck.json").read_text()))


@pytest.fixture
def backend(config):
    instance = OfficialSimulatorBackend(config)
    yield instance
    instance.close()
