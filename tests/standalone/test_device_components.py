from dataclasses import replace
from types import SimpleNamespace
import pytest
from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.environment import EnvironmentProfile, EnvironmentRequest, ReferenceEnvironmentInteractor
from culsma_opentrons.mixing import PipetteMixRequest, PipetteMixInteractor
from culsma_opentrons.models import ContainerHandle, SimulatorBinding, DriverValidationError, WellAddress
from culsma_opentrons.operations import ResolvedContainerRef
from culsma_opentrons.plugin import SimulatorSessionFactory
from .conftest import workstation


def test_environment_selection_and_virtual_time():
    config = DeckConfiguration.from_dict(workstation())
    model = ReferenceEnvironmentInteractor(config)
    request = EnvironmentRequest(37, 120, 5)
    assert model.select(request, {'sample'}, {'sample': 'C1'}) == 'incubator'
    receipt = model.execute('hold', request, {'sample'}, {'sample': 'C1'}, ['identity'])
    assert receipt['virtual_time_seconds'] == 120 and not receipt['temperature_measured']
    assert model.execute('next', request, {'sample'}, {'sample': 'C1'}, ['identity'])['virtual_time_seconds'] == 240
    for positions, labwares, transfer in [({'sample': 'D1'}, {'sample'}, False),
            ({'sample': 'C1'}, {'sample'}, True), ({}, set(), False)]:
        with pytest.raises(DriverValidationError): model.select(request, labwares, positions, transfer=transfer)
    duplicate = replace(config, reference_environments={**config.reference_environments, 'duplicate': config.reference_environments['incubator']})
    with pytest.raises(DriverValidationError): ReferenceEnvironmentInteractor(duplicate).select(request, {'sample'}, {'sample': 'C1'})


@pytest.mark.parametrize('values', [(37,-1,5), (37,1,101), (True,1,5), (float('nan'),1,5)])
def test_invalid_environment_requests(values):
    with pytest.raises(ValueError): EnvironmentRequest(*values)


def test_environment_profile_configuration_roundtrip():
    raw = workstation()
    config = DeckConfiguration.from_dict(raw)
    assert DeckConfiguration.from_dict(config.snapshot()).fingerprint() == config.fingerprint()
    profile = EnvironmentProfile.from_dict(raw['reference_environments']['incubator'])
    assert profile.co2_range_percent.includes(5)
    with pytest.raises(ValueError): replace(profile, maximum_hold_seconds=0)
    with pytest.raises(ValueError): replace(profile, allow_transfer='yes')
    with pytest.raises(ValueError): replace(profile, temperature_range_celsius={})
    raw['reference_environments']['incubator']['locations'] = ['not-a-slot']
    with pytest.raises(ValueError): DeckConfiguration.from_dict(raw)


@pytest.fixture
def mix_session(tmp_path):
    config = DeckConfiguration.from_dict(workstation())
    session = SimulatorSessionFactory().create(config, tmp_path)
    reference = ResolvedContainerRef('resolved-sample')
    address = WellAddress('sample','A1')
    session.bind_references((SimulatorBinding(reference, address),))
    request = PipetteMixRequest('mix', ContainerHandle(reference.container_id), address, 80, 100)
    try:
        yield session, PipetteMixInteractor(session.bindings), request
    finally:
        session.close()


def test_mix_preparation_no_actions_and_one_shot_execution(mix_session):
    session, mixer, request = mix_session
    mixer.validate(request)
    before = list(session.backend.protocol.commands())
    token = mixer.prepare(request)
    assert session.backend.protocol.commands() == before
    receipt = mixer.execute(token)
    assert receipt.ok and len(receipt.action_results) == 4
    command = receipt.operation.commands[0]
    assert command.source == command.target and command.volume_uL == 80
    with pytest.raises(DriverValidationError, match='consumed'): mixer.execute(token)


def test_mix_validation_and_position_change(mix_session):
    session, mixer, request = mix_session
    for invalid in [replace(request, volume_uL=120), replace(request, available_uL=3000),
                    replace(request, target=ContainerHandle('unbound'))]:
        with pytest.raises(DriverValidationError): mixer.validate(invalid)
    with pytest.raises(ValueError): replace(request, volume_uL=float('nan'))
    with pytest.raises(ValueError): replace(request, available_uL=True)
    token = mixer.prepare(request)
    session.motion.state['revision'] += 1
    with pytest.raises(DriverValidationError, match='position'): mixer.execute(token)
    assert not session.backend.receipts
