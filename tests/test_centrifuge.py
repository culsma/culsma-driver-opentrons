from dataclasses import replace
import json

import pytest

from culsma_opentrons.centrifuge import (
    CentrifugeIntent,
    CentrifugeInteractor,
    CentrifugeProfile,
    CentrifugeSimulationBackend,
    DriveUnit,
    NumericRange,
    RotorGeometry,
    RotorLoad,
    RotorPosition,
)
from culsma_opentrons.models import DriverValidationError, SimulatorBinding, WellAddress
from culsma_opentrons.operations import ResolvedContainerRef


@pytest.fixture
def profile():
    return CentrifugeProfile(
        module_id="rotor", profile_id="reference_cooled_v1", drive_unit=DriveUnit.RCF,
        drive_range=NumericRange(250, 5000), max_duration_seconds=3600,
        labware_type="plate96", max_imbalance_grams=0.5,
        temperature_range_celsius=NumericRange(4, 40),
    )


@pytest.fixture
def request_and_bindings():
    target = ResolvedContainerRef("resolved-sample")
    return (
        CentrifugeIntent("separate", (target,), 300, DriveUnit.RCF, 300, 4),
        (SimulatorBinding(target, WellAddress("assay", "A1")),),
    )


def loaded_rotor(profile, *, counterweight_mass=10):
    backend = CentrifugeSimulationBackend(profile)
    backend.open_lid()
    backend.load(RotorPosition.SAMPLE, RotorLoad("assay", "plate96", 10))
    backend.load(RotorPosition.COUNTERWEIGHT, RotorLoad("balance", "plate96", counterweight_mass))
    backend.close_lid()
    return backend, CentrifugeInteractor(backend)


def test_cold_cycle_and_changed_parameters_use_one_driver(profile, request_and_bindings):
    backend, driver = loaded_rotor(profile)
    intent, bindings = request_and_bindings
    for drive, seconds, temperature, total in [(300, 300, 4, 300), (750, 17, 12, 317)]:
        request = replace(intent, drive_value=drive, duration_seconds=seconds, temperature_celsius=temperature)
        before = backend.read_state()
        prepared = driver.prepare(request, bindings)
        assert backend.read_state() == before
        receipt = driver.execute(prepared)
        assert receipt.outcome == "completed" and receipt.mode == "simulation"
        assert receipt.step_id == intent.step_id
        payload = json.loads(json.dumps(receipt.payload))
        assert payload["cycle_settings"] == {
            "labware": "assay", "drive_value": drive, "drive_unit": "g",
            "duration_seconds": seconds, "temperature_celsius": temperature,
        }
        assert payload["bindings"][0]["reference"]["container_id"] == "resolved-sample"
        assert payload["virtual_elapsed_seconds"] == seconds
        assert payload["virtual_time_seconds"] == total
        assert payload["modeled_temperature_celsius"] == temperature
        assert not payload["sample_temperature_measured"]
        assert backend.read_state().active_cycle is None
        with pytest.raises(DriverValidationError, match="consumed"):
            driver.execute(prepared)


@pytest.mark.parametrize("limits,temperature,code", [
    (None, 4, "TEMPERATURE_UNSUPPORTED"),
    (NumericRange(8, 40), 4, "TEMPERATURE_RANGE"),
    (NumericRange(4, 40), 3.9, "TEMPERATURE_RANGE"),
])
def test_temperature_rejects_before_cycle(profile, request_and_bindings, limits, temperature, code):
    backend, driver = loaded_rotor(replace(profile, temperature_range_celsius=limits))
    intent, bindings = request_and_bindings
    before = backend.read_state()
    with pytest.raises(DriverValidationError) as error:
        driver.prepare(replace(intent, temperature_celsius=temperature), bindings)
    assert error.value.code == f"DRV_OT_CENTRIFUGE_{code}"
    assert backend.read_state() == before


def test_uncooled_model_executes_request_without_temperature(profile, request_and_bindings):
    _, driver = loaded_rotor(replace(
        profile, profile_id="reference_uncooled_v1", temperature_range_celsius=None,
    ))
    intent, bindings = request_and_bindings
    result = driver.execute(driver.prepare(replace(intent, temperature_celsius=None), bindings))
    assert result.outcome == "completed"
    assert result.payload["modeled_temperature_celsius"] is None


def test_rpm_conversion_uses_configured_rotor(profile, request_and_bindings):
    rpm_profile = replace(profile, drive_unit=DriveUnit.RPM, drive_range=NumericRange(100, 5000))
    intent, bindings = request_and_bindings
    _, driver = loaded_rotor(rpm_profile)
    with pytest.raises(DriverValidationError, match="rotor geometry"):
        driver.prepare(intent, bindings)
    _, driver = loaded_rotor(replace(rpm_profile, rotor=RotorGeometry("reference-rotor", 10)))
    receipt = driver.execute(driver.prepare(intent, bindings))
    assert receipt.payload["request"]["drive_value"] == 300
    assert receipt.payload["cycle_settings"]["drive_value"] == pytest.approx(1638, abs=1)
    assert receipt.payload["cycle_settings"]["drive_unit"] == "rpm"
    assert receipt.payload["rotor"]["rotor_id"] == "reference-rotor"


def test_open_lid_unbalanced_rotor_and_stale_preparation(profile, request_and_bindings):
    intent, bindings = request_and_bindings
    backend, driver = loaded_rotor(profile, counterweight_mass=12)
    with pytest.raises(DriverValidationError, match="balance limit"):
        driver.prepare(intent, bindings)
    backend.open_lid()
    backend.unload(RotorPosition.COUNTERWEIGHT)
    backend.load(RotorPosition.COUNTERWEIGHT, RotorLoad("balance", "plate96", 10))
    with pytest.raises(DriverValidationError, match="Close the lid"):
        driver.prepare(intent, bindings)
    backend.close_lid()
    prepared = driver.prepare(intent, bindings)
    backend.open_lid()
    backend.close_lid()
    with pytest.raises(DriverValidationError, match="changed after preparation"):
        driver.execute(prepared)
    assert backend.read_state().virtual_time_seconds == 0


def test_start_requires_loaded_plate_and_complete_plate_scope(profile, request_and_bindings):
    intent, bindings = request_and_bindings
    empty = CentrifugeInteractor(CentrifugeSimulationBackend(profile))
    with pytest.raises(DriverValidationError, match="not loaded"):
        empty.prepare(intent, bindings)
    backend, driver = loaded_rotor(profile)
    bindings += (SimulatorBinding(ResolvedContainerRef("other-sample"), WellAddress("assay", "A2")),)
    with pytest.raises(DriverValidationError, match="all affected"):
        driver.prepare(intent, bindings)
    assert backend.read_state().virtual_time_seconds == 0


def test_failure_after_start_blocks_new_actions(profile, request_and_bindings, monkeypatch):
    backend, driver = loaded_rotor(profile)
    intent, bindings = request_and_bindings
    prepared = driver.prepare(intent, bindings)

    def lose_completion():
        assert backend.read_state().active_cycle is not None
        with pytest.raises(DriverValidationError, match="stopped"):
            backend.open_lid()
        raise TimeoutError("completion unavailable")

    monkeypatch.setattr(backend, "_complete_cycle", lose_completion)
    receipt = driver.execute(prepared)
    assert receipt.outcome == "unknown" and receipt.payload["retry_safe"] is False
    assert backend.read_state().uncertain
    with pytest.raises(DriverValidationError, match="unknown"):
        driver.execute(prepared)
    with pytest.raises(DriverValidationError, match="unknown"):
        driver.prepare(intent, bindings)
    with pytest.raises(DriverValidationError, match="known state"):
        backend.open_lid()
