"""Flex standalone adapter, latch prerequisites and scalar processing ownership."""
from dataclasses import replace
import pytest
from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.plugin import SimulatorSessionFactory
from culsma_opentrons.motion.models import LabwareMoveIntent
from culsma_opentrons.models import DriverValidationError, SimulatorBinding, WellAddress
from culsma_opentrons.modules.models import TemperatureHoldIntent, OrbitalShakeIntent
from culsma_opentrons.operations import ResolvedContainerRef
from .test_module_docking import DockingFixture


class HeaterShakerDockingFixture(DockingFixture):
    def config(self):
        raw = super().config()
        raw['modules']['heater'] = {'model':'heaterShakerModuleV1','slot':'C1',
                                   'adapter':'opentrons_96_flat_bottom_adapter'}
        return raw

    def shake(self, session):
        a,b = ResolvedContainerRef('a'),ResolvedContainerRef('b')
        return session.prepare_orbital_shake(OrbitalShakeIntent('shake',(a,b),500,1),
            (SimulatorBinding(a,WellAddress('assay','A1')),SimulatorBinding(b,WellAddress('assay','A2'))))


def test_move_preparation_has_no_latch_actions(tmp_path):
    fixture = HeaterShakerDockingFixture()
    session = fixture.open(tmp_path)
    try:
        prepared = session.prepare_labware_move(LabwareMoveIntent('enter','assay','D1','module:heater',0))
        assert not session.backend.protocol.commands()
        assert session.describe_labware_move(prepared)['required_access'] == ['module:heater:open_labware_latch']
        receipt = session.execute_labware_move(prepared)
        assert receipt.outcome == 'completed'
        assert receipt.payload['completed_access'] == receipt.payload['required_access']
        assert receipt.payload['module_action_results'][0]['status'] == 'returned'
        assert receipt.payload['module_action_results'][0]['latch_position_measured'] is False
        assert session.backend.labware['assay'].parent is session.backend.module_adapters['heater']
        assert session.backend.protocol.commands()[0] == 'Unlatching labware on Heater-Shaker'
        assert 'opentrons_96_flat_bottom_adapter with gripper' in session.backend.protocol.commands()[1]
        assert session.execute_temperature_hold(fixture.hold(session)).outcome == 'completed'
        assert session.execute_orbital_shake(fixture.shake(session)).outcome == 'completed'
        assert fixture.leave(session).outcome == 'completed'
        assert not session.backend.module_access.active
    finally: session.close()


@pytest.mark.parametrize('direction',['enter','leave'])
@pytest.mark.parametrize('ownership',['commanded','observed-heat','observed-shake'])
@pytest.mark.parametrize('phase',['prepare','execute'])
def test_active_module_blocks_moves(tmp_path, monkeypatch, direction, ownership, phase):
    fixture = HeaterShakerDockingFixture()
    session = fixture.open(tmp_path)
    try:
        if direction == 'leave': fixture.enter(session)
        intent = (LabwareMoveIntent('enter','assay','D1','module:heater',0) if direction == 'enter'
                  else LabwareMoveIntent('leave','assay','module:heater','D1',1))
        if phase == 'execute': prepared = session.prepare_labware_move(intent)
        module = session.backend.modules['heater']
        if ownership == 'commanded': session.backend.module_access.begin_processing('heater','other')
        elif ownership == 'observed-heat': monkeypatch.setattr(type(module),'target_temperature',property(lambda self:37))
        else: monkeypatch.setattr(type(module),'target_speed',property(lambda self:500))
        before = list(session.backend.protocol.commands())
        with pytest.raises(DriverValidationError) as error:
            if phase == 'prepare': session.prepare_labware_move(intent)
            else: session.execute_labware_move(prepared)
        assert error.value.code == 'DRV_OT_MODULE_BUSY'
        assert session.backend.protocol.commands() == before
    finally: session.close()


@pytest.mark.parametrize('kind',['heat','shake'])
def test_stale_processing_after_move(tmp_path, kind):
    fixture = HeaterShakerDockingFixture()
    session = fixture.open(tmp_path)
    try:
        fixture.enter(session)
        prepared = fixture.hold(session) if kind == 'heat' else fixture.shake(session)
        fixture.leave(session)
        before = list(session.backend.protocol.commands())
        with pytest.raises(DriverValidationError) as error:
            if kind == 'heat': session.execute_temperature_hold(prepared)
            else: session.execute_orbital_shake(prepared)
        assert error.value.code == 'DRV_OT_MOTION_STALE_PROCESSING'
        assert session.backend.protocol.commands() == before
    finally: session.close()


def test_saved_adapter_position_restores_without_actions(tmp_path):
    fixture = HeaterShakerDockingFixture()
    session = fixture.open(tmp_path)
    fixture.enter(session)
    session.close()
    restarted = fixture.open(tmp_path)
    try:
        assert restarted.backend.labware['assay'].parent is restarted.backend.module_adapters['heater']
        assert not restarted.backend.protocol.commands()
        assert restarted.describe_session().motion_capabilities['docks']['module:heater']['adapter'] == 'opentrons_96_flat_bottom_adapter'
        assert fixture.leave(restarted).outcome == 'completed'
    finally: restarted.close()


@pytest.mark.parametrize('after',[False,True])
def test_unknown_latch_does_not_start_move(tmp_path, monkeypatch, after):
    fixture = HeaterShakerDockingFixture()
    session = fixture.open(tmp_path)
    module = session.backend.modules['heater']
    original = module.open_labware_latch
    def lost():
        if after: original()
        raise TimeoutError('latch return lost')
    monkeypatch.setattr(module,'open_labware_latch',lost)
    try:
        receipt = fixture.enter(session)
        assert receipt.outcome == 'unknown' and receipt.payload['phases']['move'] == 'not_started'
        assert receipt.payload['module_action_results'][0]['status'] == 'completion_unknown'
        assert not any('with gripper' in c for c in session.backend.protocol.commands())
        assert session.motion.state['uncertain'] and session.motion.state['revision'] == 0
    finally: session.close()
    restarted = fixture.open(tmp_path)
    try:
        with pytest.raises(DriverValidationError): restarted.check_ready()
        assert not restarted.backend.protocol.commands()
    finally: restarted.close()


@pytest.mark.parametrize('kind',['heat','shake'])
def test_failed_release_retains_processing_ownership(tmp_path, monkeypatch, kind):
    fixture = HeaterShakerDockingFixture()
    session = fixture.open(tmp_path)
    try:
        fixture.enter(session)
        prepared = fixture.hold(session) if kind == 'heat' else fixture.shake(session)
        def lost(): raise TimeoutError('release unknown')
        monkeypatch.setattr(session.backend.modules['heater'], 'deactivate_heater' if kind == 'heat' else 'deactivate_shaker', lost)
        receipt = session.execute_temperature_hold(prepared) if kind == 'heat' else session.execute_orbital_shake(prepared)
        assert receipt.outcome == 'unknown'
        assert session.backend.module_access.active == {'heater':'hold' if kind == 'heat' else 'shake'}
        with pytest.raises(DriverValidationError): fixture.leave(session)
    finally: session.close()


@pytest.mark.parametrize('problem',['missing-adapter','wrong-adapter','middle-slot','trash-slot','incompatible-plate'])
def test_config_rejects_unsupported_profile(problem):
    raw = HeaterShakerDockingFixture().config()
    if problem == 'missing-adapter': raw['modules']['heater'].pop('adapter')
    if problem == 'wrong-adapter': raw['modules']['heater']['adapter'] = 'opentrons_96_deep_well_adapter'
    if problem == 'middle-slot': raw['modules']['heater']['slot'] = 'C2'
    if problem == 'trash-slot': raw['modules']['heater']['slot'] = 'A3'
    if problem == 'incompatible-plate': raw['labware']['assay']['load_name'] = 'nest_96_wellplate_2ml_deep'
    with pytest.raises(ValueError): DeckConfiguration.from_dict(raw)


def test_contradictory_open_observation_blocks_move(tmp_path, monkeypatch):
    fixture = HeaterShakerDockingFixture()
    session = fixture.open(tmp_path)
    try:
        module = session.backend.modules['heater']
        monkeypatch.setattr(type(module),'labware_latch_status',property(lambda self:'idle_closed'))
        receipt = fixture.enter(session)
        assert receipt.outcome == 'unknown'
        assert receipt.payload['phases']['move'] == 'not_started'
        assert not any('with gripper' in c for c in session.backend.protocol.commands())
    finally: session.close()


@pytest.mark.parametrize('kind',['heat','shake'])
def test_contradictory_close_observation_blocks_processing(tmp_path, monkeypatch, kind):
    fixture = HeaterShakerDockingFixture()
    session = fixture.open(tmp_path)
    try:
        fixture.enter(session)
        prepared = fixture.hold(session) if kind == 'heat' else fixture.shake(session)
        module = session.backend.modules['heater']
        monkeypatch.setattr(type(module),'labware_latch_status',property(lambda self:'idle_open'))
        before = len(session.backend.protocol.commands())
        receipt = session.execute_temperature_hold(prepared) if kind == 'heat' else session.execute_orbital_shake(prepared)
        assert receipt.outcome == 'unknown'
        assert session.backend.protocol.commands()[before:] == ['Latching labware on Heater-Shaker']
        assert session.backend.module_access.active
    finally: session.close()
