"""Device boundary for centrifugation."""

from typing import Protocol

from .models import CentrifugeProfile, CentrifugeState, CycleObservation, CycleSettings


class CentrifugeBackendPort(Protocol):
    @property
    def profile(self) -> CentrifugeProfile: ...

    def read_state(self) -> CentrifugeState: ...

    def run_cycle(self, settings: CycleSettings, *, rotor_revision: int) -> CycleObservation:
        """A DriverValidationError means rejection before any cycle action."""
        ...
