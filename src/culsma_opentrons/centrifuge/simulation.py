"""Reference rotor and temperature behavior with an explicit virtual clock."""

from dataclasses import replace
from threading import RLock

from ..models import DriverValidationError
from .models import CentrifugeProfile, CentrifugeState, CycleObservation, CycleSettings, RotorLoad, RotorPosition


class CentrifugeSimulationBackend:
    def __init__(self, profile: CentrifugeProfile):
        if not isinstance(profile, CentrifugeProfile):
            raise TypeError("A typed centrifuge profile is required")
        self._profile = profile
        self._state = CentrifugeState()
        self._lock = RLock()

    @property
    def profile(self):
        return self._profile

    def read_state(self):
        with self._lock:
            return self._state

    def _update(self, **changes):
        self._state = replace(self._state, revision=self._state.revision + 1, **changes)

    def open_lid(self):
        with self._lock:
            self._state.require_idle()
            if not self._state.lid_open:
                self._update(lid_open=True)

    def close_lid(self):
        with self._lock:
            self._state.require_idle()
            if self._state.lid_open:
                self._update(lid_open=False)

    def _require_access(self, position):
        if not isinstance(position, RotorPosition):
            raise DriverValidationError("DRV_OT_CENTRIFUGE_POSITION", "A typed rotor position is required")
        self._state.require_idle()
        if not self._state.lid_open:
            raise DriverValidationError("DRV_OT_CENTRIFUGE_LID", "Open the lid before accessing the rotor")

    def load(self, position, load):
        with self._lock:
            self._require_access(position)
            if not isinstance(load, RotorLoad) or load.labware_type != self.profile.labware_type:
                raise DriverValidationError("DRV_OT_CENTRIFUGE_LABWARE", "Incompatible rotor load")
            if getattr(self._state, position.value) is not None:
                raise DriverValidationError("DRV_OT_CENTRIFUGE_LOAD", "Rotor position is occupied")
            if any(item is not None and item.labware == load.labware for item in (self._state.sample, self._state.counterweight)):
                raise DriverValidationError("DRV_OT_CENTRIFUGE_LOAD", "Labware is already in the rotor")
            self._update(**{position.value: load})

    def unload(self, position):
        with self._lock:
            self._require_access(position)
            loaded = getattr(self._state, position.value)
            if loaded is None:
                raise DriverValidationError("DRV_OT_CENTRIFUGE_LOAD", "Rotor position is empty")
            self._update(**{position.value: None})
            return loaded

    def run_cycle(self, settings: CycleSettings, *, rotor_revision: int) -> CycleObservation:
        with self._lock:
            if not isinstance(settings, CycleSettings):
                raise DriverValidationError("DRV_OT_CENTRIFUGE_REQUEST", "Typed cycle settings are required")
            if rotor_revision != self._state.revision:
                raise DriverValidationError("DRV_OT_CENTRIFUGE_STALE", "Rotor changed after preparation")
            self._state.validate_cycle(settings, self.profile)
            try:
                self._update(
                    active_cycle=settings,
                    modeled_temperature_celsius=settings.temperature_celsius,
                )
                self._complete_cycle()
            except Exception as error:
                self._update(uncertain=True)
                raise RuntimeError(f"The simulated cycle could not be confirmed: {error}") from error
            return CycleObservation(
                settings.duration_seconds, self._state.virtual_time_seconds,
                self._state.modeled_temperature_celsius, self._state.revision,
            )

    def _complete_cycle(self):
        cycle = self._state.active_cycle
        self._update(
            active_cycle=None,
            virtual_time_seconds=self._state.virtual_time_seconds + cycle.duration_seconds,
        )
