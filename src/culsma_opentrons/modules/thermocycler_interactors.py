"""Scalar block holds and pipetting access through the official virtual API."""

from dataclasses import asdict
from uuid import uuid4

from ..models import DriverValidationError, TransferFeedback
from .interactors import ModuleConfigurationInteractor, ModuleScopeInteractor
from .models import PreparedTemperatureHold, TemperatureHoldIntent


class ThermocyclerAccessInteractor:
    def __init__(self, backend):
        self.backend = backend

    def require_inactive(self, module_id):
        module = self.backend.modules[module_id]
        if (module.block_temperature_status != "idle" or module.block_target_temperature is not None
                or module.lid_temperature_status != "idle" or module.lid_target_temperature is not None):
            raise DriverValidationError("DRV_OT_MODULE_BUSY", "Thermocycler block and lid must be inactive")
        if self.backend.pipette.has_tip:
            raise DriverValidationError("DRV_OT_TIP_ATTACHED", "Thermocycler lid movement requires no attached tip")

    def modules_for(self, operation):
        labware = {address.labware for command in operation.commands
                   for address in (command.source_address, command.target_address)}
        return tuple(sorted({self.backend.config.labware[name]["module"] for name in labware
            if "module" in self.backend.config.labware[name]
            and self.backend.config.modules[self.backend.config.labware[name]["module"]]["model"]
            == ModuleConfigurationInteractor.thermocycler}))

    def validate(self, operation):
        for module_id in self.modules_for(operation):
            self.require_inactive(module_id)

    def describe(self, operation):
        return [{"module_id": name, "action": "open_lid"} for name in self.modules_for(operation)]

    def execute(self, operation):
        self.validate(operation)
        for item in self.describe(operation):
            observation = {**item, "status": "completion_unknown"}
            self.backend.module_action_results.append(observation)
            module = self.backend.modules[item["module_id"]]
            module.open_lid()
            observation.update(status="returned", lid_observed_position=module.lid_position,
                               evidence="blocking_open_lid_returned")


class ThermocyclerHoldInteractor:
    def __init__(self, backend, bindings):
        self.backend, self.bindings = backend, bindings
        self.preparations = {}
        self.receipts = []

    def maximum_volume(self, intent, bindings):
        volumes = {item.reference: item.volume_uL for item in intent.material_volumes}
        if (len(volumes) != len(intent.material_volumes) or set(volumes) != set(intent.targets)
                or any(volume is None for volume in volumes.values())):
            raise DriverValidationError("DRV_OT_MODULE_VOLUME", "Known volumes for every thermal target are required")
        addresses = {binding.reference: binding.address for binding in bindings}
        for reference, volume in volumes.items():
            if not 0 <= volume <= min(100, self.backend.well(addresses[reference]).max_volume):
                raise DriverValidationError("DRV_OT_MODULE_VOLUME", "Thermocycler requires known 0-100uL well volumes")
        return float(max(volumes.values()))

    def prepare(self, intent, bindings):
        self.backend.check_ready()
        if not isinstance(intent, TemperatureHoldIntent):
            raise DriverValidationError("DRV_OT_MODULE_REQUEST", "Typed temperature hold required")
        temperature = intent.temperature_celsius
        if not 4 <= temperature <= 99 or int(temperature) != temperature:
            raise DriverValidationError("DRV_OT_MODULE_TEMPERATURE", "Scalar hold requires integral 4-99C")
        module_id, labware = ModuleScopeInteractor(self.backend, self.bindings).resolve(
            intent, bindings, ModuleConfigurationInteractor.thermocycler)
        self.backend.thermocycler_access.require_inactive(module_id)
        maximum_volume = self.maximum_volume(intent, bindings)
        prepared = PreparedTemperatureHold(str(uuid4()), intent, module_id, labware,
                                           self.bindings.config.fingerprint(), maximum_volume)
        self.preparations[prepared.preparation_id] = prepared
        return prepared

    def describe(self, prepared):
        spec = self.bindings.config.modules[prepared.module_id]
        closed = spec.get("keep_lid_closed", False)
        heated = "lid_temperature_celsius" in spec
        return {"operation_kind": "thermal_hold", "resource_requirements": {},
                "module_id": prepared.module_id, "affected_labware": prepared.labware,
                "request": asdict(prepared.intent), "postcondition": "block_and_lid_deactivated_lid_closed" if closed else "block_deactivated_and_open_lid_returned",
                "block_max_volume_uL": prepared.block_max_volume_uL, "volume_basis": "language_material_state",
                "lid_heating": "explicit_setpoint" if heated else "not_requested",
                "lid_temperature_celsius": spec.get("lid_temperature_celsius"), "keep_lid_closed": closed}

    def execute(self, prepared):
        self.backend.check_ready()
        if (not isinstance(prepared, PreparedTemperatureHold)
                or self.preparations.get(prepared.preparation_id) is not prepared):
            raise DriverValidationError("DRV_OT_MODULE_PREPARATION", "Foreign, modified or consumed preparation")
        if self.bindings.config.fingerprint() != prepared.configuration_revision:
            raise DriverValidationError("DRV_OT_MODULE_STALE", "Configuration changed after preparation")
        self.backend.thermocycler_access.require_inactive(prepared.module_id)
        self.preparations.pop(prepared.preparation_id)
        module = self.backend.modules[prepared.module_id]
        start = len(self.backend.protocol.commands())
        phases, failure = {}, None
        spec = self.bindings.config.modules[prepared.module_id]
        try:
            phases["close_lid"] = "started"
            module.close_lid()
            phases["close_lid"] = "returned"
            if "lid_temperature_celsius" in spec:
                phases["heat_lid"] = "started"
                module.set_lid_temperature(spec["lid_temperature_celsius"])
                phases["heat_lid"] = "returned"
            phases["block_hold"] = "started"
            module.set_block_temperature(prepared.intent.temperature_celsius,
                hold_time_seconds=prepared.intent.duration_seconds, block_max_volume=prepared.block_max_volume_uL)
            phases["block_hold"] = "returned"
            phases["deactivate_block"] = "started"
            module.deactivate_block()
            phases["deactivate_block"] = "returned"
            if "lid_temperature_celsius" in spec:
                phases["deactivate_lid"] = "started"
                module.deactivate_lid()
                phases["deactivate_lid"] = "returned"
            if not spec.get("keep_lid_closed", False):
                phases["open_lid"] = "started"
                module.open_lid()
                phases["open_lid"] = "returned"
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            self.backend.poisoned = True
        payload = {**self.describe(prepared), "commands": self.backend.protocol.commands()[start:],
                   "phases": phases, "evidence_scope": "virtual_module_command_observation",
                   "sample_temperature_measured": False, "duration_clock": "simulated",
                   "readiness_evidence": "blocking_block_hold_returned" if phases.get("block_hold") == "returned" else "unknown",
                   "lid_observed_position": module.lid_position,
                   "block_observed_status": module.block_temperature_status,
                   "lid_observed_status": module.lid_temperature_status,
                   "failure": failure}
        receipt = TransferFeedback("unknown" if failure else "completed", "simulation", prepared.intent.step_id,
                                   prepared.configuration_revision, {}, payload)
        self.receipts.append(receipt)
        return receipt
