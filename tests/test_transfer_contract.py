from dataclasses import replace
from copy import deepcopy
import math
import pytest
from culsma_opentrons.models import DriverValidationError, PreparedTransfer, TransferAliquot, TransferReceipt, TransferRequest, WellAddress
from culsma_opentrons.transfer import OpentronsTransferDriver
from .test_operations import prepare_fixture


def test_mapper_contract(backend):
    driver, step = prepare_fixture('multi-source', backend)
    request = driver.mapper.from_step(step)
    assert isinstance(request, TransferRequest)
    assert all(isinstance(a, TransferAliquot) for a in request.aliquots)
    assert [a.volume_uL for a in request.aliquots] == [50, 100]
    assert request.target_address.well == 'A3'
    assert request.span == step.span
    assert not backend.protocol.commands()


def test_typed_transfer_interface(backend):
    driver, step = prepare_fixture('multi-source', backend)
    request = driver.mapper.from_step(step)
    assert driver.transfers.check_transfer(request).ok
    prepared = driver.transfers.prepare_transfer(request)
    operation = prepared.operation
    assert len(operation.commands) == 2
    assert not backend.protocol.commands()
    receipt = driver.transfers.execute_transfer(prepared)
    assert isinstance(receipt, TransferReceipt)
    assert receipt.ok and receipt.phase == 'simulated'
    assert len(receipt.action_results) == 8
    assert all(a.status == 'returned' for a in receipt.action_results)
    # Direct device execution never commits language material state.
    containers = driver.bindings.state.artifacts['material_state']['containers']
    assert sorted(c['volume_uL'] for c in containers.values()) == [0,200,300]


def test_stale_binding(backend):
    driver, step = prepare_fixture('multi-source', backend)
    request = driver.mapper.from_step(step)
    prepared = driver.transfers.prepare_transfer(request)
    driver.bindings.addresses[request.target] = WellAddress('tubes','A4')
    receipt = driver.transfers.execute_transfer(prepared)
    assert receipt.phase == 'rejected' and receipt.code == 'DRV_OT_BINDING_STALE'
    assert not receipt.action_results and not backend.protocol.commands()


@pytest.mark.parametrize('volume', [0,-1,float('inf'),float('nan'),True])
def test_invalid_request(backend, volume):
    driver, step = prepare_fixture('multi-source', backend)
    request = driver.mapper.from_step(step)
    request = replace(request, aliquots=(replace(request.aliquots[0], volume_uL=volume),))
    with pytest.raises(DriverValidationError) as rejected:
        driver.transfers.prepare_transfer(request)
    assert rejected.value.code == 'DRV_OT_VOLUME_RANGE'
    assert not backend.protocol.commands()
    assert not backend.poisoned


def test_tip_preflight(backend, monkeypatch):
    driver, step = prepare_fixture('multi-source', backend)
    request = driver.mapper.from_step(step)
    prepared = driver.transfers.prepare_transfer(request)
    monkeypatch.setattr(backend, 'available_tips', lambda: 1)
    before = deepcopy(driver.bindings.state.artifacts)
    receipt = driver.transfers.execute_transfer(prepared)
    assert receipt.phase == 'rejected' and receipt.code == 'DRV_OT_TIPS_INSUFFICIENT'
    assert not receipt.action_results and not backend.protocol.commands()
    assert driver.bindings.state.artifacts == before


def test_attached_tip(backend):
    driver, step = prepare_fixture('multi-source', backend)
    request = driver.mapper.from_step(step)
    prepared = driver.transfers.prepare_transfer(request)
    backend.pipette.pick_up_tip()
    before = list(backend.protocol.commands())
    receipt = driver.transfers.execute_transfer(prepared)
    assert receipt.phase == 'rejected' and receipt.code == 'DRV_OT_TIP_ATTACHED'
    assert backend.protocol.commands() == before


@pytest.mark.parametrize('action,returned', [('pick_up_tip',0),('aspirate',1),('dispense',2),('drop_tip',3)])
def test_stroke_failures(backend, monkeypatch, action, returned):
    driver, step = prepare_fixture('multi-source', backend)
    request = driver.mapper.from_step(step)
    prepared = driver.transfers.prepare_transfer(request)
    def fail(*args):
        raise RuntimeError('action failure probe')
    monkeypatch.setattr(backend.pipette, action, fail)
    receipt = driver.transfers.execute_transfer(prepared)
    assert receipt.phase == 'failed' and not receipt.ok
    assert receipt.failure.exception_type == 'RuntimeError'
    statuses = [a.status for a in receipt.action_results]
    assert statuses.count('returned') == returned
    assert statuses.count('completion_unknown') == 1
    assert statuses.count('not_started') == 7-returned
    assert backend.poisoned
    before = list(backend.protocol.commands())
    second = driver.transfers.execute_transfer(prepared)
    assert second.phase == 'rejected' and not second.action_results
    assert backend.protocol.commands() == before


def test_receipt_bridge(backend):
    driver, step = prepare_fixture('multi-source', backend)
    receipt = driver.transfers.execute_transfer(driver.transfers.prepare_transfer(driver.mapper.from_step(step)))
    payload = receipt.to_payload()
    assert payload['schema'] == 'opentrons_transfer_receipt_v1'
    assert payload['phase'] == 'simulated' and payload['operation_ok']
    assert len(payload['transfers']) == 2 and len(payload['commands']) == 8
    assert payload['span'] and payload['config_sha256']


@pytest.mark.parametrize('gate', [{'env':{'temperature':37}}, {'constraint':{'requirements':['gentle']}}, {'constraint':{'requirement_types':['unsupported']}}])
def test_direct_gate_rejection(backend, gate):
    driver, step = prepare_fixture('multi-source', backend)
    with pytest.raises(ValueError):
        driver.mapper.from_step(replace(step, gate=gate))
    assert not backend.protocol.commands()


def test_transfer_uses_only_declared_executor_contract(backend):
    driver, step = prepare_fixture('multi-source', backend)
    request = driver.mapper.from_step(step)
    planned = driver.transfers.prepare_transfer(request).operation
    receipt = backend.build_receipt(planned,0,'simulated')
    calls = []
    class Translator:
        def prepare_transfer(self, request):
            calls.append('prepare')
            return planned
    class Executor:
        def check_ready(self):
            calls.append('ready')
        def reject_transfer(self, request, failure):
            return replace(receipt, phase='rejected', failure=failure)
        def run_prepared(self, operation):
            calls.append('execute')
            return receipt
    service = OpentronsTransferDriver(Translator(), Executor())
    assert service.check_transfer(request).ok
    assert service.execute_transfer(service.prepare_transfer(request)) is receipt
    assert calls == ['ready','prepare','ready','prepare','ready','prepare','execute']
    assert not backend.protocol.commands()


def test_transfer_contract_boundaries():
    import ast
    from pathlib import Path
    import culsma_opentrons
    root = Path(culsma_opentrons.__file__).parent
    for filename, forbidden in [('transfer.py', {'backend','binding','request_mapper','translator'}),
                                 ('driver.py', {'translator','transfer','backend'}),
                                 ('request_mapper.py', {'backend'})]:
        tree = ast.parse((root / filename).read_text())
        imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not imports & forbidden


def test_receipt_payload_does_not_mutate_evidence(backend):
    driver, step = prepare_fixture('multi-source', backend)
    receipt = driver.transfers.execute_transfer(driver.transfers.prepare_transfer(driver.mapper.from_step(step)))
    original = receipt.effective_config['transfer_policy']['tip_policy']
    payload = receipt.to_payload()
    payload['effective_config']['transfer_policy']['tip_policy'] = 'modified'
    assert receipt.effective_config['transfer_policy']['tip_policy'] == original


def test_preparation_cannot_be_replayed_or_used_by_another_session(backend):
    driver, step = prepare_fixture('multi-source', backend)
    prepared = driver.transfers.prepare_transfer(driver.mapper.from_step(step))
    other = OpentronsTransferDriver(driver.transfers.translator, backend)
    rejected = other.execute_transfer(prepared)
    assert rejected.code == 'DRV_OT_PREPARATION_INVALID'
    assert not backend.protocol.commands()
    assert driver.transfers.execute_transfer(prepared).ok
    before = list(backend.protocol.commands())
    assert driver.transfers.execute_transfer(prepared).code == 'DRV_OT_PREPARATION_INVALID'
    assert backend.protocol.commands() == before


def test_modified_preparation_rejected_without_consuming_original(backend):
    driver, step = prepare_fixture('multi-source', backend)
    prepared = driver.transfers.prepare_transfer(driver.mapper.from_step(step))
    modified = replace(prepared, operation=replace(prepared.operation, commands=()))
    assert driver.transfers.execute_transfer(modified).code == 'DRV_OT_PREPARATION_INVALID'
    assert not backend.protocol.commands()
    assert driver.transfers.execute_transfer(prepared).ok


def test_changed_plan_is_not_silently_executed(backend, monkeypatch):
    driver, step = prepare_fixture('multi-source', backend)
    prepared = driver.transfers.prepare_transfer(driver.mapper.from_step(step))
    changed = replace(prepared.operation, commands=prepared.operation.commands[:1])
    monkeypatch.setattr(driver.transfers.translator, 'prepare_transfer', lambda request: changed)
    assert driver.transfers.execute_transfer(prepared).code == 'DRV_OT_PREPARATION_STALE'
    assert not backend.protocol.commands()
    assert driver.transfers.execute_transfer(prepared).code == 'DRV_OT_PREPARATION_INVALID'


def test_execution_requires_preparation_and_check_does_not_issue_one(backend):
    driver, step = prepare_fixture('multi-source', backend)
    request = driver.mapper.from_step(step)
    assert driver.transfers.check_transfer(request).ok
    assert not driver.transfers.preparations
    with pytest.raises(DriverValidationError, match='requires PreparedTransfer'):
        driver.transfers.execute_transfer(request)
    assert not backend.protocol.commands()
