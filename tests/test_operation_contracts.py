from dataclasses import FrozenInstanceError
import pytest
from culsma_opentrons.operations import (
    OperationTranslator,
    TransferOperationTranslator,
    ResolvedContainerRef,
    TransferAliquot,
    TransferIntent,
)


def test_transfer_protocol_specializes_operation_protocol():
    assert OperationTranslator in TransferOperationTranslator.__mro__
    assert TransferOperationTranslator.prepare is OperationTranslator.prepare
    assert TransferOperationTranslator._is_protocol


@pytest.mark.parametrize("volume", [0, -1, float("nan"), float("inf"), True, "10"])
def test_invalid_resolved_volume(volume):
    with pytest.raises(ValueError):
        TransferAliquot(ResolvedContainerRef("protocol::source"), volume)


def test_operation_contracts():
    ref = ResolvedContainerRef("protocol::source")
    with pytest.raises(TypeError):
        TransferAliquot("author_variable", 10)
    with pytest.raises(TypeError):
        TransferIntent("s1", ref, [TransferAliquot(ref, 10)])
    with pytest.raises(TypeError):
        TransferIntent("s1", ref, (TransferAliquot(ref, 10),), requirements=["sterile"])
    intent = TransferIntent("s1", ref, (TransferAliquot(ref, 10),))
    with pytest.raises(FrozenInstanceError):
        intent.step_id = "changed"


def test_plugin_does_not_depend_on_other_devices():
    from pathlib import Path
    import ast
    import culsma_opentrons

    for path in Path(culsma_opentrons.__file__).resolve().parent.rglob("*.py"):
        tree = ast.parse(path.read_text())
        imports = [
            n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
        ]
        imports += [
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        ]
        assert not any(
            m.startswith(
                ("culsma_driver_hamilton", "culsma.driver.operations", "culsma_os")
            )
            for m in imports
        ), path
