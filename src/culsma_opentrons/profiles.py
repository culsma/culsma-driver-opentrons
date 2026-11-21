"""Offline configuration profile: metadata validation, no robot or SDK session."""
from copy import deepcopy
from .config import DeckConfiguration
from .layout.interactors import LayoutProfileInteractor


class ConfigurationProfileInteractor:
    def program_bindings(self, configuration):
        config = DeckConfiguration.from_dict(deepcopy(configuration))
        return {identity: {'labware': address.labware, 'well': address.well}
                for identity, address in config.bindings.items()}

    def describe(self):
        return {'label': 'Opentrons official simulator',
                'layout': LayoutProfileInteractor().describe(),
                'template':{'robot_type':'OT-2','api_level':'2.20','pipette':'p300_single_gen2','mount':'right',
                    'tiprack':{'load_name':'opentrons_96_tiprack_300ul','slot':'2'},
                    'labware':{'tubes':{'load_name':'opentrons_24_tuberack_eppendorf_2ml_safelock_snapcap','slot':'1'}},
                    'bindings':{'entry.s0::alloc':{'labware':'tubes','well':'A1'},'entry.s1::alloc':{'labware':'tubes','well':'A2'}}},
                'fields': [
                    {'path': ['robot_type'], 'label': 'Robot', 'section': 'platform', 'options': ['OT-2','Flex']},
                    {'path': ['api_level'], 'label': 'API level', 'section': 'platform', 'options': ['2.20']},
                    {'path': ['pipette'], 'label': 'Pipette', 'section': 'instruments', 'options': ['p300_single_gen2','p20_single_gen2','p300_multi_gen2','p20_multi_gen2','flex_1channel_1000','flex_1channel_50']},
                    {'path': ['mount'], 'label': 'Mount', 'section': 'instruments', 'options': ['left','right']},
                    {'path': ['tiprack','load_name'], 'label': 'Tip rack definition', 'section': 'layout'},
                    {'path': ['tiprack','slot'], 'label': 'Tip rack slot', 'section': 'layout'},
                    {'path': ['transfer_policy','tip_policy'], 'label': 'Tip policy', 'section': 'transfer',
                     'options': ['fresh_per_stroke'], 'default': 'fresh_per_stroke'},
                    {'path': ['transfer_policy','stroke_mode'], 'label': 'Stroke mode', 'section': 'transfer',
                     'options': ['single','balanced_split'], 'default': 'single'},
                    {'path': ['transfer_policy','aspirate_flow_rate_uL_s'], 'label': 'Aspirate flow rate (uL/s)',
                     'section': 'transfer', 'type': 'number', 'optional': True},
                    {'path': ['transfer_policy','dispense_flow_rate_uL_s'], 'label': 'Dispense flow rate (uL/s)',
                     'section': 'transfer', 'type': 'number', 'optional': True}],
                'maps': [
                    {'path': ['labware'], 'label': 'Labware', 'section': 'layout', 'columns': ['load_name','slot','module']},
                    {'path': ['bindings'], 'label': 'Program bindings', 'section': 'program-bindings', 'columns': ['labware','well']}],
                'summaries': [{'path': ['modules'], 'section': 'instruments', 'label': 'Configured modules',
                               'columns': ['model','slot','adapter']}],
                'advanced': ['modules','labware_moves','reference_devices','reference_environments','transfer_policy'],
                'validation_scope': 'Static plugin rules and labware definitions; not program preflight or live readiness'}

    def validate(self, configuration):
        diagnostics = []
        if isinstance(configuration, dict):
            for name in ['robot_type','api_level','pipette','mount','tiprack','labware','bindings']:
                if name not in configuration:
                    diagnostics.append({'code':'PROFILE_FIELD_REQUIRED','path':'/'+name,'message':'Required field is missing'})
        try:
            config = DeckConfiguration.from_dict(deepcopy(configuration))
        except (ValueError,TypeError,KeyError) as error:
            if not diagnostics:
                diagnostics.append({'code':'PROFILE_CONFIGURATION_INVALID','path':'/','message':str(error)})
            return {'valid':False,'diagnostics':diagnostics,'scope':self.describe()['validation_scope']}
        from opentrons_shared_data.labware import load_definition
        definitions = {}
        for name,spec,path in [('tiprack',config.tiprack,'/tiprack'), *[(name,spec,'/labware/'+name) for name,spec in config.labware.items()]]:
            try:
                definitions[name] = load_definition(spec['load_name'],1)
            except (FileNotFoundError,ValueError,KeyError):
                diagnostics.append({'code':'PROFILE_LABWARE_UNKNOWN','path':path+'/load_name','message':'Labware definition is not installed'})
        for identity,address in config.bindings.items():
            if address.labware in definitions and address.well not in definitions[address.labware]['wells']:
                diagnostics.append({'code':'PROFILE_WELL_UNKNOWN','path':'/bindings/'+identity+'/well','message':'Well is absent from the installed labware definition'})
        return {'valid':not diagnostics,'diagnostics':diagnostics,'scope':self.describe()['validation_scope']}
