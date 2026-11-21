from dataclasses import asdict, replace
from hashlib import sha256
import json

import pytest
from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.plugin import SimulatorSessionFactory
from culsma_opentrons.models import DriverValidationError, SimulatorBinding, WellAddress
from culsma_opentrons.operations import ResolvedContainerRef
from culsma_opentrons.modules.models import TemperatureHoldIntent


class TemperatureFixture:
    def raw(self, config):
        value = config.snapshot()
        value['modules'] = {'temperature': {'model': 'temperature module gen2', 'slot': '3'}}
        value['labware']['assay'] = {'load_name': 'opentrons_96_aluminumblock_nest_wellplate_100ul', 'module': 'temperature'}
        value['bindings'] = {'source': {'labware': 'tubes', 'well': 'A1'}, 'target': {'labware': 'assay', 'well': 'A1'}}
        return value

    def build(self, config, tmp_path):
        raw = self.raw(config)
        session = SimulatorSessionFactory().create(DeckConfiguration.from_dict(raw), tmp_path)
        bindings = tuple(SimulatorBinding(ResolvedContainerRef(k), WellAddress(**v)) for k, v in raw['bindings'].items())
        intent = TemperatureHoldIntent('hold', (ResolvedContainerRef('target'),), 37, 60)
        return session, intent, bindings


class TestTemperatureModule:
    def test_prepare_and_ordered_virtual_commands(self, config, tmp_path):
        session, intent, bindings = TemperatureFixture().build(config, tmp_path)
        try:
            assert session.describe_session().processing_capabilities == {'thermal_hold': {'temperature': {'labware': ['assay']}}}
            prepared = session.prepare_temperature_hold(intent, bindings)
            assert not session.backend.protocol.commands()
            receipt = session.execute_temperature_hold(prepared)
            assert receipt.outcome == 'completed'
            assert receipt.consumed_resources == {}
            assert receipt.payload['phases'] == {'set_temperature': 'returned', 'hold': 'returned', 'deactivate': 'returned'}
            commands = receipt.payload['commands']
            assert len(commands) == 3
            assert '37' in commands[0] and 'Delaying' in commands[1] and 'Deactivat' in commands[2]
            assert receipt.payload['sample_temperature_measured'] is False
            assert receipt.payload['readiness_evidence'] == 'blocking_set_temperature_returned'
            assert receipt.payload['module_end_status'] == 'idle'
            assert session.backend.available_tips() == 96
            with pytest.raises(DriverValidationError, match='Foreign'):
                session.execute_temperature_hold(prepared)
        finally:
            session.close()

    @pytest.mark.parametrize('temperature', [3, 96, 37.5])
    def test_setpoint_rejects_before_commands(self, config, tmp_path, temperature):
        session, intent, bindings = TemperatureFixture().build(config, tmp_path)
        try:
            with pytest.raises(DriverValidationError) as error:
                session.prepare_temperature_hold(replace(intent, temperature_celsius=temperature), bindings)
            assert error.value.code == 'DRV_OT_MODULE_TEMPERATURE'
            assert not session.backend.protocol.commands()
        finally:
            session.close()

    @pytest.mark.parametrize('kind', ['foreign', 'modified', 'stale'])
    def test_prepared_identity(self, config, tmp_path, kind):
        session, intent, bindings = TemperatureFixture().build(config, tmp_path)
        other, _, _ = TemperatureFixture().build(config, tmp_path)
        try:
            prepared = session.prepare_temperature_hold(intent, bindings)
            if kind == 'modified':
                prepared = replace(prepared, intent=replace(intent, temperature_celsius=38))
            if kind == 'stale':
                session.bindings.config.modules['temperature']['slot'] = '4'
            with pytest.raises(DriverValidationError):
                (other if kind == 'foreign' else session).execute_temperature_hold(prepared)
            assert not session.backend.protocol.commands() and not other.backend.protocol.commands()
        finally:
            session.close()
            other.close()

    @pytest.mark.parametrize('phase', ['set_temperature', 'hold', 'deactivate'])
    def test_failure_is_unknown_and_poisoned(self, config, tmp_path, monkeypatch, phase):
        session, intent, bindings = TemperatureFixture().build(config, tmp_path)
        try:
            prepared = session.prepare_temperature_hold(intent, bindings)
            obj, method = (session.backend.protocol, 'delay') if phase == 'hold' else (session.backend.modules['temperature'], phase)
            def fail(*args, **kwargs):
                raise RuntimeError('injected module failure')
            monkeypatch.setattr(obj, method, fail)
            receipt = session.execute_temperature_hold(prepared)
            assert receipt.outcome == 'unknown' and receipt.payload['phases'][phase] == 'started'
            assert session.backend.poisoned
            with pytest.raises(DriverValidationError):
                session.prepare_temperature_hold(intent, bindings)
        finally:
            session.close()

    @pytest.mark.parametrize('kind', ['not-module', 'collateral', 'multi-labware'])
    def test_scope_rejects(self, config, tmp_path, kind):
        session, intent, bindings = TemperatureFixture().build(config, tmp_path)
        try:
            if kind == 'not-module':
                intent = replace(intent, targets=(ResolvedContainerRef('source'),))
            elif kind == 'multi-labware':
                intent = replace(intent, targets=tuple(b.reference for b in bindings))
            else:
                session.bindings.config.bindings['extra'] = WellAddress('assay', 'B1')
                bindings += (SimulatorBinding(ResolvedContainerRef('extra'), WellAddress('assay', 'B1')),)
            with pytest.raises(DriverValidationError):
                session.prepare_temperature_hold(intent, bindings)
            assert not session.backend.protocol.commands()
        finally:
            session.close()

    @pytest.mark.parametrize('kind', ['collision', 'model', 'two-labware', 'unknown-module'])
    def test_configuration_rejects(self, config, kind):
        raw = TemperatureFixture().raw(config)
        if kind == 'collision':
            raw['modules']['temperature']['slot'] = raw['tiprack']['slot']
        elif kind == 'model':
            raw['modules']['temperature']['model'] = 'heaterShakerModuleV1'
        elif kind == 'two-labware':
            raw['labware']['extra'] = dict(raw['labware']['assay'])
        else:
            raw['labware']['assay']['module'] = 'missing'
        with pytest.raises(ValueError):
            DeckConfiguration.from_dict(raw)

    def test_transfer_config_fingerprint_unchanged(self, config):
        assert not config.modules and 'modules' not in config.snapshot()
        previous = asdict(config)
        previous.pop('modules')
        previous.pop('labware_moves')
        previous.pop('reference_devices')
        previous.pop('reference_environments')
        previous['transfer_policy'].pop('precision_method')
        assert 'labware_moves' not in config.snapshot()
        assert config.fingerprint() == sha256(json.dumps(previous, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    @pytest.mark.parametrize('temperature,duration', [(float('nan'), 60), (37, 0), (37, -1), (37, float('inf')), (True, 60)])
    def test_numeric_contract(self, temperature, duration):
        with pytest.raises(ValueError):
            TemperatureHoldIntent('hold', (ResolvedContainerRef('target'),), temperature, duration)
