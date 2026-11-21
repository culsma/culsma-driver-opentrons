"""Explicit PCR lid setup, with no retained heater state across scalar effects."""
from dataclasses import replace
import pytest
from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.plugin import SimulatorSessionFactory
from culsma_opentrons.models import SimulatorBinding, WellAddress
from culsma_opentrons.modules.models import TemperatureHoldIntent, TemperatureTargetVolume
from culsma_opentrons.operations import ResolvedContainerRef
from .test_thermocycler_module import ThermocyclerFixture


class ThermocyclerPCRFixture(ThermocyclerFixture):
    def raw(self, config):
        raw = super().raw(config)
        raw['modules']['cycler'].update(lid_temperature_celsius=105, keep_lid_closed=True)
        raw['labware']['assay']['load_name'] = 'opentrons_96_wellplate_200ul_pcr_full_skirt'
        return raw

    def build(self, config, directory):
        raw = self.raw(config)
        session = SimulatorSessionFactory().create(DeckConfiguration.from_dict(raw), directory)
        bindings = tuple(SimulatorBinding(ResolvedContainerRef(key), WellAddress(**value))
                         for key, value in raw['bindings'].items())
        target = ResolvedContainerRef('target')
        intent = TemperatureHoldIntent('hold', (target,), 98, 60, (TemperatureTargetVolume(target, 50),))
        return session, intent, bindings


class TestThermocyclerPCR:
    def test_closed_lid_and_heater_cleanup(self, config, tmp_path):
        session, intent, bindings = ThermocyclerPCRFixture().build(config, tmp_path)
        try:
            for i, temperature in enumerate([98, 60, 72]):
                hold = replace(intent, step_id=f'hold-{i}', temperature_celsius=temperature)
                prepared = session.prepare_temperature_hold(hold, bindings)
                before = list(session.backend.protocol.commands())
                receipt = session.execute_temperature_hold(prepared)
                assert receipt.outcome == 'completed'
                assert receipt.payload['phases'] == dict.fromkeys(
                    ['close_lid', 'heat_lid', 'block_hold', 'deactivate_block', 'deactivate_lid'], 'returned')
                assert receipt.payload['postcondition'] == 'block_and_lid_deactivated_lid_closed'
                assert receipt.payload['lid_temperature_celsius'] == 105
                assert len(session.backend.protocol.commands()) == len(before) + 5
                assert not any('Opening Thermocycler' in c for c in receipt.payload['commands'])
                session.backend.thermocycler_access.require_inactive('cycler')
            receipt = session.execute_transfer(session.prepare_transfer(self.transfer(), bindings))
            assert receipt.outcome == 'completed'
            assert session.backend.module_action_results[-1]['action'] == 'open_lid'
        finally: session.close()

    def transfer(self):
        return ThermocyclerFixture().transfer()

    @pytest.mark.parametrize('heated,closed', [(False,False), (False,True), (True,False), (True,True)])
    def test_lid_options_are_independent_and_fingerprinted(self, config, tmp_path, heated, closed):
        f = ThermocyclerPCRFixture(); raw = f.raw(config)
        raw['modules']['cycler']['keep_lid_closed'] = closed
        if not heated: raw['modules']['cycler'].pop('lid_temperature_celsius')
        configured = DeckConfiguration.from_dict(raw)
        assert configured.fingerprint() != DeckConfiguration.from_dict(ThermocyclerFixture().raw(config)).fingerprint()
        session = SimulatorSessionFactory().create(configured, tmp_path)
        target = ResolvedContainerRef('target')
        intent = TemperatureHoldIntent('hold', (target,), 98, 1, (TemperatureTargetVolume(target, 50),))
        bindings = tuple(SimulatorBinding(ResolvedContainerRef(key), WellAddress(**value))
                         for key, value in raw['bindings'].items())
        try:
            receipt = session.execute_temperature_hold(session.prepare_temperature_hold(intent, bindings))
            assert receipt.outcome == 'completed'
            assert ('heat_lid' in receipt.payload['phases']) == heated
            assert ('deactivate_lid' in receipt.payload['phases']) == heated
            assert ('open_lid' in receipt.payload['phases']) != closed
            session.backend.thermocycler_access.require_inactive('cycler')
        finally: session.close()

    @pytest.mark.parametrize('method', ['close_lid', 'set_lid_temperature', 'set_block_temperature', 'deactivate_block', 'deactivate_lid'])
    @pytest.mark.parametrize('after', [False, True])
    def test_unknown_phase(self, config, tmp_path, monkeypatch, method, after):
        session, intent, bindings = ThermocyclerPCRFixture().build(config, tmp_path)
        try:
            module = session.backend.modules['cycler']; original = getattr(module, method)
            def fail(*args, **kwargs):
                if after: original(*args, **kwargs)
                raise TimeoutError('PCR acknowledgment lost')
            prepared = session.prepare_temperature_hold(intent, bindings)
            monkeypatch.setattr(module, method, fail)
            receipt = session.execute_temperature_hold(prepared)
            assert receipt.outcome == 'unknown' and session.backend.poisoned
            count = len(session.backend.protocol.commands())
            with pytest.raises(ValueError): session.prepare_transfer(self.transfer(), bindings)
            assert len(session.backend.protocol.commands()) == count
        finally: session.close()

    @pytest.mark.parametrize('field,value', [('lid_temperature_celsius',36), ('lid_temperature_celsius',111),
        ('lid_temperature_celsius',True), ('lid_temperature_celsius',float('nan')),
        ('lid_temperature_celsius',105.5), ('keep_lid_closed','yes'), ('keep_lid_closed',1)])
    def test_invalid_lid_configuration(self, config, field, value):
        raw = ThermocyclerPCRFixture().raw(config)
        raw['modules']['cycler'][field] = value
        with pytest.raises(ValueError): DeckConfiguration.from_dict(raw)
