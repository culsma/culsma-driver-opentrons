from copy import deepcopy
import json
from pathlib import Path

import pytest

from culsma.pipeline.plan_nodes import PlanStep
from culsma.runtime.executor import run
from culsma.runtime.state import init_state

from culsma_opentrons.binding import OpentronsBindingResolver
from culsma_opentrons.assembly import build_driver
from culsma_opentrons.models import ContainerHandle
from culsma_opentrons.translator import volume_in_uL

ROOT = Path(__file__).resolve().parents[1]


def plan():
    from culsma.frontend.resolver import resolve_files
    from culsma.pipeline.compile import compile_ast
    from culsma.pipeline.validate import validate
    from culsma.pipeline.typecheck import typecheck
    from culsma.pipeline.plan import lower_ir_to_plan
    compiled = compile_ast(resolve_files([ROOT / "examples/transfer.culs"]).prepared_program)
    validated = validate(compiled.ir, analysis=compiled.analysis, enforce_binding=True)
    typed = typecheck(validated.ir, analysis=compiled.analysis)
    assert validated.ok and typed.ok
    return lower_ir_to_plan(typed.ir, analysis=compiled.analysis)


def run_with_backend(config, backend):
    compiled = plan()
    state = init_state(compiled)
    bindings = OpentronsBindingResolver(state, config, backend)
    driver = build_driver(bindings)
    result = run(plan=compiled, driver=driver, state=state)
    return result, driver


def test_language_pipeline():
    compiled = plan()
    assert sum(s.op == "Mutation" for p in compiled.plans for s in p.steps) == 2


def test_alias_identity(config, backend):
    result, driver = run_with_backend(config, backend)
    assert result.ok, [d.to_dict() for d in result.diagnostics]
    source1 = backend.receipts[0].to_payload()["transfers"][0]["source"]["id"]
    source2 = backend.receipts[1].to_payload()["transfers"][0]["source"]["id"]
    assert source1 == source2
    assert ContainerHandle(source1) in driver.bindings.addresses
    assert backend.receipts[0].to_payload()["transfers"][0]["source_address"]["well"] == "A1"


def test_material_conservation(config, backend):
    result, _ = run_with_backend(config, backend)
    assert result.ok
    containers = result.state.artifacts["material_state"]["containers"]
    assert sorted(c["volume_uL"] for c in containers.values()) == [100, 100]
    assert sum(c["components"]["BUFFER"] for c in containers.values()) == 200
    assert all(receipt.to_payload()["mode"] == "simulation" for receipt in backend.receipts)


def test_tip_sequence(config, backend):
    result, _ = run_with_backend(config, backend)
    assert result.ok
    for receipt in backend.receipts:
        commands = receipt.to_payload()["commands"]
        assert len(commands) == 4
        assert commands[0].startswith("Picking up tip")
        assert commands[1].startswith("Aspirating 50.0 uL from A1")
        assert commands[2].startswith("Dispensing 50.0 uL into A2")
        assert commands[3].startswith("Dropping tip")
    assert "from A1" in backend.receipts[0].to_payload()["commands"][0]
    assert "from B1" in backend.receipts[1].to_payload()["commands"][0]


def test_failure_does_not_commit(config, backend, monkeypatch):
    def fail(volume, location):
        raise RuntimeError("injected virtual aspiration failure")
    monkeypatch.setattr(backend.pipette, "aspirate", fail)
    result, _ = run_with_backend(config, backend)
    assert not result.ok
    assert "RT_DRIVER_ERROR" in [d.code for d in result.diagnostics]
    containers = result.state.artifacts["material_state"]["containers"]
    assert sorted(c["volume_uL"] for c in containers.values()) == [0, 200]
    assert not backend.receipts
    assert backend.poisoned


@pytest.mark.parametrize("unit,value,expected", [("uL", 50, 50), ("mL", .05, 50), ("L", .00005, 50)])
def test_volume_units(unit, value, expected):
    assert volume_in_uL({"kind": "IRQuantity", "value": value, "unit": unit}) == pytest.approx(expected)


@pytest.mark.parametrize("unit,value", [("cells", 50), ("ug", 50), ("%", 50), ("uL", -1), ("uL", float("nan")), ("uL", True)])
def test_unsupported_quantity(unit, value):
    with pytest.raises(ValueError):
        volume_in_uL({"kind": "IRQuantity", "value": value, "unit": unit})


@pytest.mark.parametrize("op,gate,code", [
    ("sep", None, "DRV_OT_OP_UNSUPPORTED"),
    ("Mutation", {"constraint": {"requirements": ["gentle"]}}, "DRV_REQ_UNSUPPORTED"),
    ("Mutation", {"constraint": {"options": {"unknown_option": 1}}}, "DRV_REQ_OPTION_UNSUPPORTED"),
    ("Mutation", {"env": {"temperature": 37}}, "DRV_OT_GATE_UNSUPPORTED"),
])
def test_capability_rejections(config, backend, op, gate, code):
    driver = build_driver(OpentronsBindingResolver(init_state(plan()), config, backend))
    step = PlanStep("bad", op, gate=gate)
    assert driver.check(step).code == code
    assert not driver.execute(step).ok
    assert not backend.protocol.commands()


def test_unresolved_reference(config, backend):
    driver = build_driver(OpentronsBindingResolver(init_state(plan()), config, backend))
    step = PlanStep("bad", "Mutation", {"target": {"kind": "IRIdentifier", "name": "not_allocated"},
                   "sources": [{"kind": "IRPair", "left": {}, "right": {"kind": "IRQuantity", "value": 50, "unit": "uL"}}]})
    assert driver.check(step).code == "DRV_OT_REFERENCE_UNRESOLVED"
    assert not backend.protocol.commands()


def test_capacity_mismatch(config, backend):
    driver = build_driver(OpentronsBindingResolver(init_state(plan()), config, backend))
    allocation = deepcopy(plan().plans[0].steps[0])
    allocation.args["capacity"]["value"] = 3000
    assert driver.check(allocation).code == "DRV_OT_CAPACITY_MISMATCH"


def test_tip_exhaustion(config, backend):
    result, driver = run_with_backend(config, backend)
    assert result.ok
    command = driver.transfers.prepare_transfer(driver.mapper.from_step(next(s for p in plan().plans for s in p.steps if s.op == "Mutation")))
    # Two tips used by the initial run. Exhaust the remaining 94 through official API.
    for _ in range(94):
        backend.pipette.pick_up_tip()
        backend.pipette.drop_tip()
    receipt = backend.run_prepared(command.operation)
    assert not receipt.ok and receipt.failure.exception_type == "OutOfTipsError"
    from culsma_opentrons.faults import OpentronsFaultMappingInteractor
    faults=OpentronsFaultMappingInteractor().normalize({'outcome':'unknown',
        'step_id':receipt.step_id,'payload':receipt.to_payload()})
    assert faults[0]['code']=='TIP_UNAVAILABLE'
    assert faults[0]['source']=='simulation' and faults[0]['execution_state']=='unknown'
    assert faults[0]['native_details']['code']=='OutOfTipsError'
    assert backend.poisoned
