"""Configured method realization, not empirical precision/carryover certification."""
from copy import deepcopy
from dataclasses import replace
import json
import pytest
from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.models import DriverValidationError, SimulatorBinding, WellAddress
from culsma_opentrons.operations import TransferIntent, TransferAliquot, ResolvedContainerRef
from culsma_opentrons.plugin import SimulatorSessionFactory
from .test_thermocycler_module import ThermocyclerFixture


class ConstraintFixture:
    def raw(self, config):
        raw = ThermocyclerFixture().raw(config)
        raw.update(pipette='p20_single_gen2', tiprack={'load_name':'opentrons_96_tiprack_20ul','slot':'2'})
        raw['transfer_policy']['stroke_mode'] = 'balanced_split'
        raw['transfer_policy']['precision_method'] = {'method_id':'small-volume-method','revision':'1',
            'pipette':'p20_single_gen2','minimum_volume_uL':1,'maximum_volume_uL':20,
            'aspirate_flow_rate_uL_s':5,'dispense_flow_rate_uL_s':10}
        return raw

    def build(self, config, directory, change=None):
        raw = self.raw(config)
        if change: change(raw)
        session = SimulatorSessionFactory().create(DeckConfiguration.from_dict(raw), directory)
        source,target = ResolvedContainerRef('source'),ResolvedContainerRef('target')
        intent = TransferIntent('constrained',target,(TransferAliquot(source,2.5),),requirements=('high_precision','low_carryover'))
        bindings = tuple(SimulatorBinding(ResolvedContainerRef(k),WellAddress(**v)) for k,v in raw['bindings'].items())
        return session,intent,bindings


class TestConstraintMethods:
    def test_methods_apply_and_plain_transfer_resets_flow(self, config, tmp_path, monkeypatch):
        session,intent,bindings = ConstraintFixture().build(config,tmp_path)
        observed = [];pipette=session.backend.pipette;aspirate=pipette.aspirate
        def observe(*args,**kwargs):
            observed.append((pipette.flow_rate.aspirate,pipette.flow_rate.dispense,pipette.has_tip))
            return aspirate(*args,**kwargs)
        monkeypatch.setattr(pipette,'aspirate',observe)
        try:
            assert session.describe_session().transfer_requirements == ('low_carryover','high_precision','aseptic','spread')
            prepared=session.prepare_transfer(replace(intent,aliquots=(replace(intent.aliquots[0],volume_uL=25),)),bindings)
            assert not session.backend.protocol.commands()
            desc=session.describe_transfer(prepared)
            assert [m['requirement'] for m in desc['constraint_resolution']] == list(intent.requirements)
            receipt=session.execute_transfer(prepared)
            assert receipt.outcome=='completed'
            assert observed==[(5,10,True),(5,10,True)]
            assert session.backend.available_tips()==94
            # Evidence survives a JSON transport round trip.
            payload=json.loads(json.dumps(receipt.payload));description=json.loads(json.dumps(desc))
            assert session.normalizer.interpret(description,payload,intent.step_id,session.bindings.config.fingerprint()).outcome=='completed'
            plain=replace(intent,step_id='plain',requirements=())
            result=session.execute_transfer(session.prepare_transfer(plain,bindings))
            assert result.outcome=='completed' and 'constraint_resolution' not in result.payload
            assert observed[-1]==(*session.backend.default_flow_rates,True)
        finally:session.close()

    def test_plating_preferences_record_fallbacks_in_simulated_receipt(self, config, tmp_path):
        session, intent, bindings = ConstraintFixture().build(config, tmp_path)
        try:
            intent = replace(intent, requirements=('spread', 'aseptic'))
            prepared = session.prepare_transfer(intent, bindings)
            description = session.describe_transfer(prepared)
            receipt = session.execute_transfer(prepared)
            assert receipt.outcome == 'completed'
            resolutions = receipt.payload['constraint_resolution']
            assert resolutions == description['constraint_resolution']
            assert [(r['requirement'], r['status']) for r in resolutions] == [
                ('spread', 'unmet'), ('aseptic', 'partial'),
            ]
            assert resolutions[0]['method_id'] == 'dispense-at-configured-location'
            assert resolutions[1]['parameters']['tip_policy'] == 'fresh_per_stroke'
            assert session.backend.available_tips() == 95
        finally:
            session.close()

    @pytest.mark.parametrize('problem',['missing','unknown','range','wrong-pipette','option','environment'])
    def test_method_rejection_has_no_commands(self, config, tmp_path, problem):
        def change(raw):
            if problem=='missing':raw['transfer_policy'].pop('precision_method')
            if problem=='range':raw['transfer_policy']['precision_method']['minimum_volume_uL']=5
            if problem=='wrong-pipette':raw['transfer_policy']['precision_method']['pipette']='p300_single_gen2'
        session,intent,bindings=ConstraintFixture().build(config,tmp_path,change)
        try:
            if problem=='unknown':intent=replace(intent,requirements=('gentle',))
            if problem=='option':intent=replace(intent,constraint_options=('tolerance',))
            if problem=='environment':intent=replace(intent,environment_requirements=('cold_chain',))
            with pytest.raises(DriverValidationError):session.prepare_transfer(intent,bindings)
            assert not session.backend.protocol.commands()
        finally:session.close()

    @pytest.mark.parametrize('fault',['missing-method','wrong-method','wrong-flow','missing-tip-return'])
    def test_mismatched_receipt_is_unknown(self, config, tmp_path, fault):
        session,intent,bindings=ConstraintFixture().build(config,tmp_path)
        try:
            prepared=session.prepare_transfer(intent,bindings);desc=session.describe_transfer(prepared)
            receipt=session.execute_transfer(prepared);payload=deepcopy(receipt.payload)
            if fault=='missing-method':payload.pop('constraint_resolution')
            if fault=='wrong-method':payload['constraint_resolution'][0]['method_id']='unrelated'
            if fault=='wrong-flow':payload['instrument']['aspirate_flow_rate_uL_s']=999
            if fault=='missing-tip-return':payload['action_results'][0]['status']='completion_unknown'
            assert session.normalizer.interpret(desc,payload,intent.step_id,session.bindings.config.fingerprint()).outcome=='unknown'
        finally:session.close()

    def test_changed_method_invalidates_preparation(self, config, tmp_path):
        session,intent,bindings=ConstraintFixture().build(config,tmp_path)
        try:
            prepared=session.prepare_transfer(intent,bindings)
            method=session.bindings.config.transfer_policy.precision_method
            session.bindings.config=replace(session.bindings.config,transfer_policy=replace(
                session.bindings.config.transfer_policy,precision_method=replace(method,revision='2')))
            receipt = session.execute_transfer(prepared)
            assert receipt.outcome=='unknown' and receipt.payload['phase']=='rejected'
            assert not session.backend.protocol.commands()
        finally:session.close()

    @pytest.mark.parametrize('key,value',[('minimum_volume_uL',0),('maximum_volume_uL',float('inf')),
        ('aspirate_flow_rate_uL_s',True),('dispense_flow_rate_uL_s',-1),('revision',''),('pipette','')])
    def test_invalid_method_configuration(self, config,key,value):
        raw=ConstraintFixture().raw(config);raw['transfer_policy']['precision_method'][key]=value
        with pytest.raises(ValueError):DeckConfiguration.from_dict(raw)
