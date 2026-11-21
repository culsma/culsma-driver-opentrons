import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from culsma_opentrons.profiles import ConfigurationProfileInteractor


@pytest.mark.parametrize('robot',['OT-2','Flex'])
def test_roundtrip_profiles_without_hardware(robot):
    profile=ConfigurationProfileInteractor();raw=profile.describe()['template']
    if robot=='Flex':
        raw.update(robot_type='Flex',pipette='flex_1channel_1000')
        raw['tiprack']={'load_name':'opentrons_flex_96_tiprack_1000ul','slot':'A1'}
        raw['labware']['tubes']['slot']='D1'
    before=copy.deepcopy(raw)
    result=profile.validate(raw)
    assert result['valid'],result['diagnostics']
    assert raw==before
    assert 'not program preflight' in result['scope']


@pytest.mark.parametrize('change,path',[
    ('missing-mount','/mount'),('unknown-well','/bindings/entry.s0::alloc/well'),
    ('unknown-labware','/labware/tubes/load_name'),('duplicate-slot','/'),
])
def test_profile_diagnostics_are_not_preflight_claims(change,path):
    profile=ConfigurationProfileInteractor();raw=profile.describe()['template']
    if change=='missing-mount':del raw['mount']
    if change=='unknown-well':raw['bindings']['entry.s0::alloc']['well']='Z99'
    if change=='unknown-labware':raw['labware']['tubes']['load_name']='not_a_labware_definition'
    if change=='duplicate-slot':raw['labware']['tubes']['slot']=raw['tiprack']['slot']
    result=profile.validate(raw)
    assert result['valid'] is False
    assert any(d['path']==path for d in result['diagnostics'])
    assert 'live readiness' in result['scope']


def test_profile_validation_imports_no_sdk_and_writes_nothing(tmp_path):
    code='''
import os,sys
from pathlib import Path
from culsma_opentrons.plugin import OpentronsSimulatorFactory
before=dict(os.environ)
profile=OpentronsSimulatorFactory().configuration_profile()
result=profile.validate(profile.describe()['template'])
assert result['valid'],result
assert profile.program_bindings(profile.describe()['template'])
assert not any(name=='opentrons' or name.startswith('opentrons.') for name in sys.modules)
assert before==dict(os.environ)
assert list(Path.cwd().iterdir())==[]
'''
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
    env['PYTHONPATH']=str(Path(__file__).resolve().parents[1]/'src')
    result=subprocess.run([sys.executable,'-c',code],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr


def test_profile_bindings_are_detached_and_offline():
    profile = ConfigurationProfileInteractor()
    raw = profile.describe()['template']
    before = copy.deepcopy(raw)
    result = profile.program_bindings(raw)
    assert result == raw['bindings']
    result['entry.s0::alloc']['well'] = 'A3'
    assert raw == before


def test_profile_navigation_metadata_is_offline_and_detached():
    profile = ConfigurationProfileInteractor()
    description = profile.describe()
    fields = {tuple(field['path']): field for field in description['fields']}
    assert fields['robot_type',]['section'] == 'platform'
    assert fields['pipette',]['section'] == 'instruments'
    assert fields['tiprack', 'slot']['section'] == 'layout'
    assert fields['transfer_policy', 'aspirate_flow_rate_uL_s']['type'] == 'number'
    assert fields['transfer_policy', 'stroke_mode']['options'] == ['single', 'balanced_split']
    raw = description['template']
    before = copy.deepcopy(raw)
    raw['transfer_policy'] = {'aspirate_flow_rate_uL_s': 12.5, 'stroke_mode': 'balanced_split'}
    assert profile.validate(raw)['valid']
    description['fields'][0]['options'].clear()
    assert profile.describe()['fields'][0]['options'] == ['OT-2', 'Flex']
    assert profile.describe()['template'] == before
