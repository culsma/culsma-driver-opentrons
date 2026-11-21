"""Named Flex modules share a gripper without losing module-specific access."""
import pytest
from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.plugin import SimulatorSessionFactory
from culsma_opentrons.motion.models import LabwareMoveIntent
from culsma_opentrons.modules.models import TemperatureHoldIntent, OrbitalShakeIntent, MagneticSeparationIntent, MagneticReleaseIntent
from culsma_opentrons.models import DriverValidationError, SimulatorBinding, WellAddress
from culsma_opentrons.operations import ResolvedContainerRef


class WorkstationFixture:
    def config(self):
        return {'robot_type':'Flex','api_level':'2.20','pipette':'flex_1channel_1000','mount':'left',
          'tiprack':{'load_name':'opentrons_flex_96_tiprack_1000ul','slot':'D3'},
          'modules':{'shaker':{'model':'heaterShakerModuleV1','slot':'C1','adapter':'opentrons_96_flat_bottom_adapter'},
                     'heater':{'model':'temperature module gen2','slot':'B1'},
                     'magnet':{'model':'magneticBlockV1','slot':'C3','aspirate_height_mm':2,'release_destination':'D2'}},
          'labware':{'assay':{'load_name':'nest_96_wellplate_200ul_flat','slot':'D1'},
                     'captured':{'load_name':'nest_96_wellplate_2ml_deep','slot':'D2'}},
          'bindings':{'sample':{'labware':'assay','well':'A1'},'beads':{'labware':'captured','well':'A1'}},
          'labware_moves':{'mix':[{'labware':'assay','destination':'module:shaker'}],
                           'warm':[{'labware':'assay','destination':'module:heater'}],
                           'separate':[{'labware':'captured','destination':'module:magnet'}]}}

    def open(self, directory, change=None):
        raw=self.config()
        if change:change(raw)
        return SimulatorSessionFactory().create(DeckConfiguration.from_dict(raw),directory)

    def move(self, session, labware, destination):
        revision=session.motion.state['revision'];source=session.motion.positions()[labware]
        return session.execute_labware_move(session.prepare_labware_move(
            LabwareMoveIntent(f'move-{revision}',labware,source,destination,revision)))

    def bindings(self):
        return (SimulatorBinding(ResolvedContainerRef('sample'),WellAddress('assay','A1')),
                SimulatorBinding(ResolvedContainerRef('beads'),WellAddress('captured','A1')))


def test_three_modules_and_direct_handoff(tmp_path):
    f=WorkstationFixture();session=f.open(tmp_path)
    try:
        assert not session.backend.protocol.commands()
        caps=session.describe_session().processing_capabilities
        assert set(caps['thermal_hold'])=={'heater','shaker'}
        assert caps['thermal_hold']['heater']['labware']==caps['thermal_hold']['shaker']['labware']==['assay']
        assert f.move(session,'assay','module:shaker').outcome=='completed'
        shake=session.prepare_orbital_shake(OrbitalShakeIntent('mix',(ResolvedContainerRef('sample'),),500,1),f.bindings())
        assert session.execute_orbital_shake(shake).outcome=='completed'
        assert f.move(session,'assay','module:heater').outcome=='completed'
        assert session.backend.labware['assay'].parent is session.backend.modules['heater']
        heat=session.prepare_temperature_hold(TemperatureHoldIntent('warm',(ResolvedContainerRef('sample'),),37,1),f.bindings())
        assert session.describe_temperature_hold(heat)['module_id']=='heater'
        assert session.execute_temperature_hold(heat).outcome=='completed'
        assert f.move(session,'assay','D1').outcome=='completed'
        assert f.move(session,'captured','module:magnet').outcome=='completed'
        sep=session.prepare_magnetic_separation(MagneticSeparationIntent('separate',(ResolvedContainerRef('beads'),),1),f.bindings())
        assert session.execute_magnetic_operation(sep).outcome=='completed'
        release=session.prepare_magnetic_separation(MagneticReleaseIntent('release',(ResolvedContainerRef('beads'),),'separate'),())
        assert f.move(session,'captured','D2').outcome=='completed'
        assert session.execute_magnetic_operation(release).outcome=='completed'
        commands=session.backend.protocol.commands()
        assert commands.count('Unlatching labware on Heater-Shaker')==2
        assert len([c for c in commands if 'with gripper' in c])==5
    finally:session.close()


@pytest.mark.parametrize('busy',['source','destination'])
def test_handoff_requires_both_modules_inactive(tmp_path,busy):
    f=WorkstationFixture();session=f.open(tmp_path)
    try:
        assert f.move(session,'assay','module:shaker').outcome=='completed'
        if busy=='source':session.backend.module_access.begin_processing('shaker','other')
        else:session.backend.temperature_access.begin_hold('heater','other')
        before=list(session.backend.protocol.commands())
        with pytest.raises(DriverValidationError):f.move(session,'assay','module:heater')
        assert list(session.backend.protocol.commands())==before
    finally:session.close()


@pytest.mark.parametrize('direction',['flat-to-magnet','deep-to-shaker','module-slot'])
def test_wrong_parent_or_geometry_rejects_before_move(tmp_path,direction):
    f=WorkstationFixture();session=f.open(tmp_path)
    try:
        labware,destination={'flat-to-magnet':('assay','module:magnet'),
                             'deep-to-shaker':('captured','module:shaker'),
                             'module-slot':('assay','C1')}[direction]
        with pytest.raises(DriverValidationError):f.move(session,labware,destination)
        assert not session.backend.protocol.commands()
    finally:session.close()


def test_multiple_temperature_modules_keep_named_identity(tmp_path):
    def change(raw):raw['modules']['heater2']={'model':'temperature module gen2','slot':'A1'}
    f=WorkstationFixture();session=f.open(tmp_path,change)
    try:
        assert f.move(session,'assay','module:heater2').outcome=='completed'
        prepared=session.prepare_temperature_hold(TemperatureHoldIntent('warm',(ResolvedContainerRef('sample'),),37,1),f.bindings())
        assert prepared.module_id=='heater2'
        assert session.execute_temperature_hold(prepared).outcome=='completed'
        assert session.backend.modules['heater'] is not session.backend.modules['heater2']
    finally:session.close()


@pytest.mark.parametrize('problem',['overlap','trash','unsupported','incompatible-route'])
def test_joint_configuration_still_checks_every_module(problem):
    raw=WorkstationFixture().config()
    if problem=='overlap':raw['modules']['magnet']['slot']='C1'
    if problem=='trash':raw['modules']['magnet']['slot']='A3'
    if problem=='unsupported':raw['modules']['heater']['model']='magnetic module gen2'
    if problem=='incompatible-route':raw['labware_moves']['separate'][0]['destination']='module:heater'
    with pytest.raises(ValueError):DeckConfiguration.from_dict(raw)
