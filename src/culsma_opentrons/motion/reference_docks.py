"""Coordinate SDK slot movement with a simulated rotor's access and load state."""

from ..centrifuge import CentrifugeInteractor, CentrifugeSimulationBackend, RotorPosition
from ..models import DriverValidationError


class ReferenceDockInteractor:
    def __init__(self, configuration, positions):
        self.configuration = configuration
        self.drivers = {}
        for name, dock in configuration.items():
            backend = CentrifugeSimulationBackend(dock.profile)
            backend.open_lid()
            backend.load(RotorPosition.COUNTERWEIGHT, dock.counterweight)
            for plate, location in positions.items():
                if location == f"module:{name}":
                    backend.load(RotorPosition.SAMPLE, dock.sample_load(plate))
            backend.close_lid()
            self.drivers[name] = CentrifugeInteractor(backend)

    def endpoints(self, intent):
        for location, outgoing in ((intent.source, True), (intent.destination, False)):
            name = location.removeprefix("module:")
            if location.startswith("module:") and name in self.drivers:
                yield name, outgoing, self.drivers[name].backend

    def validate(self, intent):
        for name, outgoing, backend in self.endpoints(intent):
            state = backend.read_state()
            state.require_idle()
            if outgoing:
                if state.sample is None or state.sample.labware != intent.labware:
                    raise DriverValidationError("DRV_OT_CENTRIFUGE_LOAD", "The rotor does not contain the departing plate")
            elif state.sample is not None or intent.labware not in self.configuration[name].sample_masses_grams:
                raise DriverValidationError("DRV_OT_CENTRIFUGE_LOAD", "The rotor cannot accept the incoming plate")

    def begin(self, intent, payload):
        for name, _, backend in self.endpoints(intent):
            backend.open_lid()
            self.record(payload, name, "open_lid")

    def finish(self, intent, payload):
        for name, outgoing, backend in self.endpoints(intent):
            if outgoing:
                backend.unload(RotorPosition.SAMPLE)
                self.record(payload, name, "unload")
            else:
                load = self.configuration[name].sample_load(intent.labware)
                backend.load(RotorPosition.SAMPLE, load)
                self.record(payload, name, "load")
            backend.close_lid()
            self.record(payload, name, "close_lid")

    @staticmethod
    def record(payload, name, action):
        payload.setdefault("reference_dock_actions", []).append({
            "module_id": name, "action": action, "outcome": "completed",
            "evidence_scope": "reference_centrifuge_model",
        })
