from copy import deepcopy
import json
import pytest
from culsma.driver.models import ProgramExecutionRequest
from culsma_opentrons.plugin import SimulatorSessionFactory
from culsma_opentrons.standalone import StandaloneProgramExecutor

PROGRAM = '''let sample = well(label="sample", capacity=2mL,
  load=[content(kind=formulation,type=buffer,code="BUFFER"):100uL]);
let output = well(label="output", capacity=2mL);
sample << [sample:80uL];
with env(thermal=37C,co2=5%,duration=2min) { hold(sample=sample); }
with env(thermal=4C,duration=1min) { sep(sample=sample, program=centrifuge_program(drive=300g)); }
with env(thermal=20C) { output << [sample.contents[0]]; }
'''


def workstation():
    plate = 'nest_96_wellplate_2ml_deep'
    return {'schema_version': 2, 'robot_type': 'Flex', 'api_level': '2.20',
        'pipette': 'flex_1channel_1000', 'mount': 'left',
        'tiprack': {'load_name': 'opentrons_flex_96_tiprack_1000ul', 'slot': 'D3'},
        'labware': {'sample': {'load_name': plate, 'slot': 'D1'}, 'output': {'load_name': plate, 'slot': 'D2'}},
        'bindings': {'entry.s0::alloc': {'labware': 'sample', 'well': 'A1'}, 'entry.s1::alloc': {'labware': 'output', 'well': 'A1'}},
        'reference_devices': {'rotor': {'slot': 'B1', 'profile': {'profile_id': 'generic-plate-rotor',
            'drive_unit': 'rpm', 'drive_range': {'minimum': 100, 'maximum': 10000},
            'max_duration_seconds': 3600, 'labware_type': plate, 'max_imbalance_grams': 0.1,
            'temperature_range_celsius': {'minimum': 4, 'maximum': 40},
            'rotor': {'rotor_id': 'test-rotor', 'radius_cm': 10}},
            'sample_masses_grams': {'sample': 100}, 'counterweight': {'labware': 'balance', 'mass_grams': 100}}},
        'reference_environments': {
            'incubator': {'locations': ['C1'], 'labware_types': [plate],
                'temperature_range_celsius': {'minimum': 20, 'maximum': 40},
                'co2_range_percent': {'minimum': 0, 'maximum': 10}, 'maximum_hold_seconds': 86400},
            'enclosure': {'locations': ['D1', 'D2'], 'labware_types': [plate],
                'temperature_range_celsius': {'minimum': 20, 'maximum': 20},
                'maximum_hold_seconds': 3600, 'allow_transfer': True}},
        'labware_moves': {'entry.s3': [{'labware': 'sample', 'destination': 'C1'}],
            'entry.s4.b.s0': [{'labware': 'sample', 'destination': 'module:rotor'}],
            'entry.s5.b.s0': [{'labware': 'sample', 'destination': 'D1'}]}}


class RecordingSessions(SimulatorSessionFactory):
    def __init__(self):
        self.created = []

    def create(self, *args, **kwargs):
        session = super().create(*args, **kwargs)
        self.created.append(session)
        return session


@pytest.fixture
def scenario(tmp_path):
    program = tmp_path / 'input.culs'
    program.write_text(PROGRAM)
    config = tmp_path / 'deck.json'
    config.write_text(json.dumps(workstation()))
    return ProgramExecutionRequest((program,), 'opentrons-simulator', config, tmp_path / 'run')
