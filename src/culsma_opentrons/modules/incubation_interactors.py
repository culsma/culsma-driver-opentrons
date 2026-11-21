"""Heater-Shaker scalar incubation, independent of shaking and scientific results."""

from dataclasses import asdict
from uuid import uuid4

from ..models import DriverValidationError, TransferFeedback
from .interactors import ModuleConfigurationInteractor, ModuleScopeInteractor
from .models import PreparedTemperatureHold, TemperatureHoldIntent


class HeaterShakerHoldInteractor:
    def __init__(self, backend, bindings):
        self.backend, self.bindings = backend, bindings
        self.preparations = {}
        self.receipts = []

    def prepare(self, intent, bindings):
        self.backend.check_ready()
        if not isinstance(intent, TemperatureHoldIntent):
            raise DriverValidationError("DRV_OT_MODULE_REQUEST", "Typed temperature hold required")
        if not 37 <= intent.temperature_celsius <= 95 or int(intent.temperature_celsius) != intent.temperature_celsius:
            raise DriverValidationError("DRV_OT_MODULE_TEMPERATURE", "Heater-Shaker requires integral 37-95C")
        module_id, labware = ModuleScopeInteractor(self.backend, self.bindings).resolve(
            intent, bindings, ModuleConfigurationInteractor.heater_shaker)
        self.backend.module_access.require_idle(module_id)
        if self.backend.pipette.has_tip:
            raise DriverValidationError("DRV_OT_TIP_ATTACHED", "Latching requires no attached tip")
        prepared = PreparedTemperatureHold(str(uuid4()), intent, module_id, labware, self.bindings.config.fingerprint())
        self.preparations[prepared.preparation_id] = prepared
        return prepared

    def describe(self, prepared):
        return {"operation_kind": "thermal_hold", "resource_requirements": {},
                "module_id": prepared.module_id, "affected_labware": prepared.labware,
                "request": asdict(prepared.intent), "postcondition": "heater_deactivated"}

    def execute(self, prepared):
        self.backend.check_ready()
        if (not isinstance(prepared, PreparedTemperatureHold)
                or self.preparations.get(prepared.preparation_id) is not prepared):
            raise DriverValidationError("DRV_OT_MODULE_PREPARATION", "Foreign, modified or consumed preparation")
        if self.bindings.config.fingerprint() != prepared.configuration_revision:
            raise DriverValidationError("DRV_OT_MODULE_STALE", "Configuration changed after preparation")
        if self.backend.module_for_labware(prepared.labware) != prepared.module_id:
            raise DriverValidationError("DRV_OT_MODULE_BINDING", "Plate moved after preparation")
        self.backend.module_access.require_idle(prepared.module_id)
        if self.backend.pipette.has_tip:
            raise DriverValidationError("DRV_OT_TIP_ATTACHED", "Latching requires no attached tip")
        self.preparations.pop(prepared.preparation_id)
        module = self.backend.modules[prepared.module_id]
        start = len(self.backend.protocol.commands())
        phases, failure = {}, None
        try:
            self.backend.module_access.begin_processing(prepared.module_id, prepared.intent.step_id)
            phases["close_latch"] = "started"
            module.close_labware_latch()
            self.backend.module_access.require_closed(prepared.module_id)
            phases["close_latch"] = "returned"
            phases["set_temperature"] = "started"
            module.set_and_wait_for_temperature(prepared.intent.temperature_celsius)
            phases["set_temperature"] = "returned"
            phases["hold"] = "started"
            self.backend.protocol.delay(seconds=prepared.intent.duration_seconds)
            phases["hold"] = "returned"
            phases["deactivate_heater"] = "started"
            module.deactivate_heater()
            phases["deactivate_heater"] = "returned"
            self.backend.module_access.finish_processing(prepared.module_id, prepared.intent.step_id)
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            self.backend.poisoned = True
        payload = {**self.describe(prepared), "commands": self.backend.protocol.commands()[start:],
                   "phases": phases, "evidence_scope": "virtual_module_command_observation",
                   "sample_temperature_measured": False, "duration_clock": "simulated",
                   "readiness_evidence": "blocking_set_and_wait_for_temperature_returned"
                       if phases.get("set_temperature") == "returned" else "unknown",
                   "module_end_status": module.temperature_status, "failure": failure}
        receipt = TransferFeedback("unknown" if failure else "completed", "simulation", prepared.intent.step_id,
                                   prepared.configuration_revision, {}, payload)
        self.receipts.append(receipt)
        return receipt
