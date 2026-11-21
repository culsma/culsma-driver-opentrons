import ast
from copy import deepcopy
import json
from pathlib import Path

import pytest

from culsma_opentrons.config import DeckConfiguration

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("change", ["same_well", "same_slot", "unknown_labware", "bad_slot", "unknown_key", "wrong_robot"])
def test_config_validation(change):
    raw = json.loads((ROOT / "examples/deck.json").read_text())
    if change == "same_well":
        raw["bindings"]["entry.s1::alloc"] = deepcopy(raw["bindings"]["entry.s0::alloc"])
    elif change == "same_slot":
        raw["tiprack"]["slot"] = "1"
    elif change == "unknown_labware":
        raw["bindings"]["entry.s0::alloc"]["labware"] = "absent"
    elif change == "bad_slot":
        raw["tiprack"]["slot"] = "12"
    elif change == "unknown_key":
        raw["guess_names"] = True
    elif change == "wrong_robot":
        raw["robot_type"] = "Flex"
    with pytest.raises(ValueError):
        DeckConfiguration.from_dict(raw)


def test_architecture_boundaries():
    source = ROOT / "src/culsma_opentrons"
    for path in source.glob("*.py"):
        tree = ast.parse(path.read_text())
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        assert not any("hardware_control" in name or name.startswith(("requests", "httpx", "serial", "socket")) for name in imports)
        assert not any("repository" in name for name in imports) or path.name == "assembly.py"
        if path.name != "backend.py":
            # Static labware definitions in opentrons_shared_data do not import the runtime.
            assert not any(name == "opentrons" or name.startswith("opentrons.") for name in imports)
        if path.name == "cli.py":
            assert not any(name.startswith(("culsma.", "opentrons")) for name in imports)
            assert not any(name in {"backend", "binding", "driver", "config"} for name in imports)
        assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("_")
                       and not node.name.startswith("__") for node in ast.walk(tree))
    # The language core never imports this vendor plugin.
    import culsma
    core = Path(culsma.__file__).resolve().parent
    if core.exists():
        for path in core.rglob("*.py"):
            assert "culsma_opentrons" not in path.read_text()
