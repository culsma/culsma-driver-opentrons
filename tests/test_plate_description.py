import json
from pathlib import Path

from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.models import WellAddress
from culsma_opentrons.assembly import SimulatorSessionAssembler


class TestPlateDescription:
    def test_session_describes_96well_geometry(self):
        root = Path(__file__).resolve().parents[1]
        raw = json.loads((root / "examples/deck.json").read_text())
        raw["labware"]["plate"] = {
            "load_name": "corning_96_wellplate_360ul_flat",
            "slot": "3",
        }
        session = SimulatorSessionAssembler().build(None, DeckConfiguration.from_dict(raw))
        try:
            description = session.describe_session()
            assert description.labware_geometries["plate"] == (8, 12)
            assert description.labware_geometries["tubes"] == (4, 6)
            assert description.capacities["plate", "A1"] == 360
            assert description.capacities["plate", "H12"] == 360
            assert sum(name == "plate" for name, _ in description.capacities) == 96
            assert session.backend.well(WellAddress("plate", "H12")).well_name == "H12"
            assert not session.backend.protocol.commands()
        finally:
            session.close()
