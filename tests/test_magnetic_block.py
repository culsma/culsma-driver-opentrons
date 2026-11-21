"""Passive Flex field: placement + dwell, extraction and verified removal."""
import pytest
from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.plugin import SimulatorSessionFactory
from culsma_opentrons.motion.models import LabwareMoveIntent
from culsma_opentrons.modules.models import MagneticSeparationIntent, MagneticReleaseIntent, MagneticSourceSelection
from culsma_opentrons.models import DriverValidationError, SimulatorBinding, WellAddress
from culsma_opentrons.operations import ResolvedContainerRef, TransferIntent, TransferAliquot


class BlockFixture:
    def config(self):
        return {'robot_type':'Flex','api_level':'2.20','pipette':'flex_1channel_1000','mount':'left',
            'tiprack':{'load_name':'opentrons_flex_96_tiprack_1000ul','slot':'D3'},
            'modules':{'magnet':{'model':'magneticBlockV1','slot':'C1','aspirate_height_mm':2,'release_destination':'D1'}},
            'labware':{'assay':{'load_name':'nest_96_wellplate_2ml_deep','slot':'D1'},
                       'waste':{'load_name':'opentrons_24_tuberack_nest_1.5ml_snapcap','slot':'B1'}},
            'bindings':{'sample':{'labware':'assay','well':'A1'},'waste':{'labware':'waste','well':'A1'}},
            'labware_moves':{'separate':[{'labware':'assay','destination':'module:magnet'}]}}

    def open(self, directory, change=None):
        raw = self.config()
        if change: change(raw)
        return SimulatorSessionFactory().create(DeckConfiguration.from_dict(raw), directory)

    def bindings(self):
        return (SimulatorBinding(ResolvedContainerRef('sample'),WellAddress('assay','A1')),
                SimulatorBinding(ResolvedContainerRef('waste'),WellAddress('waste','A1')))

    def separation(self, session):
        return session.prepare_magnetic_separation(MagneticSeparationIntent('separate',(ResolvedContainerRef('sample'),),30),self.bindings())

    def enter(self, session):
        return session.execute_labware_move(session.prepare_labware_move(LabwareMoveIntent('enter','assay','D1','module:magnet',0)))

    def leave(self, session):
        return session.execute_labware_move(session.prepare_labware_move(LabwareMoveIntent('leave','assay','module:magnet','D1',1)))

    def release(self, session):
        return session.prepare_magnetic_separation(MagneticReleaseIntent('release',(ResolvedContainerRef('sample'),),'separate'),())

    def transfer(self, session):
        return session.prepare_magnetic_transfer(TransferIntent('extract',ResolvedContainerRef('waste'),
            (TransferAliquot(ResolvedContainerRef('sample'),40),)),self.bindings(),MagneticSourceSelection('sample','separate'))


def test_passive_block_protocol(tmp_path):
    f=BlockFixture(); session=f.open(tmp_path)
    try:
        assert f.enter(session).outcome=='completed'
        prepared=f.separation(session)
        assert prepared.labware=='assay'
        receipt=session.execute_magnetic_operation(prepared)
        assert receipt.outcome=='completed',receipt
        assert receipt.payload['phases']=={'verify_docked':'returned','wait':'returned'}
        assert receipt.payload['module_observed_status']=='unavailable_passive_block'
        assert receipt.payload['retained_condition']['release_motion']['destination']=='D1'
        assert session.execute_transfer(f.transfer(session)).outcome=='completed'
        assert f.leave(session).outcome=='completed'
        release=session.execute_magnetic_operation(f.release(session))
        assert release.outcome=='completed' and release.payload['phases']=={'verify_removed':'returned'}
        assert not session.magnetic.held
        assert not any('Engag' in c or 'Disengag' in c for c in session.backend.protocol.commands())
    finally:session.close()


def test_block_requires_docking(tmp_path):
    f=BlockFixture();session=f.open(tmp_path)
    try:
        with pytest.raises(DriverValidationError):f.separation(session)
        assert not session.backend.protocol.commands()
    finally:session.close()


def test_block_release_needs_removal(tmp_path):
    f=BlockFixture();session=f.open(tmp_path)
    try:
        f.enter(session);session.execute_magnetic_operation(f.separation(session))
        receipt=session.execute_magnetic_operation(f.release(session))
        assert receipt.outcome=='unknown'
        assert 'magnet' in session.magnetic.held
        assert session.backend.poisoned
    finally:session.close()


def test_block_restart_does_not_recreate_witness(tmp_path):
    f=BlockFixture();session=f.open(tmp_path)
    f.enter(session);session.execute_magnetic_operation(f.separation(session));session.close()
    restarted=f.open(tmp_path)
    try:
        assert restarted.backend.module_for_labware('assay')=='magnet'
        assert not restarted.backend.protocol.commands()
        with pytest.raises(DriverValidationError): f.transfer(restarted)
        with pytest.raises(DriverValidationError): f.release(restarted)
    finally:restarted.close()


@pytest.mark.parametrize('problem',['ot2','height','plate','release-module','release-trash','release-tips','no-routes'])
def test_block_configuration(problem):
    raw=BlockFixture().config()
    if problem=='ot2':raw['robot_type']='OT-2';raw['pipette']='p300_single_gen2'
    if problem=='height':raw['modules']['magnet']['aspirate_height_mm']=float('nan')
    if problem=='plate':raw['labware']['assay']['load_name']='nest_96_wellplate_200ul_flat'
    if problem=='release-module':raw['modules']['magnet']['release_destination']='C1'
    if problem=='release-trash':raw['modules']['magnet']['release_destination']='A3'
    if problem=='release-tips':raw['modules']['magnet']['release_destination']='D3'
    if problem=='no-routes':raw['labware_moves']={}
    with pytest.raises(ValueError):DeckConfiguration.from_dict(raw)
