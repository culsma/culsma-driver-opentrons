"""Flex virtual motion evidence, preparation and durable placement boundaries."""
import ast
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import pytest
from culsma_opentrons.centrifuge import CentrifugeIntent, DriveUnit
from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.plugin import SimulatorSessionFactory
from culsma_opentrons.motion.models import LabwareMoveIntent
from culsma_opentrons.models import DriverValidationError, SimulatorBinding, WellAddress
from culsma_opentrons.operations import ResolvedContainerRef, TransferAliquot, TransferIntent


class MotionFixture:
    def config(self):
        return {'robot_type':'Flex','api_level':'2.20','pipette':'flex_1channel_1000','mount':'left',
            'tiprack':{'load_name':'opentrons_flex_96_tiprack_1000ul','slot':'D3'},
            'labware':{'assay':{'load_name':'nest_96_wellplate_200ul_flat','slot':'D1'}},
            'bindings':{'a':{'labware':'assay','well':'A1'},'b':{'labware':'assay','well':'A2'}},
            'labware_moves':{'transfer':[{'labware':'assay','destination':'D2'}]}}

    def open(self, directory):
        return SimulatorSessionFactory().create(DeckConfiguration.from_dict(self.config()), directory)

    def intent(self):
        return LabwareMoveIntent('move','assay','D1','D2',0)


def test_preparation_and_completed_motion(tmp_path):
    session = MotionFixture().open(tmp_path)
    try:
        plate = session.backend.labware['assay']
        well = plate['A1']
        prepared = session.prepare_labware_move(MotionFixture().intent())
        assert not session.backend.protocol.commands()
        receipt = session.execute_labware_move(prepared)
        assert receipt.outcome == 'completed' and receipt.payload['phases'] == {'move':'returned'}
        assert receipt.payload['position_measured'] is False
        assert session.backend.labware['assay'] is plate and plate['A1'] is well
        assert str(plate.parent) == 'D2'
        with pytest.raises(DriverValidationError) as error:
            session.execute_labware_move(prepared)
        assert error.value.code == 'DRV_OT_MOTION_PREPARATION'
    finally:
        session.close()


def test_saved_positions_load_without_replaying_motion(tmp_path):
    session = MotionFixture().open(tmp_path)
    session.execute_labware_move(session.prepare_labware_move(MotionFixture().intent()))
    session.close()
    restarted = MotionFixture().open(tmp_path)
    try:
        assert str(restarted.backend.labware['assay'].parent) == 'D2'
        assert not restarted.backend.protocol.commands() and not restarted.motion.receipts
        assert restarted.describe_session().motion_capabilities['initial_locations']['assay'] == 'D1'
    finally:
        restarted.close()


def test_uncertain_motion_persists_and_blocks_actions(tmp_path, monkeypatch):
    session = MotionFixture().open(tmp_path)
    original = session.backend.protocol.move_labware
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError('completion lost')
    monkeypatch.setattr(session.backend.protocol,'move_labware',lost)
    try:
        receipt = session.execute_labware_move(session.prepare_labware_move(MotionFixture().intent()))
        assert receipt.outcome == 'unknown'
        with pytest.raises(DriverValidationError): session.check_ready()
    finally:
        session.close()
    restarted = MotionFixture().open(tmp_path)
    try:
        with pytest.raises(DriverValidationError): restarted.check_ready()
        assert not restarted.backend.protocol.commands()
    finally:
        restarted.close()


@pytest.mark.parametrize("operation", ["load", "centrifuge"])
def test_reference_dock_preserves_uncertain_operation_on_reopen(tmp_path, monkeypatch, operation):
    raw = MotionFixture().config()
    raw["schema_version"] = 2
    raw["reference_devices"] = {
        "rotor": {
            "slot": "B1",
            "profile": {
                "profile_id": "cooled_rcf",
                "drive_unit": "g",
                "drive_range": {"minimum": 100, "maximum": 10000},
                "max_duration_seconds": 3600,
                "temperature_range_celsius": {"minimum": 4, "maximum": 40},
                "labware_type": raw["labware"]["assay"]["load_name"],
                "max_imbalance_grams": 0.5,
            },
            "sample_masses_grams": {"assay": 20},
            "counterweight": {"labware": "balance", "mass_grams": 20},
        },
    }
    config = DeckConfiguration.from_dict(raw)
    factory = SimulatorSessionFactory()
    session = factory.create(config, tmp_path)

    def lost_completion(*args, **kwargs):
        raise TimeoutError("confirmation lost")

    try:
        rotor = session.centrifuges["rotor"].backend
        move = replace(MotionFixture().intent(), destination="module:rotor")
        if operation == "load":
            monkeypatch.setattr(rotor, "load", lost_completion)
            receipt = session.execute_labware_move(session.prepare_labware_move(move))
            assert receipt.payload["phases"]["move"] == "returned"
            assert session.motion.positions()["assay"] == "D1"
            expected_step, expected_kind = "move", "labware_move"
        else:
            assert session.execute_labware_move(session.prepare_labware_move(move)).outcome == "completed"
            a, b = ResolvedContainerRef("a"), ResolvedContainerRef("b")
            bindings = (SimulatorBinding(a, WellAddress("assay", "A1")),
                        SimulatorBinding(b, WellAddress("assay", "A2")))
            intent = CentrifugeIntent("spin", (a, b), 300, DriveUnit.RCF, 300, 4)
            monkeypatch.setattr(rotor, "_complete_cycle", lost_completion)
            receipt = session.execute_centrifuge(session.prepare_centrifuge(intent, bindings))
            assert rotor.read_state().uncertain
            expected_step, expected_kind = "spin", "centrifuge"
        assert receipt.outcome == "unknown"
        assert receipt.payload["retry_safe"] is False
        fault, = receipt.payload["faults"]
        assert fault["operation_id"] == expected_step
        assert fault["context"] == {"module": "rotor", "planned_location": "module:rotor"}
        assert fault["native_details"]["operation_kind"] == expected_kind
        assert "confirmation lost" in fault["native_details"]["message"]
        assert session.motion.repository.load()["blocking_fault"] == fault
    finally:
        session.close()

    reopened = factory.create(config, tmp_path)
    try:
        with pytest.raises(DriverValidationError, match="confirmation lost") as error:
            reopened.check_ready()
        assert error.value.code == "DRV_OT_COMPLETION_UNCONFIRMED"
        assert f"{expected_kind.replace('_', ' ')} on module 'rotor'" in str(error.value)
        assert f"operation '{expected_step}'" in str(error.value)
        assert not reopened.backend.protocol.commands()
    finally:
        reopened.close()


def test_legacy_uncertain_state_without_fault_remains_blocked(tmp_path):
    fixture = MotionFixture()
    session = fixture.open(tmp_path)
    try:
        session.motion.mark_uncertain()
        assert "blocking_fault" not in session.motion.repository.load()
    finally:
        session.close()
    reopened = fixture.open(tmp_path)
    try:
        with pytest.raises(DriverValidationError, match="Labware placement is not confirmed") as error:
            reopened.check_ready()
        assert error.value.code == "DRV_OT_MOTION_UNCERTAIN"
    finally:
        reopened.close()


def test_transfer_prepared_before_move_is_rejected(tmp_path):
    session = MotionFixture().open(tmp_path)
    try:
        a,b=ResolvedContainerRef('a'),ResolvedContainerRef('b')
        prepared=session.prepare_transfer(TransferIntent('transfer',b,(TransferAliquot(a,50),)),
            (SimulatorBinding(a,WellAddress('assay','A1')),SimulatorBinding(b,WellAddress('assay','A2'))))
        session.execute_labware_move(session.prepare_labware_move(MotionFixture().intent()))
        before=list(session.backend.protocol.commands())
        with pytest.raises(DriverValidationError) as error:
            session.execute_transfer(prepared)
        assert error.value.code == 'DRV_OT_MOTION_STALE_TRANSFER'
        assert session.backend.protocol.commands()==before
    finally:
        session.close()


@pytest.mark.parametrize('problem',['occupied','same','source','foreign','modified','position-revision','configuration'])
def test_invalid_motion_never_submits(tmp_path,problem):
    session=MotionFixture().open(tmp_path)
    try:
        intent=MotionFixture().intent()
        if problem=='occupied': intent=replace(intent,destination='D3')
        if problem=='same': intent=replace(intent,destination='D1')
        if problem=='source': intent=replace(intent,source='C1')
        if problem=='position-revision': intent=replace(intent,position_revision=1)
        if problem=='configuration': session.motion.config.labware_moves['transfer'][0]['destination']='C2'
        if problem in {'foreign','modified'}:
            prepared=session.prepare_labware_move(intent)
            changed=replace(prepared,preparation_id='foreign' if problem=='foreign' else prepared.preparation_id)
            with pytest.raises(DriverValidationError): session.execute_labware_move(changed)
        else:
            with pytest.raises(DriverValidationError): session.prepare_labware_move(intent)
        assert not session.backend.protocol.commands()
    finally:
        session.close()


def test_configuration_and_durable_session_lock(tmp_path):
    fixture=MotionFixture()
    config=fixture.config()
    first=DeckConfiguration.from_dict(config)
    config['labware_moves']['transfer'][0]['destination']='C2'
    assert DeckConfiguration.from_dict(config).fingerprint()!=first.fingerprint()
    session=fixture.open(tmp_path)
    try:
        with pytest.raises((OSError,BlockingIOError)): fixture.open(tmp_path)
    finally:
        session.close()
    config=fixture.config()
    config['robot_type']='OT-2'
    with pytest.raises(ValueError): DeckConfiguration.from_dict(config)


def test_motion_module_architecture():
    root=Path(__file__).resolve().parents[1]/'src/culsma_opentrons/motion'
    assert {p.name for p in root.glob('*.py')}=={'__init__.py','models.py','repository.py','interactors.py','reference_docks.py'}
    for p in root.glob('*.py'):
        tree=ast.parse(p.read_text())
        imports=[n.module or '' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
        assert not any(n.startswith(('culsma_os','opentrons')) for n in imports)
        if p.name=='interactors.py':
            names={a.name for n in ast.walk(tree) if isinstance(n,ast.ImportFrom) for a in n.names}
            assert 'MotionStateRepository' not in names
        assert not any(isinstance(n,ast.FunctionDef) for n in tree.body)


def test_deepwell_plate_profile(tmp_path):
    raw = MotionFixture().config()
    raw['labware']['assay']['load_name'] = 'nest_96_wellplate_2ml_deep'
    session = SimulatorSessionFactory().create(DeckConfiguration.from_dict(raw), tmp_path)
    try:
        result = session.execute_labware_move(session.prepare_labware_move(MotionFixture().intent()))
        assert result.outcome == 'completed'
        assert str(session.backend.labware['assay'].parent) == 'D2'
    finally:
        session.close()


def test_station_state_directory_adopts_old_artifact_once_without_resetting_newer_state(tmp_path):
    legacy=tmp_path/'legacy'
    old=MotionFixture().open(legacy)
    old.execute_labware_move(old.prepare_labware_move(MotionFixture().intent()))
    old.close()
    original=(legacy/'motion-state.json').read_bytes()
    config=DeckConfiguration.from_dict(MotionFixture().config())
    factory=SimulatorSessionFactory()
    state=tmp_path/'station'
    adopted=factory.create(config,legacy,state_dir=state)
    try:
        assert adopted.motion.state['revision']==1
        assert str(adopted.backend.labware['assay'].parent)=='D2'
        assert not adopted.backend.protocol.commands()
        intent=LabwareMoveIntent('return','assay','D2','D1',1)
        assert adopted.execute_labware_move(adopted.prepare_labware_move(intent)).outcome=='completed'
    finally:adopted.close()
    resumed=factory.create(config,legacy,state_dir=state)
    try:
        assert resumed.motion.state['revision']==2
        assert str(resumed.backend.labware['assay'].parent)=='D1'
        assert not resumed.backend.protocol.commands()
    finally:resumed.close()
    fresh=factory.create(config,tmp_path/'new-artifacts',state_dir=state)
    try:
        assert fresh.motion.state['revision']==2 and not fresh.backend.protocol.commands()
    finally:fresh.close()
    assert (legacy/'motion-state.json').read_bytes()==original


def test_station_state_adoption_keeps_unknown_movement(tmp_path,monkeypatch):
    legacy=tmp_path/'legacy'
    old=MotionFixture().open(legacy)
    def lost(*args,**kwargs):raise TimeoutError('unknown move')
    monkeypatch.setattr(old.backend.protocol,'move_labware',lost)
    assert old.execute_labware_move(old.prepare_labware_move(MotionFixture().intent())).outcome=='unknown'
    old.close()
    new=SimulatorSessionFactory().create(DeckConfiguration.from_dict(MotionFixture().config()),legacy,state_dir=tmp_path/'station')
    try:
        assert new.motion.state['uncertain'] and not new.backend.protocol.commands()
        with pytest.raises(DriverValidationError,match='not confirmed'):
            new.check_ready()
    finally:new.close()


def test_conflicting_artifact_state_cannot_replace_station_state(tmp_path):
    import json
    config=DeckConfiguration.from_dict(MotionFixture().config())
    factory=SimulatorSessionFactory();state=tmp_path/'station'
    current=factory.create(config,tmp_path/'first-artifacts',state_dir=state)
    current.execute_labware_move(current.prepare_labware_move(MotionFixture().intent()))
    current.close()
    before=(state/'motion-state.json').read_bytes()
    foreign=tmp_path/'foreign'
    foreign.mkdir()
    stale=json.loads(before);stale['revision']=0;stale['positions']['assay']='D1'
    (foreign/'motion-state.json').write_text(json.dumps(stale))
    with pytest.raises(ValueError,match='MOTION_LEGACY_STATE_CONFLICT'):
        factory.create(config,foreign,state_dir=state)
    assert (state/'motion-state.json').read_bytes()==before


def test_invalid_legacy_state_is_rejected_before_adoption(tmp_path):
    import json
    config=DeckConfiguration.from_dict(MotionFixture().config())
    legacy=tmp_path/'legacy';legacy.mkdir()
    (legacy/'motion-state.json').write_text(json.dumps({'configuration_revision':'wrong'}))
    with pytest.raises(ValueError,match='MOTION_SAVED_CONFIGURATION_MISMATCH'):
        SimulatorSessionFactory().create(config,legacy,state_dir=tmp_path/'station')
    assert not (tmp_path/'station'/'motion-state.json').exists()
