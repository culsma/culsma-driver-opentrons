"""Official Heater-Shaker heating, independently selectable from orbital shaking."""

from dataclasses import replace
import pytest

from culsma_opentrons.models import DriverValidationError, SimulatorBinding, WellAddress
from culsma_opentrons.modules.models import TemperatureHoldIntent
from culsma_opentrons.operations import ResolvedContainerRef
from .test_agitation_module import AgitationFixture


class IncubationFixture:
    def build(self, config, directory):
        session, shake, bindings = AgitationFixture().build(config, directory)
        return session, TemperatureHoldIntent('incubate', shake.targets, 37, 30), bindings


class TestIncubationModule:
    @pytest.mark.parametrize('temperature', [37, 60, 95])
    def test_blocking_heater_commands(self, config, tmp_path, temperature):
        session, intent, bindings = IncubationFixture().build(config, tmp_path)
        try:
            intent = replace(intent, temperature_celsius=temperature)
            prepared = session.prepare_temperature_hold(intent, bindings)
            assert not session.backend.protocol.commands()
            receipt = session.execute_temperature_hold(prepared)
            assert receipt.outcome == 'completed' and receipt.consumed_resources == {}
            assert receipt.payload['phases'] == dict.fromkeys(['close_latch','set_temperature','hold','deactivate_heater'], 'returned')
            commands = receipt.payload['commands']
            assert len(commands) == 5 and 'Latching' in commands[0]
            assert str(temperature) in commands[1] and 'Waiting for Heater-Shaker' in commands[2]
            assert 'Delaying' in commands[3] and 'Deactivating Heater' in commands[4]
            assert not any('RPM' in command for command in commands)
            assert receipt.payload['sample_temperature_measured'] is False
            assert receipt.payload['readiness_evidence'] == 'blocking_set_and_wait_for_temperature_returned'
            with pytest.raises(DriverValidationError): session.execute_temperature_hold(prepared)
        finally: session.close()

    @pytest.mark.parametrize('kind', ['temperature-low','temperature-high','temperature-fractional','collateral','foreign','modified','stale','active-heater','active-shaker','attached-tip'])
    def test_prepare_guards(self, config, tmp_path, monkeypatch, kind):
        session, intent, bindings = IncubationFixture().build(config, tmp_path)
        other, _, _ = IncubationFixture().build(config, tmp_path)
        try:
            if kind.startswith('temperature'):
                intent = replace(intent, temperature_celsius={'temperature-low':36,'temperature-high':96,'temperature-fractional':37.5}[kind])
            if kind == 'collateral':
                session.bindings.config.bindings['extra'] = WellAddress('assay','B1')
                bindings += (SimulatorBinding(ResolvedContainerRef('extra'), WellAddress('assay','B1')),)
            if kind.startswith('active'):
                prop = 'target_temperature' if kind == 'active-heater' else 'target_speed'
                monkeypatch.setattr(type(session.backend.modules['shaker']), prop, property(lambda self: 500))
            if kind == 'attached-tip': session.backend.pipette.pick_up_tip()
            before = list(session.backend.protocol.commands())
            with pytest.raises(DriverValidationError):
                prepared = session.prepare_temperature_hold(intent, bindings)
                if kind == 'modified': prepared = replace(prepared, intent=replace(intent, temperature_celsius=38))
                if kind == 'stale': session.bindings.config.modules['shaker']['slot'] = '7'
                (other if kind == 'foreign' else session).execute_temperature_hold(prepared)
            assert session.backend.protocol.commands() == before
            assert not other.backend.protocol.commands()
        finally:
            session.close()
            other.close()

    @pytest.mark.parametrize('phase,method', [('close_latch','close_labware_latch'),('set_temperature','set_and_wait_for_temperature'),
        ('hold','delay'),('deactivate_heater','deactivate_heater')])
    @pytest.mark.parametrize('after_return', [False,True])
    def test_unknown_blocks_pipetting(self, config, tmp_path, monkeypatch, phase, method, after_return):
        session, intent, bindings = IncubationFixture().build(config, tmp_path)
        try:
            prepared = session.prepare_temperature_hold(intent, bindings)
            obj = session.backend.protocol if method == 'delay' else session.backend.modules['shaker']
            original = getattr(obj, method)
            def fail(*args, **kwargs):
                if after_return: original(*args, **kwargs)
                raise RuntimeError('uncertain incubation phase')
            monkeypatch.setattr(obj, method, fail)
            receipt = session.execute_temperature_hold(prepared)
            assert receipt.outcome == 'unknown' and receipt.payload['phases'][phase] == 'started'
            assert session.backend.poisoned
            with pytest.raises(DriverValidationError): session.prepare_transfer(AgitationFixture().transfer(), bindings)
        finally: session.close()

    def test_heating_and_shaking_are_separate_operations(self, config, tmp_path):
        session, intent, bindings = IncubationFixture().build(config, tmp_path)
        try:
            assert session.execute_temperature_hold(session.prepare_temperature_hold(intent, bindings)).outcome == 'completed'
            from culsma_opentrons.modules.models import OrbitalShakeIntent
            shake = OrbitalShakeIntent('shake',intent.targets,500,1)
            assert session.execute_orbital_shake(session.prepare_orbital_shake(shake,bindings)).outcome == 'completed'
            assert session.execute_transfer(session.prepare_transfer(AgitationFixture().transfer(),bindings)).outcome == 'completed'
            commands = session.backend.protocol.commands()
            assert next(i for i,c in enumerate(commands) if 'Deactivating Heater' in c) < next(i for i,c in enumerate(commands) if 'RPM' in c)
        finally: session.close()
