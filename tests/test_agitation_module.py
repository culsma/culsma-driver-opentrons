"""Official virtual Heater-Shaker commands, preparation contracts and pipetting prerequisites."""
from dataclasses import asdict, replace

import pytest
from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.models import DriverValidationError, SimulatorBinding, WellAddress
from culsma_opentrons.operations import ResolvedContainerRef, TransferIntent, TransferAliquot
from culsma_opentrons.plugin import SimulatorSessionFactory
from culsma_opentrons.modules.models import OrbitalShakeIntent, TemperatureHoldIntent


class AgitationFixture:
    def raw(self, config):
        value = config.snapshot()
        value['modules'] = {'shaker': {'model': 'heaterShakerModuleV1', 'slot': '6'}}
        value['labware']['assay'] = {'load_name': 'nest_96_wellplate_200ul_flat',
                                    'adapter': 'opentrons_96_flat_bottom_adapter', 'module': 'shaker'}
        value['bindings'] = {'source': {'labware': 'tubes', 'well': 'A1'}, 'target': {'labware': 'assay', 'well': 'A1'}}
        return value

    def build(self, config, directory):
        raw = self.raw(config)
        session = SimulatorSessionFactory().create(DeckConfiguration.from_dict(raw), directory)
        bindings = tuple(SimulatorBinding(ResolvedContainerRef(k), WellAddress(**v)) for k, v in raw['bindings'].items())
        intent = OrbitalShakeIntent('shake', (ResolvedContainerRef('target'),), 500, 30)
        return session, intent, bindings

    def transfer(self):
        return TransferIntent('transfer', ResolvedContainerRef('target'),
                              (TransferAliquot(ResolvedContainerRef('source'), 50),))


class TestAgitationModule:
    @pytest.mark.parametrize('rate', [200, 500, 3000])
    def test_prepare_and_execute(self, config, tmp_path, rate):
        session, intent, bindings = AgitationFixture().build(config, tmp_path)
        try:
            assert 'simulation:opentrons:module:shaker' in session.describe_session().shared_resources
            assert session.describe_session().processing_capabilities == {
                'thermal_hold': {'shaker': {'labware': ['assay']}},
                'orbital_shake': {'shaker': {
                    'labware': ['assay'],
                    'default_motion': 'orbital',
                    'environment_conditions': {'thermal': {'unit': 'C', 'minimum': 37, 'maximum': 95}},
                }},
            }
            intent = replace(intent, rate_rpm=rate)
            prepared = session.prepare_orbital_shake(intent, bindings)
            assert not session.backend.protocol.commands()
            receipt = session.execute_orbital_shake(prepared)
            assert receipt.outcome == 'completed' and receipt.consumed_resources == {}
            assert receipt.payload['phases'] == {'close_latch': 'returned', 'set_speed': 'returned', 'hold': 'returned', 'deactivate': 'returned'}
            commands = receipt.payload['commands']
            assert len(commands) == 4 and 'Latching' in commands[0] and f'{rate} RPM' in commands[1]
            assert 'Delaying' in commands[2] and 'Deactivating Shaker' in commands[3]
            assert receipt.payload['speed_measured'] is False and receipt.payload['mixing_result_measured'] is False
            assert receipt.payload['readiness_evidence'] == 'blocking_set_speed_returned'
            assert receipt.payload['latch_observed_status'] == session.backend.modules['shaker'].labware_latch_status
            assert session.backend.available_tips() == 96
            with pytest.raises(DriverValidationError): session.execute_orbital_shake(prepared)
        finally: session.close()

    @pytest.mark.parametrize('rate', [199, 3001])
    def test_bounds_reject_before_actions(self, config, tmp_path, rate):
        session, intent, bindings = AgitationFixture().build(config, tmp_path)
        try:
            with pytest.raises(DriverValidationError) as error:
                session.prepare_orbital_shake(replace(intent, rate_rpm=rate), bindings)
            assert error.value.code == 'DRV_OT_MODULE_RATE'
            assert not session.backend.protocol.commands()
        finally: session.close()

    @pytest.mark.parametrize('kind', ['foreign', 'modified', 'stale'])
    def test_preparation_identity(self, config, tmp_path, kind):
        session, intent, bindings = AgitationFixture().build(config, tmp_path)
        other, _, _ = AgitationFixture().build(config, tmp_path)
        try:
            prepared = session.prepare_orbital_shake(intent, bindings)
            if kind == 'modified': prepared = replace(prepared, intent=replace(intent, rate_rpm=600))
            if kind == 'stale': session.bindings.config.modules['shaker']['slot'] = '7'
            with pytest.raises(DriverValidationError):
                (other if kind == 'foreign' else session).execute_orbital_shake(prepared)
            assert not session.backend.protocol.commands() and not other.backend.protocol.commands()
        finally:
            session.close()
            other.close()

    @pytest.mark.parametrize('phase,method', [('close_latch', 'close_labware_latch'), ('set_speed', 'set_and_wait_for_shake_speed'),
                                            ('hold', 'delay'), ('deactivate', 'deactivate_shaker')])
    @pytest.mark.parametrize('after_return', [False, True])
    def test_failed_phase_is_unknown(self, config, tmp_path, monkeypatch, phase, method, after_return):
        session, intent, bindings = AgitationFixture().build(config, tmp_path)
        try:
            prepared = session.prepare_orbital_shake(intent, bindings)
            obj = session.backend.protocol if method == 'delay' else session.backend.modules['shaker']
            original = getattr(obj, method)
            def fail(*args, **kwargs):
                if after_return: original(*args, **kwargs)
                raise RuntimeError('injected uncertain module phase')
            monkeypatch.setattr(obj, method, fail)
            receipt = session.execute_orbital_shake(prepared)
            assert receipt.outcome == 'unknown' and receipt.payload['phases'][phase] == 'started'
            assert session.backend.poisoned
            with pytest.raises(DriverValidationError): session.prepare_orbital_shake(intent, bindings)
        finally: session.close()

    @pytest.mark.parametrize('kind', ['not-module', 'collateral'])
    def test_scope_and_capability(self, config, tmp_path, kind):
        session, intent, bindings = AgitationFixture().build(config, tmp_path)
        try:
            if kind == 'not-module': intent = replace(intent, targets=(ResolvedContainerRef('source'),))
            elif kind == 'collateral':
                session.bindings.config.bindings['extra'] = WellAddress('assay', 'B1')
                bindings += (SimulatorBinding(ResolvedContainerRef('extra'), WellAddress('assay', 'B1')),)
            with pytest.raises(DriverValidationError): session.prepare_orbital_shake(intent, bindings)
            assert not session.backend.protocol.commands()
        finally: session.close()

    @pytest.mark.parametrize('kind', ['bad-slot', 'adjacent-labware', 'adapter', 'plate', 'multichannel'])
    def test_configuration_rejections(self, config, kind):
        raw = AgitationFixture().raw(config)
        if kind == 'bad-slot': raw['modules']['shaker']['slot'] = '5'
        if kind == 'adjacent-labware': raw['tiprack']['slot'] = '5'
        if kind == 'adapter': raw['labware']['assay']['adapter'] = 'opentrons_96_pcr_adapter'
        if kind == 'plate': raw['labware']['assay']['load_name'] = 'corning_96_wellplate_360ul_flat'
        if kind == 'multichannel': raw['pipette'] = 'p300_multi_gen2'
        with pytest.raises(ValueError): DeckConfiguration.from_dict(raw)

    @pytest.mark.parametrize('rate,duration', [(500.1, 30), (True, 30), (500, 0), (500, float('nan'))])
    def test_numeric_shape(self, rate, duration):
        with pytest.raises(ValueError): OrbitalShakeIntent('shake', (ResolvedContainerRef('target'),), rate, duration)

    def test_latch_is_transfer_execution_prerequisite(self, config, tmp_path):
        session, intent, bindings = AgitationFixture().build(config, tmp_path)
        try:
            prepared = session.prepare_transfer(AgitationFixture().transfer(), bindings)
            assert not session.backend.protocol.commands()
            description = session.describe_transfer(prepared)
            assert description['module_preconditions'] == [{'module_id': 'shaker', 'action': 'close_labware_latch'}]
            receipt = session.execute_transfer(prepared)
            assert receipt.outcome == 'completed'
            assert receipt.payload['commands'][0] == 'Latching labware on Heater-Shaker'
            assert receipt.payload['module_action_results'][0]['status'] == 'returned'
            assert receipt.consumed_resources == {'tips': 1}
            altered = dict(receipt.payload, module_action_results=[])
            assert session.normalizer.interpret(description, altered, 'transfer', session.bindings.config.fingerprint()).outcome == 'unknown'
        finally: session.close()

    def test_latch_failure_before_pipetting_retains_unknown(self, config, tmp_path, monkeypatch):
        session, intent, bindings = AgitationFixture().build(config, tmp_path)
        try:
            prepared = session.prepare_transfer(AgitationFixture().transfer(), bindings)
            def fail(*args, **kwargs): raise RuntimeError('uncertain latch')
            monkeypatch.setattr(session.backend.modules['shaker'], 'close_labware_latch', fail)
            receipt = session.execute_transfer(prepared)
            assert receipt.outcome == 'unknown' and receipt.consumed_resources == {'tips': 0}
            assert receipt.payload['module_action_results'][0]['status'] == 'completion_unknown'
            assert all(a['status'] == 'not_started' for a in receipt.payload['action_results'])
            assert session.backend.available_tips() == 96 and session.backend.poisoned
        finally: session.close()

    def test_transfer_rejects_observed_active_shaker_before_actions(self, config, tmp_path, monkeypatch):
        session, intent, bindings = AgitationFixture().build(config, tmp_path)
        module = session.backend.modules['shaker']
        monkeypatch.setattr(type(module), 'speed_status', property(lambda self: 'holding at target'))
        try:
            with pytest.raises(DriverValidationError) as error:
                session.prepare_transfer(AgitationFixture().transfer(), bindings)
            assert error.value.code == 'DRV_OT_MODULE_BUSY'
            assert not session.backend.protocol.commands()
        finally: session.close()
