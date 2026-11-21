"""Versioned presentation metadata, not calibrated geometry or collision approval."""


class LayoutProfileInteractor:
    def describe(self):
        variants = {}
        for robot in ('OT-2', 'Flex'):
            slots = ([str(n) for n in (10, 11, 12, 7, 8, 9, 4, 5, 6, 1, 2, 3)]
                     if robot == 'OT-2' else [r + str(c) for r in 'ABCD' for c in range(1, 4)])
            reserved = '12' if robot == 'OT-2' else 'A3'
            variants[robot] = {
                'id': 'opentrons-' + robot.lower() + '-schematic-v1',
                'label': robot + ' deck', 'width': 3, 'height': 4,
                'positions': [{'id': slot, 'label': slot, 'x': index % 3,
                    'y': index // 3, 'width': 1, 'height': 1,
                    'accepts': [] if slot == reserved else ['labware', 'tiprack', 'module'],
                    'fixed_label': 'Waste' if slot == reserved else None}
                    for index, slot in enumerate(slots)]}
        return {
            'schema': 'culsma_layout_v1', 'revision': 1, 'schematic': True,
            'selector_path': ['robot_type'], 'variants': variants,
            'collections': [
                {'id': 'labware', 'path': ['labware'], 'keyed': True, 'editable': True,
                 'category': 'labware', 'model_field': 'load_name', 'position_field': 'slot', 'parent_field': 'module'},
                {'id': 'tiprack', 'path': ['tiprack'], 'keyed': False, 'editable': True,
                 'category': 'tiprack', 'model_field': 'load_name', 'position_field': 'slot'},
                {'id': 'modules', 'path': ['modules'], 'keyed': True, 'editable': False,
                 'category': 'module', 'model_field': 'model', 'position_field': 'slot'}],
            'catalogue': [
                {'id': 'opentrons_24_tuberack_eppendorf_2ml_safelock_snapcap',
                 'label': '24-position tube rack · Eppendorf 2 mL', 'category': 'labware'},
                {'id': 'corning_96_wellplate_360ul_flat', 'label': '96-well plate · Corning 360 uL', 'category': 'labware'},
                {'id': 'nest_96_wellplate_200ul_flat', 'label': '96-well plate · NEST 200 uL', 'category': 'labware'},
                {'id': 'nest_96_wellplate_2ml_deep', 'label': '96-well deep plate · NEST 2 mL', 'category': 'labware'},
                {'id': 'nest_96_wellplate_100ul_pcr_full_skirt', 'label': '96-well PCR plate · NEST 100 uL', 'category': 'labware'},
                {'id': 'nest_12_reservoir_15ml', 'label': '12-channel reservoir · NEST 15 mL', 'category': 'labware'},
                *[{'id': name, 'label': label, 'category': 'tiprack', 'when': {'pipette': pipettes}}
                  for name, label, pipettes in [
                    ('opentrons_96_tiprack_300ul', '96-tip rack · 300 uL', ['p300_single_gen2', 'p300_multi_gen2']),
                    ('opentrons_96_tiprack_20ul', '96-tip rack · 20 uL', ['p20_single_gen2', 'p20_multi_gen2']),
                    ('opentrons_flex_96_tiprack_1000ul', 'Flex 96-tip rack · 1000 uL', ['flex_1channel_1000']),
                    ('opentrons_flex_96_tiprack_50ul', 'Flex 96-tip rack · 50 uL', ['flex_1channel_50'])]],
                {'id': 'temperature module gen2', 'label': 'Temperature Module', 'category': 'module'},
                {'id': 'heaterShakerModuleV1', 'label': 'Heater-Shaker', 'category': 'module'},
                {'id': 'magnetic module gen2', 'label': 'Magnetic Module', 'category': 'module'},
                {'id': 'magneticBlockV1', 'label': 'Magnetic Block', 'category': 'module'},
                {'id': 'thermocyclerModuleV2', 'label': 'Thermocycler', 'category': 'module',
                 'footprints': {'7': ['7', '8', '10', '11']}}],
            'scope': 'Schematic proposed placement. Driver validation and resident preflight remain authoritative; no hardware detection or physical move.'}


class ConfiguredLayoutInteractor:
    """Project configured slots only; no robot discovery or material observation."""
    def __init__(self, repository):
        self.repository = repository

    def execute(self, path):
        from ..config import DeckConfiguration
        from copy import deepcopy
        configuration = DeckConfiguration.from_dict(self.repository.read(path))
        return {'definition': LayoutProfileInteractor().describe(),
                'configuration': deepcopy({
                    'robot_type': configuration.robot_type, 'pipette': configuration.pipette,
                    'tiprack': configuration.tiprack, 'labware': configuration.labware,
                    'modules': configuration.modules})}
