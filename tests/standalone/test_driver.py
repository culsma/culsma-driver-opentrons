from dataclasses import replace
import pytest
from culsma.pipeline.plan_nodes import PlanStep
from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.models import DriverValidationError
from culsma_opentrons.plugin import SimulatorSessionFactory
from culsma_opentrons.standalone.driver import StandaloneDriver
from .conftest import workstation
from .test_mapping import snapshots


@pytest.fixture
def driver(snapshots, tmp_path):
    step, state = snapshots['entry.s2']
    config = DeckConfiguration.from_dict(workstation())
    session = SimulatorSessionFactory().create(config, tmp_path)
    try:
        yield StandaloneDriver(state, config, session), step
    finally:
        session.close()


def test_driver_check_validate_and_execution_are_separate(driver):
    instance, step = driver
    mapped = instance.mapper.map(step)
    assert instance.validate(step, mapped, instance.positions) == 1
    assert instance.check(step).ok
    assert not instance.session.backend.receipts
    result = instance.execute(step)
    assert result.ok and result.payload['operation_kind'] == 'pipette_mix'
    assert not instance.execute(step).ok
    with pytest.raises(DriverValidationError, match='accessible'):
        instance.validate(step, mapped, {**instance.positions, 'sample': 'module:rotor'})
    assert not instance.check(replace(step, op='image')).ok


def test_pipette_commands_validate_actual_strokes_without_effects(driver):
    from culsma_opentrons.operations import TransferIntent, TransferAliquot
    instance, step = driver
    mixing = instance.mapper.map(step)
    commands = instance.pipette_commands(mixing)
    assert len(commands) == 1 and commands[0].volume_uL == 80
    assert commands[0].source == commands[0].target
    identities = [b.reference for b in mixing.bindings]
    source, target = identities
    transfer = replace(mixing, kind='transfer', request=TransferIntent('large', target,
        (TransferAliquot(source, 1500),)), targets=(source.container_id, target.container_id))
    instance.config = replace(instance.config, transfer_policy=replace(instance.config.transfer_policy, stroke_mode='balanced_split'))
    commands = instance.pipette_commands(transfer)
    assert [c.volume_uL for c in commands] == [750, 750]
    assert commands[0].source.id == source.container_id and commands[0].target.id == target.container_id
    assert commands[0].stroke_id != commands[1].stroke_id
    assert not instance.session.backend.receipts
    with pytest.raises(DriverValidationError):
        instance.pipette_commands(replace(mixing, kind='hold'))


def test_projected_routes_do_not_move_sdk_labware(driver):
    instance, step = driver
    assert instance.projected_positions('entry.s3')['sample'] == 'C1'
    assert instance.positions['sample'] == 'D1'
    assert instance.session.motion.positions()['sample'] == 'D1'
    assert instance.execute_routes('entry.s3') is None
    assert instance.session.motion.positions()['sample'] == 'C1'
    assert instance.receipts[0]['operation_kind'] == 'labware_move'


def test_direct_operation_receipt_and_private_projection(driver):
    instance, step = driver
    operation = instance.mapper.map(step)
    result = instance.execute_operation(step, operation)
    assert result.ok and result.payload['mixed_volume_uL'] == 80
    assert instance.mapper.material['containers'][operation.targets[0]]['volume_uL'] == 100
    # A dry run updates only the projected driver state, without further SDK effects.
    preview = StandaloneDriver(instance.state, instance.config, instance.session, preflight=True)
    before = len(instance.session.backend.receipts)
    assert preview.check(step).ok and preview.execute(step).ok
    assert preview.tips_required == 1 and len(instance.session.backend.receipts) == before
