"""Flex carrier movement and explicit virtual position continuity."""
from copy import deepcopy
from dataclasses import asdict
from uuid import uuid4
from ..faults import OpentronsFaultMappingInteractor
from ..models import DriverValidationError, TransferFeedback
from .models import LabwareMoveIntent, PreparedLabwareMove
from .repository import MotionStateRepositoryPort


class MotionConfigurationInteractor:
    plate_profiles = frozenset({"nest_96_wellplate_200ul_flat", "nest_96_wellplate_2ml_deep"})
    slots = frozenset(f"{row}{column}" for row in "ABCD" for column in range(1, 4))

    def parse(self, raw, robot_type, labware, modules=None, reference_devices=None):
        modules = modules or {}
        docks = self.docks(modules, labware, reference_devices)
        destinations = self.destinations(docks, reference_devices)
        if not isinstance(raw, dict):
            raise ValueError("MOTION_ROUTES_INVALID")
        if raw and robot_type != "Flex":
            raise ValueError("MOTION_REQUIRES_FLEX")
        result = {}
        for step_id, routes in raw.items():
            if not isinstance(step_id, str) or not step_id or not isinstance(routes, list) or not routes:
                raise ValueError("MOTION_ROUTES_INVALID")
            values = []
            for route in routes:
                if (not isinstance(route, dict) or set(route) != {"labware", "destination"}
                        or route.get("labware") not in labware or route.get("destination") not in destinations
                        or labware[route["labware"]].get("load_name") not in self.plate_profiles):
                    raise ValueError("MOTION_ROUTE_UNSUPPORTED")
                if route["destination"] in docks and route["labware"] not in docks[route["destination"]]["labware"]:
                    raise ValueError("MOTION_DOCK_INCOMPATIBLE")
                values.append(dict(route))
            result[step_id] = values
        return result

    def docks(self, modules, labware, reference_devices=None):
        result = {}
        for name, spec in modules.items():
            if spec["model"] not in {"temperature module gen2", "heaterShakerModuleV1", "magneticBlockV1"}:
                continue
            if spec["model"] == "heaterShakerModuleV1" and "adapter" not in spec:
                continue
            dock = {"module_id": name, "labware": sorted(key for key, item in labware.items()
                        if item["load_name"] == ("nest_96_wellplate_2ml_deep" if spec["model"] == "magneticBlockV1" else "nest_96_wellplate_200ul_flat"))}
            if spec["model"] == "heaterShakerModuleV1":
                dock["adapter"] = spec["adapter"]
            result[f"module:{name}"] = dock
        for name, dock in (reference_devices or {}).items():
            result[f"module:{name}"] = {"module_id": name, "labware": sorted(dock.sample_masses_grams)}
        return result

    def destinations(self, docks, reference_devices):
        reserved = {dock.slot for dock in (reference_devices or {}).values()}
        return (self.slots - reserved) | docks.keys()

    def initial_locations(self, config):
        return {**{name: f"module:{spec['module']}" if "module" in spec else spec["slot"]
                   for name, spec in config.labware.items()},
                **{f"@module:{name}": spec["slot"] for name, spec in config.modules.items()},
                "@tiprack": config.tiprack["slot"], "@trash": "A3"}

    def capabilities(self, config):
        if not config.labware_moves:
            return {}
        docks = self.docks(config.modules, config.labware, config.reference_devices)
        return {"initial_locations": self.initial_locations(config), "destinations": sorted(self.destinations(docks, config.reference_devices)),
                **({"docks": docks} if docks else {}),
                "movable_labware": sorted(name for name, spec in config.labware.items() if spec["load_name"] in self.plate_profiles),
                "before_steps": deepcopy(config.labware_moves)}


class MotionModuleAccessInteractor:
    """Own module access prerequisites inside the already-authorized motion effect."""
    def __init__(self, backend):
        self.backend = backend

    def modules_for(self, intent):
        return tuple(sorted({location.split(":", 1)[1] for location in (intent.source, intent.destination)
                             if location.startswith("module:") and location.split(":", 1)[1] in self.backend.modules}))

    def requirements(self, intent):
        return [f"module:{name}:open_labware_latch" for name in self.modules_for(intent)
                if self.backend.config.modules[name]["model"] == "heaterShakerModuleV1"]

    def validate(self, intent):
        for name in self.modules_for(intent):
            if self.backend.config.modules[name]["model"] == "heaterShakerModuleV1":
                self.backend.module_access.require_idle(name)
            elif self.backend.config.modules[name]["model"] == "magneticBlockV1":
                if (self.backend.magnetic is not None and name in self.backend.magnetic.held
                        and (intent.source != f"module:{name}" or intent.destination != self.backend.config.modules[name]["release_destination"])):
                    raise DriverValidationError("DRV_OT_MAGNETIC_RELEASE_ROUTE", "An active block plate may move only to its release slot")
            elif self.backend.config.modules[name]["model"] == "temperature module gen2":
                self.backend.temperature_access.require_idle(name)

    def execute(self, intent, payload):
        self.validate(intent)
        for name in self.modules_for(intent):
            if self.backend.config.modules[name]["model"] != "heaterShakerModuleV1":
                continue
            result = {"module_id": name, "action": "open_labware_latch", "status": "completion_unknown"}
            payload.setdefault("module_action_results", []).append(result)
            module = self.backend.modules[name]
            module.open_labware_latch()
            if module.labware_latch_status not in {"idle_open", "idle_unknown"}:
                raise RuntimeError("MOTION_LATCH_OPEN_UNCONFIRMED")
            result.update(status="returned", latch_observed_status=module.labware_latch_status,
                          evidence="blocking_open_latch_returned", latch_position_measured=False)
            payload.setdefault("completed_access", []).append(f"module:{name}:open_labware_latch")


class MotionStateAdoptionInteractor:
    """Adopt a validated old artifact once; conflicting histories require explicit review."""
    def __init__(self, repository: MotionStateRepositoryPort):
        self.repository = repository

    def select(self, legacy_path):
        saved = self.repository.load()
        legacy = self.repository.legacy(legacy_path)
        if legacy is None:
            return saved, False
        source, digest, previous = legacy
        imports = dict(saved.get('imported_sources', {})) if saved is not None else {}
        if source in imports:
            if imports[source] != digest:
                raise ValueError('MOTION_LEGACY_STATE_CHANGED')
            return saved, False
        current = {k:v for k,v in saved.items() if k != 'imported_sources'} if saved is not None else None
        original = {k:v for k,v in previous.items() if k != 'imported_sources'}
        if current is not None and current != original:
            raise ValueError('MOTION_LEGACY_STATE_CONFLICT')
        imports[source] = digest
        return {**previous, 'imported_sources': imports}, True


class LabwareMotionInteractor:
    def __init__(self, config, repository: MotionStateRepositoryPort, saved=None):
        self.config, self.repository = config, repository
        self.backend = None
        self.preparations, self.receipts = {}, []
        self.transfer_preparations = {}
        self.processing_preparations = {}
        saved = repository.load() if saved is None else saved
        configuration = MotionConfigurationInteractor()
        initial = configuration.initial_locations(config)
        docks = configuration.docks(config.modules, config.labware, config.reference_devices)
        destinations = configuration.destinations(docks, config.reference_devices)
        if saved is None:
            saved = {"configuration_revision": config.fingerprint(), "positions": initial, "revision": 0, "uncertain": False}
        if (not isinstance(saved, dict) or saved.get("configuration_revision") != config.fingerprint()
                or not isinstance(saved.get("positions"), dict) or set(saved["positions"]) != set(initial)
                or any(v not in destinations for v in saved["positions"].values())
                or len(set(saved["positions"].values())) != len(initial)
                or type(saved.get("revision")) is not int or saved["revision"] < 0
                or type(saved.get("uncertain")) is not bool
                or any(saved["positions"][k] != v for k, v in initial.items() if k.startswith("@"))):
            raise ValueError("MOTION_SAVED_CONFIGURATION_MISMATCH")
        for name, location in saved["positions"].items():
            dock = docks.get(location)
            if dock is not None and name not in dock["labware"]:
                raise ValueError("MOTION_SAVED_DOCK_INCOMPATIBLE")
        self.state = deepcopy(saved)

    def attach(self, backend):
        self.backend = backend
        backend.motion = self
        self.module_access = MotionModuleAccessInteractor(backend)
        backend.poisoned = backend.poisoned or self.state["uncertain"]
        from .reference_docks import ReferenceDockInteractor
        self.reference_docks = ReferenceDockInteractor(self.config.reference_devices, self.positions())

    def mark_uncertain(self, fault=None):
        self.state["uncertain"] = True
        if fault is not None:
            self.state["blocking_fault"] = deepcopy(fault)
        self.repository.save(self.state)

    def confirm_position(self):
        updated = {**self.state, "uncertain": False}
        updated.pop("blocking_fault", None)
        self.repository.save(updated)
        self.state = updated

    def positions(self):
        return deepcopy(self.state["positions"])

    def require_known(self):
        if self.state["configuration_revision"] != self.config.fingerprint():
            raise DriverValidationError("DRV_OT_MOTION_CONFIGURATION_STALE", "Configured movement profile changed")
        if self.state["uncertain"]:
            fault = self.state.get("blocking_fault")
            if fault is not None:
                details = fault["native_details"]
                operation = details["operation_kind"].replace("_", " ")
                module = fault["context"].get("module")
                device = f" on module '{module}'" if module else ""
                raise DriverValidationError(
                    details["code"],
                    f"Completion is not confirmed for {operation}{device} "
                    f"(operation '{fault['operation_id']}'): "
                    f"{details['exception_type']}: {details['message']}",
                )
            raise DriverValidationError("DRV_OT_MOTION_UNCERTAIN", "Labware placement is not confirmed")

    def pin_transfer(self, prepared):
        self.require_known()
        self.transfer_preparations[prepared.preparation_id] = (prepared, self.state["revision"])
        return prepared

    def consume_transfer(self, prepared):
        self.require_known()
        previous = self.transfer_preparations.pop(prepared.preparation_id, None)
        if previous is None or previous[0] is not prepared or previous[1] != self.state["revision"]:
            raise DriverValidationError("DRV_OT_MOTION_STALE_TRANSFER", "Labware moved after transfer preparation")

    def pin_processing(self, prepared):
        self.require_known()
        self.processing_preparations[prepared.preparation_id] = (prepared, self.state["revision"])
        return prepared

    def consume_processing(self, prepared):
        self.require_known()
        previous = self.processing_preparations.pop(prepared.preparation_id, None)
        if previous is None or previous[0] is not prepared or previous[1] != self.state["revision"]:
            raise DriverValidationError("DRV_OT_MOTION_STALE_PROCESSING", "Labware moved after processing preparation")

    def current_location(self, labware):
        module = self.backend.module_for_labware(labware)
        if module is not None:
            return f"module:{module}"
        slot = str(self.backend.labware[labware].parent)
        return next((f"module:{name}" for name, dock in self.config.reference_devices.items()
                     if dock.slot == slot), slot)


    def validate(self, intent):
        self.require_known()
        if (not isinstance(intent, LabwareMoveIntent) or any(not isinstance(v, str) or not v.strip()
                for v in (intent.step_id, intent.labware, intent.source, intent.destination))):
            raise DriverValidationError("DRV_OT_MOTION_REQUEST", "Typed movement required")
        self.backend.check_ready()
        current = self.state["positions"]
        if type(intent.position_revision) is not int or intent.position_revision != self.state["revision"]:
            raise DriverValidationError("DRV_OT_MOTION_STALE", "Position revision changed")
        config = MotionConfigurationInteractor()
        docks = config.docks(self.config.modules, self.config.labware, self.config.reference_devices)
        if (intent.labware not in self.backend.labware
                or self.config.labware[intent.labware]["load_name"] not in MotionConfigurationInteractor.plate_profiles
                or current[intent.labware] != intent.source or self.current_location(intent.labware) != intent.source
                or intent.destination not in config.destinations(docks, self.config.reference_devices)
                or intent.destination in current.values()):
            raise DriverValidationError("DRV_OT_MOTION_LOCATION", "Stale, occupied or unsupported movement")
        dock = docks.get(intent.destination)
        if dock is not None and intent.labware not in dock["labware"]:
            raise DriverValidationError("DRV_OT_MOTION_LOCATION", "Incompatible module labware")
        self.module_access.validate(intent)
        self.reference_docks.validate(intent)
        if self.backend.pipette.has_tip:
            raise DriverValidationError("DRV_OT_MOTION_TIP_ATTACHED", "Remove attached tips before moving labware")

    def prepare(self, intent):
        self.validate(intent)
        prepared = PreparedLabwareMove(str(uuid4()), intent, self.config.fingerprint(), self.state["revision"])
        self.preparations[prepared.preparation_id] = prepared
        return prepared

    def describe(self, prepared):
        return {"operation_kind": "labware_move", "resource_requirements": {}, "request": asdict(prepared.intent),
                "position_revision": prepared.position_revision,
                **({"required_access": self.module_access.requirements(prepared.intent)}
                   if self.module_access.requirements(prepared.intent) else {})}

    def fault_context(self, intent):
        modules = {location.removeprefix("module:") for location in (intent.source, intent.destination)
                   if location.startswith("module:")}
        context = {"planned_location": intent.destination}
        if len(modules) == 1:
            context["module"] = modules.pop()
        return context

    def execute(self, prepared):
        if not isinstance(prepared, PreparedLabwareMove) or self.preparations.pop(prepared.preparation_id, None) is not prepared:
            raise DriverValidationError("DRV_OT_MOTION_PREPARATION", "Foreign, modified or consumed preparation")
        if prepared.configuration_revision != self.config.fingerprint() or prepared.position_revision != self.state["revision"]:
            raise DriverValidationError("DRV_OT_MOTION_STALE", "Position revision changed")
        self.validate(prepared.intent)
        intent = prepared.intent
        self.mark_uncertain()  # Persist uncertainty before the first possible movement.
        payload = {**self.describe(prepared), "phases": {"move": "not_started"}, "position_measured": False}
        outcome = "unknown"
        try:
            self.module_access.execute(intent, payload)
            self.reference_docks.begin(intent, payload)
            destination = self.backend.motion_destination(intent.destination)
            payload["phases"]["move"] = "started"
            command_start = len(self.backend.protocol.commands()) if "reference_dock_actions" in payload else None
            self.backend.protocol.move_labware(self.backend.labware[intent.labware], destination, use_gripper=True)
            if self.current_location(intent.labware) != intent.destination:
                raise RuntimeError("MOTION_DESTINATION_UNCONFIRMED")
            payload["phases"]["move"] = "returned"
            if command_start is not None:
                payload["sdk_movement"] = {
                    "backend": "official-opentrons",
                    "commands": self.backend.protocol.commands()[command_start:],
                }
            self.reference_docks.finish(intent, payload)
            updated = deepcopy(self.state)
            updated["positions"][intent.labware] = intent.destination
            updated["revision"] += 1
            updated["uncertain"] = False
            self.repository.save(updated)
            self.state = updated
            outcome = "completed"
        except Exception as error:
            self.backend.poisoned = True
            payload["error"] = f"{type(error).__name__}: {error}"
            payload["retry_safe"] = False
            fault = OpentronsFaultMappingInteractor(schema_version=2).completion_fault(
                error, intent.step_id, "labware_move", self.fault_context(intent),
            )
            payload["faults"] = [fault]
            self.mark_uncertain(fault)
        receipt = TransferFeedback(outcome, "simulation", intent.step_id, self.config.fingerprint(), {}, payload)
        self.receipts.append(receipt)
        return receipt

    def close(self):
        self.repository.close()
