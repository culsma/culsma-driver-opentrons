import copy
import pytest
from culsma_opentrons.profiles import ConfigurationProfileInteractor
from culsma_opentrons.layout.interactors import LayoutProfileInteractor


def test_layout_definitions_are_detached_and_selected_by_model():
    first = LayoutProfileInteractor().describe()
    assert first['schema'] == 'culsma_layout_v1' and first['schematic']
    assert first['selector_path'] == ['robot_type']
    assert set(first['variants']) == {'OT-2','Flex'}
    for variant in first['variants'].values():
        assert len(variant['positions']) == 12
        assert len({p['id'] for p in variant['positions']}) == 12
    first['variants']['OT-2']['positions'].clear()
    assert len(LayoutProfileInteractor().describe()['variants']['OT-2']['positions']) == 12


def test_catalogue_entries_use_installed_official_labware_definitions():
    from opentrons_shared_data.labware import load_definition
    for item in LayoutProfileInteractor().describe()['catalogue']:
        if item['category'] in {'labware','tiprack'}:
            definition = load_definition(item['id'],1)
            assert definition['wells']


def test_ui_edits_are_accepted_and_duplicate_slots_rejected_by_driver():
    profile = ConfigurationProfileInteractor()
    raw = profile.describe()['template']
    raw['labware']['tubes']['slot'] = '3'
    raw['labware']['extra'] = {'load_name':'corning_96_wellplate_360ul_flat','slot':'4'}
    before = copy.deepcopy(raw)
    assert profile.validate(raw)['valid'] and raw == before
    raw['labware']['extra']['slot'] = '3'
    assert not profile.validate(raw)['valid']


def test_reserved_positions_and_module_footprint_are_declared():
    raw = LayoutProfileInteractor().describe()
    assert next(p for p in raw['variants']['OT-2']['positions'] if p['id']=='12')['accepts'] == []
    assert next(p for p in raw['variants']['Flex']['positions'] if p['id']=='A3')['accepts'] == []
    thermal = next(c for c in raw['catalogue'] if c['id']=='thermocyclerModuleV2')
    assert thermal['footprints']['7'] == ['7','8','10','11']
