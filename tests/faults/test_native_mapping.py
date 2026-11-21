"""Generate errors through the official simulator, not invented message matching."""
import pytest
from opentrons import simulate
from culsma_opentrons.faults import OpentronsFaultMappingInteractor


def test_official_simulator_errors_are_unwrapped_with_distinct_guidance_reasons():
    ctx=simulate.get_protocol_api('2.20')
    rack=ctx.load_labware('opentrons_96_tiprack_300ul','1')
    plate=ctx.load_labware('corning_96_wellplate_360ul_flat','2')
    pipette=ctx.load_instrument('p300_single_gen2','right',tip_racks=[rack])
    mapper=OpentronsFaultMappingInteractor(schema_version=2)
    with pytest.raises(Exception) as missing:pipette.aspirate(10,plate['A1'])
    assert mapper.preparation(missing.value,'s')[0]['reason']=='culsma.tip.not_attached'
    pipette.pick_up_tip()
    with pytest.raises(Exception) as attached:pipette.pick_up_tip()
    assert mapper.preparation(attached.value,'s')[0]['reason']=='culsma.tip.already_attached'
    with pytest.raises(Exception) as capacity:pipette.aspirate(301,plate['A1'])
    assert mapper.preparation(capacity.value,'s')[0]['reason']=='culsma.transfer.capacity_exceeded'


def test_unknown_native_identity_never_guesses_from_message():
    mapper=OpentronsFaultMappingInteractor(schema_version=2)
    assert mapper.preparation(ValueError('tip rack empty liquid not found'),'s')[0]['reason']=='culsma.device.unclassified'
    assert 'schema' not in OpentronsFaultMappingInteractor().preparation(ValueError(),'s')[0]


def test_unconfirmed_completion_preserves_original_exception_and_schema_negotiation():
    mapper = OpentronsFaultMappingInteractor(schema_version=2)
    fault = mapper.completion_fault(
        TimeoutError("no completion confirmation"), "move/1", "labware_move",
        {"module": "rotor", "planned_location": "module:rotor"},
    )
    assert fault == {
        "schema": "culsma_device_fault_v2",
        "code": "DEVICE_ERROR",
        "reason": "culsma.operation.completion_unconfirmed",
        "operation_id": "move/1",
        "source": "simulation",
        "execution_state": "unknown",
        "basis": "model_state",
        "location": None,
        "context": {"module": "rotor", "planned_location": "module:rotor"},
        "stop_observation": {"state": "unknown"},
        "native_details": {
            "code": "DRV_OT_COMPLETION_UNCONFIRMED",
            "operation_kind": "labware_move",
            "exception_type": "TimeoutError",
            "exception_code": "TimeoutError",
            "message": "no completion confirmation",
        },
    }
    receipt = {"outcome": "unknown", "step_id": "move/1", "payload": {"faults": [fault]}}
    legacy = mapper.with_fault_schema(1).normalize(receipt)[0]
    assert set(legacy) == {"code", "source", "execution_state", "operation_id", "location", "native_details"}
    assert legacy["native_details"] == fault["native_details"]
    assert mapper.normalize(receipt) == [fault]
    assert fault["schema"] == "culsma_device_fault_v2"
    mapper.normalize(receipt)[0]["native_details"].clear()
    assert fault["native_details"]["exception_type"] == "TimeoutError"
