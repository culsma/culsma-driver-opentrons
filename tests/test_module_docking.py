"""Official Flex module parents, access checks and revision-bound processing."""
from dataclasses import replace
import pytest
from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.plugin import SimulatorSessionFactory
from culsma_opentrons.motion.models import LabwareMoveIntent
from culsma_opentrons.models import DriverValidationError, SimulatorBinding, WellAddress
from culsma_opentrons.operations import ResolvedContainerRef
from culsma_opentrons.modules.models import TemperatureHoldIntent
from .test_labware_motion import MotionFixture


class DockingFixture:
    def config(self):
        raw = MotionFixture().config()
        raw['modules'] = {'heater': {'model': 'temperature module gen2', 'slot': 'C1'}}
        raw['labware_moves']['transfer'][0]['destination'] = 'module:heater'
        return raw

    def open(self, directory, change=None):
        raw = self.config()
        if change: change(raw)
        return SimulatorSessionFactory().create(DeckConfiguration.from_dict(raw), directory)

    def enter(self, session):
        return session.execute_labware_move(session.prepare_labware_move(
            LabwareMoveIntent('enter', 'assay', 'D1', 'module:heater', 0)))

    def leave(self, session):
        return session.execute_labware_move(session.prepare_labware_move(
            LabwareMoveIntent('leave', 'assay', 'module:heater', 'D1', 1)))

    def hold(self, session):
        a, b = ResolvedContainerRef('a'), ResolvedContainerRef('b')
        return session.prepare_temperature_hold(TemperatureHoldIntent('hold', (a,b), 37, 1),
            (SimulatorBinding(a, WellAddress('assay','A1')), SimulatorBinding(b, WellAddress('assay','A2'))))


def test_dock_hold_and_undock(tmp_path):
    fixture = DockingFixture()
    session = fixture.open(tmp_path)
    try:
        with pytest.raises(DriverValidationError): fixture.hold(session)
        assert not session.backend.protocol.commands()
        assert fixture.enter(session).outcome == 'completed'
        assert session.backend.labware['assay'].parent is session.backend.modules['heater']
        assert session.execute_temperature_hold(fixture.hold(session)).outcome == 'completed'
        assert fixture.leave(session).outcome == 'completed'
        assert str(session.backend.labware['assay'].parent) == 'D1'
        assert session.motion.state['revision'] == 2
    finally: session.close()


@pytest.mark.parametrize('direction', ['enter','leave'])
@pytest.mark.parametrize('phase', ['prepare','execute'])
def test_busy_module_rejects_move(tmp_path, direction, phase):
    fixture = DockingFixture()
    session = fixture.open(tmp_path)
    try:
        if direction == 'leave': assert fixture.enter(session).outcome == 'completed'
        intent = (LabwareMoveIntent('enter','assay','D1','module:heater',0) if direction == 'enter'
                  else LabwareMoveIntent('leave','assay','module:heater','D1',1))
        if phase == 'execute': prepared = session.prepare_labware_move(intent)
        session.backend.temperature_access.begin_hold('heater','outside-hold')
        session.backend.modules['heater'].set_temperature(37)
        commands = list(session.backend.protocol.commands())
        with pytest.raises(DriverValidationError) as error:
            if phase == 'prepare': session.prepare_labware_move(intent)
            else: session.execute_labware_move(prepared)
        assert error.value.code == 'DRV_OT_MODULE_BUSY'
        assert session.backend.protocol.commands() == commands
    finally: session.close()


def test_stale_processing_rejects_after_undock(tmp_path):
    fixture = DockingFixture()
    session = fixture.open(tmp_path)
    try:
        assert fixture.enter(session).outcome == 'completed'
        prepared = fixture.hold(session)
        assert fixture.leave(session).outcome == 'completed'
        before = list(session.backend.protocol.commands())
        with pytest.raises(DriverValidationError) as error: session.execute_temperature_hold(prepared)
        assert error.value.code == 'DRV_OT_MOTION_STALE_PROCESSING'
        assert session.backend.protocol.commands() == before
    finally: session.close()


def test_module_position_restores_without_actions(tmp_path):
    fixture = DockingFixture()
    session = fixture.open(tmp_path)
    assert fixture.enter(session).outcome == 'completed'
    session.close()
    restarted = fixture.open(tmp_path)
    try:
        assert restarted.backend.labware['assay'].parent is restarted.backend.modules['heater']
        assert not restarted.backend.protocol.commands()
        assert restarted.motion.positions()['assay'] == 'module:heater'
        assert restarted.describe_session().motion_capabilities['initial_locations']['assay'] == 'D1'
        assert fixture.leave(restarted).outcome == 'completed'
    finally: restarted.close()


@pytest.mark.parametrize('problem', ['module-slot','incompatible','unknown-module','unsupported-module','occupied-module'])
def test_docking_configuration_and_occupancy(tmp_path, problem):
    fixture = DockingFixture()
    raw = fixture.config()
    route = raw['labware_moves']['transfer'][0]
    if problem == 'module-slot': route['destination'] = 'C1'
    if problem == 'incompatible': raw['labware']['assay']['load_name'] = 'nest_96_wellplate_2ml_deep'
    if problem == 'unknown-module': route['destination'] = 'module:missing'
    if problem == 'unsupported-module': raw['modules']['heater']['model'] = 'heaterShakerModuleV1'
    if problem == 'occupied-module': raw['labware']['parked'] = {'module':'heater','load_name':'nest_96_wellplate_200ul_flat'}
    if problem in {'incompatible','unknown-module','unsupported-module'}:
        with pytest.raises(ValueError): DeckConfiguration.from_dict(raw)
        return
    session = SimulatorSessionFactory().create(DeckConfiguration.from_dict(raw), tmp_path)
    try:
        with pytest.raises(DriverValidationError):
            session.prepare_labware_move(LabwareMoveIntent('move','assay','D1',route['destination'],0))
        assert not session.backend.protocol.commands()
    finally: session.close()


def test_observed_active_module_rejects_move(tmp_path, monkeypatch):
    fixture = DockingFixture()
    session = fixture.open(tmp_path)
    try:
        module = session.backend.modules['heater']
        monkeypatch.setattr(type(module), 'target', property(lambda self: 37))
        with pytest.raises(DriverValidationError): fixture.enter(session)
        assert not session.backend.protocol.commands()
    finally: session.close()


def test_failed_temperature_release_keeps_access_blocked(tmp_path, monkeypatch):
    fixture = DockingFixture()
    session = fixture.open(tmp_path)
    try:
        fixture.enter(session)
        def lost(): raise TimeoutError('release unknown')
        monkeypatch.setattr(session.backend.modules['heater'], 'deactivate', lost)
        receipt = session.execute_temperature_hold(fixture.hold(session))
        assert receipt.outcome == 'unknown'
        assert session.backend.temperature_access.active == {'heater':'hold'}
        with pytest.raises(DriverValidationError): fixture.leave(session)
    finally: session.close()


def test_busy_dock_blocks_transfer(tmp_path):
    from culsma_opentrons.operations import TransferIntent, TransferAliquot
    fixture = DockingFixture()
    session = fixture.open(tmp_path)
    try:
        fixture.enter(session)
        session.backend.temperature_access.begin_hold('heater', 'outside-hold')
        a,b = ResolvedContainerRef('a'),ResolvedContainerRef('b')
        before = list(session.backend.protocol.commands())
        with pytest.raises(DriverValidationError):
            session.prepare_transfer(TransferIntent('transfer', b, (TransferAliquot(a, 10),)),
                (SimulatorBinding(a,WellAddress('assay','A1')),SimulatorBinding(b,WellAddress('assay','A2'))))
        assert session.backend.protocol.commands() == before
    finally: session.close()
