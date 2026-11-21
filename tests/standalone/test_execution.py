from dataclasses import replace
import json
import pytest
from culsma.cli import execute_pipeline
from culsma_opentrons.standalone import StandaloneProgramExecutor, StandaloneProgramExecutorFactory
from culsma_opentrons.motion.interactors import LabwareMotionInteractor
from .conftest import RecordingSessions


def test_factory():
    assert isinstance(StandaloneProgramExecutorFactory().build(), StandaloneProgramExecutor)


def test_independent_program_and_material_authority(scenario):
    sessions = RecordingSessions()
    executor = StandaloneProgramExecutor(sessions)
    result = executor.execute(scenario)
    assert result['summary']['runtime_ok'], result['run']['diagnostics']
    baseline = execute_pipeline(list(scenario.input_paths))
    assert result['results'] == baseline['results']
    receipts = result['run']['state']['artifacts']['device_execution']['receipts']
    mixing = next(r for r in receipts if r['operation_kind'] == 'pipette_mix')
    assert mixing['mixed_volume_uL'] == 80 and mixing['material_net_transfer_uL'] == 0
    assert [a['action'] for a in mixing['action_results']] == ['pick_up_tip', 'aspirate', 'dispense', 'drop_tip']
    assert all(a['status'] == 'returned' for a in mixing['action_results'])
    assert len([r for r in receipts if r['operation_kind'] == 'labware_move']) == 3
    hold = next(r for r in receipts if r['operation_kind'] == 'hold')['environment']
    assert hold['request'] == {'temperature_celsius': 37, 'duration_seconds': 120, 'co2_percent': 5}
    assert hold['evidence_scope'] == 'reference_environment_model' and not hold['co2_measured']
    centrifuge = next(r for r in receipts if r['operation_kind'] == 'centrifuge')
    assert centrifuge['request']['drive_value'] == 300
    assert centrifuge['cycle_settings']['drive_value'] == pytest.approx(1637.91865, rel=1e-5)
    assert centrifuge['cycle_settings']['temperature_celsius'] == 4
    transfer = next(r for r in receipts if r['operation_kind'] == 'transfer')
    assert transfer['selection']['producer_step_id'] == 'entry.s4.b.s0'
    assert transfer['environment']['request']['temperature_celsius'] == 20
    assert all(s.closed for s in sessions.created)
    assert json.loads((scenario.artifacts_dir / 'opentrons/evidence.json').read_text())['receipts'] == json.loads(json.dumps(receipts))
    with pytest.raises(ValueError, match='RUN_EXISTS'):
        executor.execute(scenario)


@pytest.mark.parametrize('change,code', [
    ('co2', 'ENVIRONMENT_UNSUPPORTED'), ('cold', 'TEMPERATURE_RANGE'),
    ('route', 'MOTION_LOCATION'), ('transfer_environment', 'ENVIRONMENT_UNSUPPORTED'),
    ('capacity', 'CAPACITY'), ('imbalance', 'IMBALANCE'),
])
def test_invalid_configuration_rejected_before_any_actions(scenario, change, code):
    raw = json.loads(scenario.config_path.read_text())
    if change == 'co2': raw['reference_environments']['incubator']['co2_range_percent']['maximum'] = 4
    if change == 'cold': raw['reference_devices']['rotor']['profile']['temperature_range_celsius']['minimum'] = 10
    if change == 'route': raw['labware_moves']['entry.s3'][0]['destination'] = 'D2'
    if change == 'transfer_environment': raw['reference_environments']['enclosure']['allow_transfer'] = False
    if change == 'capacity':
        scenario.input_paths[0].write_text(scenario.input_paths[0].read_text().replace('capacity=2mL', 'capacity=3mL'))
    if change == 'imbalance': raw['reference_devices']['rotor']['counterweight']['mass_grams'] = 110
    scenario.config_path.write_text(json.dumps(raw))
    sessions = RecordingSessions()
    result = StandaloneProgramExecutor(sessions).execute(scenario)
    assert not result['summary']['runtime_ok']
    assert result['summary']['device_execution_started'] is False
    failed = [h for h in result['run']['state']['history'] if h['status'] == 'failed']
    assert code in failed[0]['driver_code']
    assert not sessions.created[0].backend.receipts
    assert not sessions.created[0].motion.receipts
    assert not (scenario.artifacts_dir / 'opentrons/execution-started.json').exists()


def test_device_fault_stops_and_does_not_commit_current_material(scenario, monkeypatch):
    from culsma_opentrons.backend import OfficialSimulatorBackend
    original = OfficialSimulatorBackend.run_prepared

    def fail_transfer(self, operation):
        # First same-well mix succeeds; supernatant transfer loses confirmation.
        if operation.commands[0].source != operation.commands[0].target:
            raise RuntimeError('injected transfer confirmation loss')
        return original(self, operation)

    monkeypatch.setattr(OfficialSimulatorBackend, 'run_prepared', fail_transfer)
    result = StandaloneProgramExecutor().execute(scenario)
    assert not result['summary']['runtime_ok']
    failed = [h for h in result['run']['state']['history'] if h['status'] == 'failed']
    assert failed[0]['step_id'] == 'entry.s5.b.s0'
    failure = next(e for e in result['run']['events'] if e['kind'] == 'STEP_FAILED')
    assert failure['payload']['driver_payload']['retry_safe'] is False
    containers = result['run']['state']['artifacts']['material_state']['containers']
    assert sorted(c['volume_uL'] for c in containers.values()) == [0, 100]
    assert (scenario.artifacts_dir / 'opentrons/evidence.json').exists()


def test_first_effect_failure_prevents_later_device_actions(scenario, monkeypatch):
    from culsma_opentrons.backend import OfficialSimulatorBackend
    calls = []

    def fail_first(self, operation):
        calls.append(operation.step_id)
        raise RuntimeError('injected SDK failure')

    monkeypatch.setattr(OfficialSimulatorBackend, 'run_prepared', fail_first)
    sessions = RecordingSessions()
    result = StandaloneProgramExecutor(sessions).execute(scenario)
    assert not result['summary']['runtime_ok']
    assert calls == ['entry.s2']
    assert not sessions.created[0].motion.receipts
    assert result['run']['state']['step_status']['entry.s3'] == 'skipped'
    fault = next(e for e in result['run']['events'] if e['kind'] == 'STEP_FAILED')['payload']['driver_payload']['faults'][0]
    assert fault['operation_id'] == 'entry.s2'
    assert fault['native_details']['message'] == 'injected SDK failure'
