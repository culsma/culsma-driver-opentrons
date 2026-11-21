"""Method requirements travel from CULS through preflight to device evidence."""
import json
import pytest
from culsma_opentrons.standalone import StandaloneProgramExecutor
from .conftest import RecordingSessions


REQUIREMENTS = 'constraint(ConstraintRequirement.LOW_CARRYOVER, ConstraintRequirement.HIGH_PRECISION)'


def configure_precision(scenario):
    raw = json.loads(scenario.config_path.read_text())
    raw['transfer_policy'] = {'precision_method': {
        'method_id': 'configured-flex-method', 'revision': '1', 'pipette': raw['pipette'],
        'minimum_volume_uL': 20, 'maximum_volume_uL': 1000,
        'aspirate_flow_rate_uL_s': 25, 'dispense_flow_rate_uL_s': 40}}
    scenario.config_path.write_text(json.dumps(raw))
    return raw


@pytest.mark.parametrize('constrain_mix,constrain_transfer', [(True, False), (False, True), (True, True)])
def test_constraints_reach_methods_and_receipts(scenario, constrain_mix, constrain_transfer):
    raw = configure_precision(scenario)
    program = scenario.input_paths[0].read_text()
    if constrain_mix:
        program = program.replace('sample << [sample:80uL];', f'sample << [sample:80uL] with {REQUIREMENTS};')
    if constrain_transfer:
        program = program.replace('output << [sample.contents[0]];', f'output << [sample.contents[0]] with {REQUIREMENTS};')
        raw['labware_moves']['entry.s5.b.s0.b.s0'] = raw['labware_moves'].pop('entry.s5.b.s0')
        scenario.config_path.write_text(json.dumps(raw))
    scenario.input_paths[0].write_text(program)
    sessions = RecordingSessions()
    result = StandaloneProgramExecutor(sessions).execute(scenario)
    assert result['summary']['runtime_ok'], result['run']['diagnostics']
    assert result['summary']['device_preflight_ok']
    evidence = json.loads((scenario.artifacts_dir / 'opentrons/evidence.json').read_text())
    for kind, constrained in [('pipette_mix', constrain_mix), ('transfer', constrain_transfer)]:
        receipt = next(r for r in evidence['receipts'] if r['operation_kind'] == kind)
        if constrained:
            methods = {m['requirement']: m for m in receipt['constraint_resolution']}
            assert set(methods) == {'low_carryover', 'high_precision'}
            assert methods['low_carryover']['method_id'] == 'fresh-tip-per-stroke'
            assert methods['high_precision']['method_id'] == 'configured-flex-method'
            assert methods['high_precision']['parameters']['aspirate_flow_rate_uL_s'] == 25
            assert receipt['instrument']['aspirate_flow_rate_uL_s'] == 25
            assert receipt['instrument']['dispense_flow_rate_uL_s'] == 40
        else:
            assert not receipt.get('constraint_resolution')
            assert receipt['instrument']['aspirate_flow_rate_uL_s'] == sessions.created[0].backend.default_flow_rates[0]
            assert receipt['instrument']['dispense_flow_rate_uL_s'] == sessions.created[0].backend.default_flow_rates[1]
        assert [a['action'] for a in receipt['action_results']] == ['pick_up_tip', 'aspirate', 'dispense', 'drop_tip']
        assert all(a['status'] == 'returned' for a in receipt['action_results'])
        if kind == 'transfer':
            assert receipt['selection']['producer_step_id'] == 'entry.s4.b.s0'
    containers = result['run']['state']['artifacts']['material_state']['containers']
    assert sorted(c['volume_uL'] for c in containers.values()) == [0, 100]


@pytest.mark.parametrize('problem,expected_code', [
    ('missing_method', 'DRV_OT_CONSTRAINT_METHOD'), ('volume_range', 'DRV_OT_CONSTRAINT_METHOD'),
    ('wrong_pipette', 'DRV_OT_CONSTRAINT_METHOD'), ('unsupported', 'DRV_OT_REQUIREMENT_UNSUPPORTED'),
])
def test_invalid_constraint_rejected_before_any_actions(scenario, problem, expected_code):
    raw = configure_precision(scenario)
    requirement = REQUIREMENTS
    if problem == 'missing_method':
        raw['transfer_policy'].pop('precision_method')
    elif problem == 'volume_range':
        raw['transfer_policy']['precision_method']['maximum_volume_uL'] = 50
    elif problem == 'wrong_pipette':
        raw['transfer_policy']['precision_method']['pipette'] = 'p300_single_gen2'
    else:
        requirement = 'constraint(ConstraintRequirement.DARK_PROTECTED)'
    raw['labware_moves']['entry.s5.b.s0.b.s0'] = raw['labware_moves'].pop('entry.s5.b.s0')
    scenario.config_path.write_text(json.dumps(raw))
    # The constrained transfer comes after mixing, incubation, centrifugation and routes.
    # Whole-program preflight must still reject it before any of those device actions.
    program = scenario.input_paths[0].read_text().replace('output << [sample.contents[0]];',
        f'output << [sample.contents[0]] with {requirement};')
    scenario.input_paths[0].write_text(program)
    sessions = RecordingSessions()
    result = StandaloneProgramExecutor(sessions).execute(scenario)
    assert not result['summary']['runtime_ok']
    assert result['summary']['device_execution_started'] is False
    failure = next(e for e in result['run']['events'] if e['kind'] == 'STEP_FAILED')
    assert failure['step_id'] == 'entry.s5.b.s0.b.s0'
    assert failure['payload']['driver_code'] == expected_code
    assert not sessions.created[0].backend.receipts
    assert not sessions.created[0].motion.receipts
    assert not (scenario.artifacts_dir / 'opentrons/execution-started.json').exists()


def test_block_constraint_applies_to_mixing_and_plain_transfer(scenario):
    raw = configure_precision(scenario)
    raw['labware_moves'] = {}
    raw['reference_devices'] = {}
    scenario.config_path.write_text(json.dumps(raw))
    scenario.input_paths[0].write_text('''
let sample = well(capacity=2mL, load=[content(kind=formulation,type=buffer,code="B"):1500uL]);
let output = well(capacity=2mL);
with constraint(ConstraintRequirement.LOW_CARRYOVER, ConstraintRequirement.HIGH_PRECISION) {
  sample << [sample:80uL];
  output << [sample:1500uL];
}
''')
    raw['transfer_policy']['stroke_mode'] = 'balanced_split'
    # The aliquot exceeds this method range; its two actual 750 uL strokes fit.
    raw['transfer_policy']['precision_method']['maximum_volume_uL'] = 800
    scenario.config_path.write_text(json.dumps(raw))
    result = StandaloneProgramExecutor().execute(scenario)
    assert result['summary']['runtime_ok'], result['run']['diagnostics']
    receipts = result['run']['state']['artifacts']['device_execution']['receipts']
    transfer = next(r for r in receipts if r['operation_kind'] == 'transfer')
    assert [c['volume_uL'] for c in transfer['transfers']] == [750, 750]
    assert len(transfer['action_results']) == 8
    assert all(len(r['constraint_resolution']) == 2 for r in receipts if r['operation_kind'] in {'transfer', 'pipette_mix'})
