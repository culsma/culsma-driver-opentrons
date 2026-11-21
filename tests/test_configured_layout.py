"""Offline current-layout reads do not create SDK or instrument sessions."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import os
import pytest
from culsma_opentrons.layout.interactors import ConfiguredLayoutInteractor
from culsma_opentrons.layout.repository import FileLayoutConfigurationRepository
from culsma_opentrons.profiles import ConfigurationProfileInteractor


@pytest.mark.parametrize('robot',['OT-2','Flex'])
def test_configured_layout_contains_positions_but_no_program_bindings(tmp_path,robot):
    raw=ConfigurationProfileInteractor().describe()['template']
    if robot=='Flex':
        raw.update(robot_type='Flex',pipette='flex_1channel_1000')
        raw['tiprack']={'load_name':'opentrons_flex_96_tiprack_1000ul','slot':'A1'}
        raw['labware']['tubes']['slot']='D1'
    path=tmp_path/'deck.json';path.write_text(json.dumps(raw));before=path.read_bytes()
    result=ConfiguredLayoutInteractor(FileLayoutConfigurationRepository()).execute(path)
    assert len(result['definition']['variants'][robot]['positions'])==12
    assert result['configuration']['robot_type']==robot and 'bindings' not in result['configuration']
    assert set(result['configuration'])=={'robot_type','pipette','tiprack','labware','modules'}
    assert path.read_bytes()==before


def test_reader_imports_no_instrument_sdk(tmp_path):
    path=tmp_path/'deck.json';path.write_text(json.dumps(ConfigurationProfileInteractor().describe()['template']))
    code='''import sys
from culsma_opentrons.layout.interactors import ConfiguredLayoutInteractor
from culsma_opentrons.layout.repository import FileLayoutConfigurationRepository
assert ConfiguredLayoutInteractor(FileLayoutConfigurationRepository()).execute(sys.argv[1])
assert not any(n=='opentrons' or n.startswith('opentrons.') for n in sys.modules)
'''
    result=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,text=True,
        env={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1]/'src')},timeout=10)
    assert result.returncode==0,result.stderr
