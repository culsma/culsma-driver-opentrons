"""Typed magnetic preparation, retained command witnesses and release evidence."""
from dataclasses import replace
import pytest
from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.plugin import SimulatorSessionFactory
from culsma_opentrons.models import DriverValidationError, SimulatorBinding, WellAddress
from culsma_opentrons.operations import ResolvedContainerRef, TransferIntent, TransferAliquot
from culsma_opentrons.modules.models import MagneticSeparationIntent, MagneticReleaseIntent, MagneticSourceSelection


class MagneticFixture:
    def build(self, config, directory):
        raw = config.snapshot()
        raw['modules'] = {'magnet': {'model':'magnetic module gen2','slot':'6', 'engage_height_mm':6, 'aspirate_height_mm':2}}
        raw['labware']['assay'] = {'load_name':'nest_96_wellplate_100ul_pcr_full_skirt','module':'magnet'}
        raw['bindings'] = {'sample':{'labware':'assay','well':'A1'}, 'waste':{'labware':'tubes','well':'A1'}}
        session = SimulatorSessionFactory().create(DeckConfiguration.from_dict(raw), directory)
        bindings = tuple(SimulatorBinding(ResolvedContainerRef(k), WellAddress(**v)) for k,v in raw['bindings'].items())
        return session, MagneticSeparationIntent('separate', (ResolvedContainerRef('sample'),), 300), bindings

    def transfer(self):
        return TransferIntent('flowthrough', ResolvedContainerRef('waste'), (TransferAliquot(ResolvedContainerRef('sample'),40),))


class TestMagneticModule:
    def test_prepare_separate_transfer_and_release(self, config, tmp_path):
        session, intent, bindings = MagneticFixture().build(config, tmp_path)
        try:
            assert session.describe_session().processing_capabilities == {'magnetic_separation':{'magnet':{'labware':['assay']}}}
            prepared = session.prepare_magnetic_separation(intent, bindings)
            assert not session.backend.protocol.commands()
            receipt = session.execute_magnetic_operation(prepared)
            assert receipt.outcome == 'completed' and receipt.payload['phases'] == {'engage':'returned','wait':'returned'}
            assert receipt.payload['field_measured'] is False
            assert 'magnet' in session.magnetic.held
            transfer = session.prepare_magnetic_transfer(MagneticFixture().transfer(), bindings, MagneticSourceSelection('sample','separate'))
            assert len(session.backend.protocol.commands()) == 2
            feedback = session.execute_transfer(transfer)
            assert feedback.outcome == 'completed'
            assert feedback.payload['module_action_results'][0]['evidence'] == 'session_owned_engage_and_wait_returned'
            release = session.prepare_magnetic_separation(MagneticReleaseIntent('release', intent.targets, 'separate'), ())
            assert session.execute_magnetic_operation(release).outcome == 'completed'
            assert not session.magnetic.held
            assert session.backend.protocol.commands()[-1] == 'Disengaging Magnetic Module'
            with pytest.raises(DriverValidationError): session.execute_magnetic_operation(prepared)
        finally: session.close()

    @pytest.mark.parametrize('phase,method', [('engage','engage'),('wait','delay'),('disengage','disengage')])
    @pytest.mark.parametrize('after_return',[False,True])
    def test_failed_phase_is_unknown(self, config, tmp_path, monkeypatch, phase, method, after_return):
        session, intent, bindings = MagneticFixture().build(config, tmp_path)
        try:
            prepared = session.prepare_magnetic_separation(intent, bindings)
            if phase == 'disengage':
                session.execute_magnetic_operation(prepared)
                prepared = session.prepare_magnetic_separation(MagneticReleaseIntent('release',intent.targets,'separate'),())
            obj = session.backend.protocol if method=='delay' else session.backend.modules['magnet']
            original = getattr(obj,method)
            def fail(*args,**kwargs):
                if after_return: original(*args,**kwargs)
                raise RuntimeError('uncertain magnetic phase')
            monkeypatch.setattr(obj,method,fail)
            receipt = session.execute_magnetic_operation(prepared)
            assert receipt.outcome == 'unknown' and receipt.payload['phases'][phase]=='started'
            assert session.backend.poisoned
        finally: session.close()

    @pytest.mark.parametrize('kind',['missing','wrong-producer','bound','ordinary','released','stale','forged'])
    def test_rejects_invalid_field_or_selection(self, config, tmp_path, kind):
        session, intent, bindings = MagneticFixture().build(config, tmp_path)
        try:
            prepared = session.prepare_magnetic_separation(intent,bindings)
            if kind != 'missing': session.execute_magnetic_operation(prepared)
            if kind == 'released':
                release=session.prepare_magnetic_separation(MagneticReleaseIntent('release',intent.targets,'separate'),())
                session.execute_magnetic_operation(release)
            start=len(session.backend.protocol.commands())
            with pytest.raises(DriverValidationError):
                if kind=='ordinary': session.prepare_transfer(MagneticFixture().transfer(),bindings)
                elif kind in {'stale','forged'}:
                    if kind=='stale': session.bindings.config.modules['magnet']['engage_height_mm']=7
                    session.execute_magnetic_operation(replace(prepared) if kind=='forged' else prepared)
                else:
                    session.prepare_magnetic_transfer(MagneticFixture().transfer(),bindings,
                        MagneticSourceSelection('sample','wrong' if kind=='wrong-producer' else 'separate','0' if kind=='bound' else '1'))
            assert len(session.backend.protocol.commands())==start
        finally: session.close()

    @pytest.mark.parametrize('kind',['height','aspiration','model','plate','channels'])
    def test_configuration(self,config,tmp_path,kind):
        session,intent,bindings=MagneticFixture().build(config,tmp_path)
        raw=session.bindings.config.snapshot();session.close()
        if kind=='height':raw['modules']['magnet']['engage_height_mm']=float('nan')
        if kind=='aspiration':raw['modules']['magnet']['aspirate_height_mm']=99
        if kind=='model':raw['modules']['magnet']['model']='magnetic module gen1'
        if kind=='plate':raw['labware']['assay']['load_name']='nest_96_wellplate_200ul_flat'
        if kind=='channels':raw['pipette']='p300_multi_gen2'
        with pytest.raises(ValueError):DeckConfiguration.from_dict(raw)
