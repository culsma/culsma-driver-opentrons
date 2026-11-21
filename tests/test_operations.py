from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from culsma.frontend.resolver import resolve_files
from culsma.pipeline.compile import compile_ast
from culsma.pipeline.plan import lower_ir_to_plan
from culsma.pipeline.typecheck import typecheck
from culsma.pipeline.validate import validate
from culsma.runtime.executor import run
from culsma.runtime.state import init_state
from culsma_opentrons.backend import OfficialSimulatorBackend
from culsma_opentrons.binding import OpentronsBindingResolver
from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.assembly import build_driver
from culsma_opentrons.models import TransferOperation
from culsma_opentrons.policy import TransferPolicy
from culsma_opentrons.support import validate_plan_support
from culsma_opentrons.translator import plan_strokes

ROOT = Path(__file__).resolve().parents[1]


def compile_example(name):
    path = ROOT / 'examples' / name / 'protocol.culs'
    compiled = compile_ast(resolve_files([path]).prepared_program)
    validated = validate(compiled.ir, analysis=compiled.analysis, enforce_binding=True)
    typed = typecheck(validated.ir, analysis=compiled.analysis)
    assert validated.ok and typed.ok
    plan = lower_ir_to_plan(typed.ir, analysis=compiled.analysis)
    assert not any(d.severity == 'error' for d in plan.diagnostics)
    return plan


def configuration(name):
    return DeckConfiguration.from_dict(json.loads((ROOT / 'examples' / name / 'deck.json').read_text()))




def prepare_fixture(name, backend):
    from dataclasses import replace
    full_plan = compile_example(name)
    setup = replace(full_plan, plans=[replace(p, steps=[s for s in p.steps if s.op != 'Mutation']) for p in full_plan.plans])
    state = init_state(setup)
    bindings = OpentronsBindingResolver(state, configuration(name), backend)
    driver = build_driver(bindings)
    assert run(setup, driver, state).ok
    step = next(s for p in full_plan.plans for s in p.steps if s.op == 'Mutation')
    return driver, step


def test_preparation_has_no_actions(config, backend):
    driver, step = prepare_fixture('multi-source', backend)
    before = deepcopy(driver.bindings.state.artifacts)
    request = driver.mapper.from_step(step)
    operation = driver.transfers.prepare_transfer(request).operation
    assert isinstance(operation, TransferOperation)
    assert len(operation.commands) == 2
    assert not backend.protocol.commands()
    assert driver.bindings.state.artifacts == before


@pytest.mark.parametrize('raw', [ {'tip_policy':'reuse'}, {'stroke_mode':'automatic'},
    {'aspirate_flow_rate_uL_s':0}, {'dispense_flow_rate_uL_s':float('nan')},
    {'aspirate_flow_rate_uL_s':True}, {'unknown':1} ])
def test_policy_validation(raw):
    with pytest.raises(ValueError):
        TransferPolicy.from_dict(raw)


@pytest.mark.parametrize('volume,expected', [(300,[300]),(310,[155,155]),(610,[610/3]*3)])
def test_split_conservation(volume, expected):
    strokes = plan_strokes(volume,20,300,'balanced_split')
    assert strokes == pytest.approx(expected)
    assert sum(strokes) == pytest.approx(volume)
    assert all(20 <= value <= 300 for value in strokes)


def test_stroke_policy():
    with pytest.raises(ValueError, match='Aliquot'):
        plan_strokes(310,20,300,'single')
    with pytest.raises(ValueError):
        plan_strokes(.5,1,20,'balanced_split')


def test_pipette_rack_pair():
    raw = json.loads((ROOT / 'examples/deck.json').read_text())
    raw['pipette'] = 'p20_single_gen2'
    with pytest.raises(ValueError, match='matching pair'):
        DeckConfiguration.from_dict(raw)


@pytest.mark.parametrize('failed_aspirate,returned', [(1,1),(2,5)])
def test_partial_failure_evidence(failed_aspirate, returned, monkeypatch):
    plan = compile_example('multi-source')
    config = configuration('multi-source')
    backend = OfficialSimulatorBackend(config)
    try:
        count = 0
        original = backend.pipette.aspirate
        def aspirate(volume, location):
            nonlocal count
            count += 1
            if count == failed_aspirate:
                raise RuntimeError('partial failure probe')
            return original(volume, location)
        monkeypatch.setattr(backend.pipette, 'aspirate', aspirate)
        state = init_state(plan)
        driver = build_driver(OpentronsBindingResolver(state,config,backend))
        result = run(plan,driver,state)
        assert not result.ok
        assert sorted(c['volume_uL'] for c in state.artifacts['material_state']['containers'].values()) == [0,200,300]
        payload = next(e.payload['driver_payload'] for e in result.events if e.kind == 'STEP_FAILED')
        assert payload['retry_safe'] is False
        assert payload['operation_ok'] is False
        statuses = [a['status'] for a in payload['action_results']]
        assert statuses.count('returned') == returned
        assert statuses.count('completion_unknown') == 1
        assert statuses.count('not_started') == 7 - returned
        assert driver.check(next(s for s in plan.plans[0].steps if s.op == "Mutation")).code == "DRV_OT_SESSION_FAILED"
        assert not backend.receipts
        previous = list(backend.protocol.commands())
        with pytest.raises(ValueError, match='Simulator session failed'):
            driver.transfers.prepare_transfer(driver.mapper.from_step(next(s for s in plan.plans[0].steps if s.op == 'Mutation')))
        assert backend.protocol.commands() == previous
    finally:
        backend.close()




def test_effective_config_roundtrip(config):
    snapshot = config.snapshot()
    reloaded = DeckConfiguration.from_dict(snapshot)
    assert reloaded.fingerprint() == config.fingerprint()


def test_fingerprint_tracks_policy(config):
    snapshot = config.snapshot()
    snapshot['transfer_policy']['stroke_mode'] = 'balanced_split'
    assert DeckConfiguration.from_dict(snapshot).fingerprint() != config.fingerprint()
