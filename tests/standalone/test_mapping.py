from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import pytest
from culsma.cli import execute_pipeline
from culsma.driver.stub import StubDriver
from culsma.pipeline.plan_nodes import PlanStep
from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.models import DriverValidationError
from culsma_opentrons.standalone.mapping import ProgramOperationMapper, quantity, environment_request, method_requirements
from culsma_opentrons.standalone import StandaloneProgramExecutor
from .conftest import workstation


@pytest.fixture
def snapshots(scenario):
    values = {}

    @contextmanager
    def factory(plan, state):
        values['plan'] = plan
        class Recorder(StubDriver):
            def execute(self, step):
                values[step.step_id] = (step, deepcopy(state))
                return super().execute(step)
        yield Recorder()

    assert execute_pipeline(list(scenario.input_paths), driver_factory=factory)['summary']['runtime_ok']
    return values


def test_quantity_and_environment_mapping():
    assert quantity({'kind': 'IRQuantity', 'value': 2, 'unit': 'min'}, {'min': 60}) == 120
    with pytest.raises(DriverValidationError): quantity({'kind': 'IRIdentifier', 'name': 'unknown'}, {'min': 60})
    step = PlanStep('hold', 'env_hold', gate={'env': {'thermal': {'kind': 'IRQuantity', 'value': 20, 'unit': 'C'},
        'duration': {'kind': 'IRQuantity', 'value': 2, 'unit': 'h'}}})
    assert environment_request(step, hold=True).duration_seconds == 7200
    with pytest.raises(DriverValidationError): environment_request(step)
    assert environment_request(PlanStep('plain', 'Mutation')) is None


@pytest.mark.parametrize('step_id,kind', [('entry.s0::alloc','setup'), ('entry.s2','mix'),
    ('entry.s3','hold'), ('entry.s4.b.s0','centrifuge'), ('entry.s5.b.s0','transfer')])
def test_each_operation_maps_without_changing_core_state(snapshots, step_id, kind):
    step, state = snapshots[step_id]
    before = deepcopy(state.artifacts)
    mapper = ProgramOperationMapper(DeckConfiguration.from_dict(workstation()), state)
    result = mapper.map(step)
    assert result.kind == kind
    assert state.artifacts == before
    if kind == 'setup': assert len(result.bindings) == 1
    if kind == 'mix':
        assert result.request.volume_uL == 80 and result.request.available_uL == 100
        assert mapper.resolve(step.args['target']) == result.request.target.id
        assert len(mapper.bindings()) == 2
    if kind == 'transfer':
        assert result.request.aliquots[0].volume_uL == 100
        assert result.producer_step_id == 'entry.s4.b.s0'
        assert result.environment.temperature_celsius == 20
        assert mapper.mutation(step, mapper.compute.apply_step(step, mapper.material).delta, mapper.bindings()) == result


def test_mapper_rejects_unconsumed_fields_and_stale_selection(snapshots):
    step, state = snapshots['entry.s5.b.s0']
    mapper = ProgramOperationMapper(DeckConfiguration.from_dict(workstation()), state)
    with pytest.raises(DriverValidationError, match='Unconsumed'):
        mapper.map(replace(step, args={**step.args, 'unsupported': 1}))
    with pytest.raises(DriverValidationError): mapper.resolve({'kind': 'IRIdentifier', 'name': 'absent'})
    for record in mapper.material['contents_states'].values(): record['program_kind'] = 'magnetic_program'
    with pytest.raises(DriverValidationError): mapper.map(step)


def test_plan_bindings_and_routes_are_exact(snapshots):
    executor = StandaloneProgramExecutor()
    config = DeckConfiguration.from_dict(workstation())
    executor.validate_plan(snapshots['plan'], config)
    with pytest.raises(ValueError, match='allocation'):
        executor.validate_plan(snapshots['plan'], replace(config, bindings={}))
    with pytest.raises(ValueError, match='routes'):
        executor.validate_plan(snapshots['plan'], replace(config, labware_moves={'entry.s0::alloc': []}))


def test_scalar_mapper_rejects_multichannel(snapshots):
    config = DeckConfiguration.from_dict(workstation())
    with pytest.raises(ValueError, match='single-channel'):
        StandaloneProgramExecutor().validate_plan(snapshots['plan'], replace(config, pipette='p300_multi_gen2'))


def test_method_requirements_preserve_order_and_reject_unconsumed_constraints():
    step = PlanStep('transfer', 'Mutation', gate={'constraint': {
        'requirements': ['low_carryover', 'high_precision', 'low_carryover'], 'options': {}}})
    assert method_requirements(step) == ('low_carryover', 'high_precision')
    assert method_requirements(replace(step, gate=None)) == ()
    with pytest.raises(DriverValidationError, match='sep'):
        method_requirements(replace(step, op='sep'))


@pytest.mark.parametrize('constraint', [
    {'requirements': ['dark_protected']}, {'requirements': ['high_precision'], 'options': {'tolerance': 1}},
    {'requirements': 'low_carryover'}, {'requirements': [True]}, {'requirements': ['']},
    {'options': []}, {'unknown': 1}, {'requirement_types': {'low_carryover': {'family': 'custom'}}}, [],
])
def test_method_requirements_reject_unsupported_or_malformed_values(constraint):
    with pytest.raises(DriverValidationError):
        method_requirements(PlanStep('transfer', 'Mutation', gate={'constraint': constraint}))


@pytest.mark.parametrize('step_id', ['entry.s2', 'entry.s5.b.s0'])
def test_mapper_forwards_method_requirements_for_mixing_and_supernatant(snapshots, step_id):
    step, state = snapshots[step_id]
    step = replace(step, gate={**step.gate, 'constraint': {'requirements': ['low_carryover', 'high_precision']}})
    before = deepcopy(state.artifacts)
    mapper = ProgramOperationMapper(DeckConfiguration.from_dict(workstation()), state)
    result = mapper.map(step)
    assert result.request.requirements == ('low_carryover', 'high_precision')
    assert state.artifacts == before
    if result.kind == 'transfer':
        assert result.producer_step_id == 'entry.s4.b.s0'
