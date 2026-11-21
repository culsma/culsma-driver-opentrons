"""Prepare and execute one-shot typed centrifuge requests."""

from dataclasses import asdict
from threading import RLock
from uuid import uuid4

from ..faults import OpentronsFaultMappingInteractor
from ..models import DriverValidationError, SimulatorBinding, TransferFeedback, WellAddress
from ..operations import ResolvedContainerRef
from .models import CentrifugeIntent, CycleSettings, PreparedCentrifuge
from .ports import CentrifugeBackendPort


class CentrifugeInteractor:
    def __init__(self, backend: CentrifugeBackendPort):
        self.backend = backend
        self._preparations = {}
        self._lock = RLock()
        self._uncertain = False

    def prepare(self, intent, bindings):
        with self._lock:
            return self._prepare(intent, bindings)

    def _prepare(self, intent, bindings):
        if self._uncertain:
            raise DriverValidationError("DRV_OT_CENTRIFUGE_UNKNOWN", "Previous cycle completion is unknown")
        if not isinstance(intent, CentrifugeIntent):
            raise DriverValidationError("DRV_OT_CENTRIFUGE_REQUEST", "A typed centrifuge request is required")
        bindings = tuple(bindings)
        addresses = {}
        for binding in bindings:
            if (
                not isinstance(binding, SimulatorBinding)
                or not isinstance(binding.reference, ResolvedContainerRef)
                or not isinstance(binding.address, WellAddress)
                or not all(isinstance(v, str) and v.strip() for v in (binding.address.labware, binding.address.well))
                or binding.reference in addresses
            ):
                raise DriverValidationError("DRV_OT_CENTRIFUGE_BINDING", "Distinct typed well bindings are required")
            addresses[binding.reference] = binding.address
        if len(set(addresses.values())) != len(addresses):
            raise DriverValidationError("DRV_OT_CENTRIFUGE_BINDING", "Two targets cannot share a well")
        if not set(intent.targets) <= addresses.keys():
            raise DriverValidationError("DRV_OT_CENTRIFUGE_BINDING", "Missing target binding")
        labware_ids = {addresses[target].labware for target in intent.targets}
        if len(labware_ids) != 1:
            raise DriverValidationError("DRV_OT_CENTRIFUGE_BINDING", "One loaded sample plate is required")
        (labware,) = labware_ids
        plate_bindings = tuple(b for b in bindings if b.address.labware == labware)
        if {binding.reference for binding in plate_bindings} != set(intent.targets):
            raise DriverValidationError("DRV_OT_CENTRIFUGE_BINDING", "Declare all affected containers on the plate")

        profile = self.backend.profile
        state = self.backend.read_state()
        if state.counterweight is not None and any(
            address.labware == state.counterweight.labware for address in addresses.values()
        ):
            raise DriverValidationError("DRV_OT_CENTRIFUGE_BINDING", "The counterweight must be an inert load")
        settings = CycleSettings(
            labware, profile.convert_drive(intent.drive_value, intent.drive_unit),
            profile.drive_unit, intent.duration_seconds, intent.temperature_celsius,
        )
        state.validate_cycle(settings, profile)
        prepared = PreparedCentrifuge(
            str(uuid4()), intent, plate_bindings, profile.module_id, profile.fingerprint(), state.revision, settings,
        )
        self._preparations[prepared.preparation_id] = prepared
        return prepared

    def describe(self, prepared):
        return {
            "operation_kind": "centrifuge",
            "module_id": prepared.module_id,
            "resource_requirements": {},
            "request": asdict(prepared.intent),
            "bindings": [asdict(binding) for binding in prepared.bindings],
            "cycle_settings": asdict(prepared.settings),
            "rotor_revision": prepared.rotor_revision,
        }

    def execute(self, prepared):
        with self._lock:
            return self._execute(prepared)

    def _execute(self, prepared):
        if self._uncertain:
            raise DriverValidationError("DRV_OT_CENTRIFUGE_UNKNOWN", "Previous cycle completion is unknown")
        if (
            not isinstance(prepared, PreparedCentrifuge)
            or self._preparations.get(prepared.preparation_id) is not prepared
        ):
            raise DriverValidationError("DRV_OT_CENTRIFUGE_PREPARATION", "Foreign, modified or consumed preparation")
        del self._preparations[prepared.preparation_id]
        profile = self.backend.profile
        if prepared.configuration_revision != profile.fingerprint():
            raise DriverValidationError("DRV_OT_CENTRIFUGE_STALE", "Centrifuge configuration changed")
        payload = {
            **self.describe(prepared),
            "profile_id": profile.profile_id,
            "rotor": asdict(profile.rotor) if profile.rotor is not None else None,
            "evidence_scope": "reference_centrifuge_model",
            "sample_temperature_measured": False,
        }
        try:
            observation = self.backend.run_cycle(prepared.settings, rotor_revision=prepared.rotor_revision)
        except DriverValidationError:
            raise
        except Exception as error:
            self._uncertain = True
            self._preparations.clear()
            outcome = "unknown"
            payload.update(error=f"{type(error).__name__}: {error}", retry_safe=False)
            payload["faults"] = [OpentronsFaultMappingInteractor(schema_version=2).completion_fault(
                error, prepared.intent.step_id, "centrifuge",
                {"module": prepared.module_id, "planned_location": f"module:{prepared.module_id}"},
            )]
        else:
            outcome = "completed"
            self._preparations.clear()  # The rotor revision has advanced.
            payload.update(asdict(observation), rotor_stopped=True)
        return TransferFeedback(
            outcome, "simulation", prepared.intent.step_id, prepared.configuration_revision, {}, payload,
        )
