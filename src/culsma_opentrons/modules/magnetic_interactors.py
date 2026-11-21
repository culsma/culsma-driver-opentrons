"""Session-owned command witnesses for the official virtual magnetic module and passive block."""
from dataclasses import asdict
from uuid import uuid4
from ..models import DriverValidationError, TransferFeedback
from .models import MagneticSeparationIntent, MagneticReleaseIntent, PreparedMagneticOperation, MagneticSourceSelection
from .interactors import ModuleConfigurationInteractor, ModuleScopeInteractor


class MagneticModuleFieldInteractor:
    """Motorized OT-2 field commands; preserve the original receipt contract."""
    def __init__(self, backend):
        self.backend = backend

    def describe(self, module_id, labware):
        return {}

    def begin(self, module_id, labware, phases):
        phases["engage"] = "started"
        self.backend.modules[module_id].engage(height_from_base=self.backend.config.modules[module_id]["engage_height_mm"])
        phases["engage"] = "returned"

    def release(self, module_id, labware, phases):
        phases["disengage"] = "started"
        self.backend.modules[module_id].disengage()
        phases["disengage"] = "returned"

    def status(self, module_id):
        return self.backend.modules[module_id].status


class MagneticBlockFieldInteractor:
    """Passive exposure is a placement condition, never a switchable field."""
    def __init__(self, backend):
        self.backend = backend

    def describe(self, module_id, labware):
        return {"release_kind": "remove_labware", "release_motion": {
            "labware": labware, "source": f"module:{module_id}",
            "destination": self.backend.config.modules[module_id]["release_destination"]}}

    def begin(self, module_id, labware, phases):
        if self.backend.module_for_labware(labware) != module_id:
            raise DriverValidationError("DRV_OT_MAGNETIC_NOT_DOCKED", "Plate must be on the passive block")
        phases["verify_docked"] = "returned"

    def release(self, module_id, labware, phases):
        destination = self.backend.config.modules[module_id]["release_destination"]
        if (self.backend.module_for_labware(labware) is not None
                or str(self.backend.labware[labware].parent) != destination):
            raise DriverValidationError("DRV_OT_MAGNETIC_NOT_REMOVED", "Journaled removal must finish before release")
        phases["verify_removed"] = "returned"

    def status(self, module_id):
        return "unavailable_passive_block"


class MagneticSeparationInteractor:
    def __init__(self, backend, bindings):
        self.backend, self.bindings = backend, bindings
        self.preparations, self.held, self.receipts = {}, {}, []
        self.held_labware = {}
        self.fields = {ModuleConfigurationInteractor.magnetic: MagneticModuleFieldInteractor(backend),
                       ModuleConfigurationInteractor.magnetic_block: MagneticBlockFieldInteractor(backend)}

    def prepare(self, intent, bindings):
        self.backend.check_ready()
        if not isinstance(intent, (MagneticSeparationIntent, MagneticReleaseIntent)):
            raise DriverValidationError("DRV_OT_MAGNETIC_REQUEST", "Typed magnetic request required")
        if isinstance(intent, MagneticReleaseIntent):
            candidates = [(name, held) for name, held in self.held.items()
                          if held.step_id == intent.producer_step_id and held.targets == intent.targets]
            if len(candidates) != 1:
                raise DriverValidationError("DRV_OT_MAGNETIC_CONDITION_LOST", "No matching live session field witness")
            module_id = candidates[0][0]
            labware = self.held_labware[module_id]
        else:
            module_id, labware = ModuleScopeInteractor(self.backend, self.bindings).resolve(
                intent, bindings, tuple(self.fields))
        if not isinstance(intent, MagneticReleaseIntent):
            field = self.fields[self.bindings.config.modules[module_id]["model"]]
            removal = field.describe(module_id, labware).get("release_motion")
            if removal is not None and self.backend.motion.positions().get(labware) != removal["source"]:
                raise DriverValidationError("DRV_OT_MAGNETIC_NOT_DOCKED", "Plate must be docked")
        if self.backend.pipette.channels != 1:
            raise DriverValidationError("DRV_OT_MAGNETIC_CHANNELS", "Single-channel magnetic profile required")
        if isinstance(intent, MagneticReleaseIntent):
            self.require_witness(module_id, intent.producer_step_id, intent.targets)
        elif module_id in self.held:
            raise DriverValidationError("DRV_OT_MAGNETIC_ACTIVE", "Release the existing field before another separation")
        prepared = PreparedMagneticOperation(str(uuid4()), intent, module_id, labware, self.bindings.config.fingerprint())
        self.preparations[prepared.preparation_id] = prepared
        return prepared

    def require_witness(self, module_id, producer, targets=None):
        intent = self.held.get(module_id)
        if intent is None or intent.step_id != producer or (targets is not None and intent.targets != targets):
            raise DriverValidationError("DRV_OT_MAGNETIC_CONDITION_LOST", "No matching live session field witness; historical evidence is insufficient")
        return intent

    def require_held(self, module_id, producer, targets=None):
        intent = self.require_witness(module_id, producer, targets)
        labware = self.held_labware[module_id]
        if self.backend.module_for_labware(labware) != module_id:
            raise DriverValidationError("DRV_OT_MAGNETIC_CONDITION_LOST", "Retained field no longer covers this plate")
        return intent

    def describe(self, prepared):
        release = isinstance(prepared.intent, MagneticReleaseIntent)
        value = {"operation_kind": "magnetic_release" if release else "magnetic_separation", "resource_requirements": {},
                 "module_id": prepared.module_id, "affected_labware": prepared.labware, "request": asdict(prepared.intent)}
        if release:
            value["system_effect"] = "condition_release"
        else:
            value["retained_condition"] = {"kind": "magnetic_field", "module_id": prepared.module_id,
                "producer_step_id": prepared.intent.step_id,
                "targets": [t.container_id for t in prepared.intent.targets]}
        field = self.fields[self.bindings.config.modules[prepared.module_id]["model"]]
        if not release:
            value["retained_condition"].update(field.describe(prepared.module_id, prepared.labware))
        return value

    def execute(self, prepared):
        self.backend.check_ready()
        if not isinstance(prepared, PreparedMagneticOperation) or self.preparations.get(prepared.preparation_id) is not prepared:
            raise DriverValidationError("DRV_OT_MAGNETIC_PREPARATION", "Foreign, modified or consumed preparation")
        if self.bindings.config.fingerprint() != prepared.configuration_revision:
            raise DriverValidationError("DRV_OT_MAGNETIC_STALE", "Configuration changed")
        self.preparations.pop(prepared.preparation_id)
        release = isinstance(prepared.intent, MagneticReleaseIntent)
        field = self.fields[self.bindings.config.modules[prepared.module_id]["model"]]
        start = len(self.backend.protocol.commands())
        phases, failure = {}, None
        try:
            if release:
                self.require_witness(prepared.module_id, prepared.intent.producer_step_id, prepared.intent.targets)
                field.release(prepared.module_id, prepared.labware, phases)
                self.held.pop(prepared.module_id)
                self.held_labware.pop(prepared.module_id)
            else:
                field.begin(prepared.module_id, prepared.labware, phases)
                phases["wait"] = "started"
                self.backend.protocol.delay(seconds=prepared.intent.duration_seconds)
                phases["wait"] = "returned"
                self.held[prepared.module_id] = prepared.intent
                self.held_labware[prepared.module_id] = prepared.labware
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            self.backend.poisoned = True
        payload = {**self.describe(prepared), "phases": phases, "commands": self.backend.protocol.commands()[start:],
                   "evidence_scope": "virtual_module_command_observation", "module_observed_status": field.status(prepared.module_id),
                   "field_measured": False, "capture_yield_measured": False, "duration_clock": "simulated", "failure": failure}
        receipt = TransferFeedback("unknown" if failure else "completed", "simulation", prepared.intent.step_id,
                                   prepared.configuration_revision, {}, payload)
        self.receipts.append(receipt)
        return receipt


class MagneticTransferAccessInteractor:
    def __init__(self, backend):
        self.backend = backend

    def module_for(self, address):
        name = self.backend.module_for_labware(address.labware)
        return name if self.backend.config.modules.get(name, {}).get("model") in {ModuleConfigurationInteractor.magnetic, ModuleConfigurationInteractor.magnetic_block} else None

    def validate(self, operation):
        selection = operation.magnetic_selection
        if selection is not None and not isinstance(selection, MagneticSourceSelection):
            raise DriverValidationError("DRV_OT_MAGNETIC_SELECTION", "Typed flowthrough selection required")
        if selection is not None and self.backend.magnetic is None:
            raise DriverValidationError("DRV_OT_MAGNETIC_CONDITION_LOST", "A live magnetic session is required")
        seen = set()
        for command in operation.commands:
            module_id = self.module_for(command.source_address)
            target_module = self.module_for(command.target_address)
            if self.backend.magnetic is not None and target_module in self.backend.magnetic.held:
                raise DriverValidationError("DRV_OT_MAGNETIC_TARGET", "Adding to an active magnetic labware is outside this profile")
            if selection is not None:
                if (module_id is None or command.source.id != selection.source_id or selection.output_slot != "1"):
                    raise DriverValidationError("DRV_OT_MAGNETIC_SELECTION", "Flowthrough source must match one magnetic well")
                held = self.backend.magnetic.require_held(module_id, selection.producer_step_id)
                if command.source.id not in {t.container_id for t in held.targets}:
                    raise DriverValidationError("DRV_OT_MAGNETIC_SELECTION", "Source is outside the retained condition")
                seen.add(module_id)
            elif self.backend.magnetic is not None and module_id in self.backend.magnetic.held:
                raise DriverValidationError("DRV_OT_MAGNETIC_SELECTION", "Active magnetic sources require explicit flowthrough selection")
        if selection is not None and not seen:
            raise DriverValidationError("DRV_OT_MAGNETIC_SELECTION", "No magnetic source")

    def describe(self, operation):
        if operation.magnetic_selection is None:
            return []
        return [{"module_id": name, "action": "verify_retained_magnetic_field"}
                for name in sorted({self.module_for(c.source_address) for c in operation.commands})]

    def observe(self, operation):
        self.validate(operation)
        for item in self.describe(operation):
            self.backend.module_action_results.append({**item, "status": "returned",
                "producer_step_id": operation.magnetic_selection.producer_step_id,
                "module_observed_status": self.backend.magnetic.fields[self.backend.config.modules[item["module_id"]]["model"]].status(item["module_id"]),
                "evidence": ("session_owned_docking_and_wait_returned" if self.backend.config.modules[item["module_id"]]["model"] == ModuleConfigurationInteractor.magnetic_block else "session_owned_engage_and_wait_returned"), "field_measured": False})

    def aspiration_location(self, operation, command):
        well = self.backend.well(command.source_address)
        if operation.magnetic_selection is None:
            return well
        module_id = self.module_for(command.source_address)
        return well.bottom(z=self.backend.config.modules[module_id]["aspirate_height_mm"])
