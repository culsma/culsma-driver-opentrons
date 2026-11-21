from dataclasses import asdict
from uuid import uuid4
from ..models import DriverValidationError, TransferFeedback
from .models import TemperatureHoldIntent, PreparedTemperatureHold, OrbitalShakeIntent, PreparedOrbitalShake


class ModuleConfigurationInteractor:
    heater_shaker = "heaterShakerModuleV1"
    temperature = "temperature module gen2"
    magnetic = "magnetic module gen2"
    magnetic_block = "magneticBlockV1"
    thermocycler = "thermocyclerModuleV2"
    module_drivers = {
        temperature: ("temperature", ("thermal_hold",)),
        thermocycler: ("temperature", ("thermal_hold",)),
        heater_shaker: ("shaker", ("orbital_shake", "thermal_hold")),
        magnetic: ("magnet", ("magnetic_separation",)),
        magnetic_block: ("magnet", ("magnetic_separation",)),
    }

    def parse(self, raw, robot_type="OT-2"):
        if not isinstance(raw, dict):
            raise ValueError("modules must be an object")
        result = {}
        for name, spec in raw.items():
            if (not isinstance(name, str) or not name or not isinstance(spec, dict)
                    or not self.valid_fields(spec, robot_type)
                    or spec["model"] not in self.module_drivers
                    or not isinstance(spec["slot"], str)
                    or spec["slot"] not in ({f"{r}{c}" for r in "ABCD" for c in range(1, 4)}
                        if robot_type == "Flex" else {str(n) for n in range(1, 12)})):
                raise ValueError("Named supported module placements are required")
            if robot_type == "Flex":
                columns = ("1", "3") if spec["model"] in {self.heater_shaker, self.magnetic_block} else ("1",)
                if (spec["model"] not in {self.temperature, self.heater_shaker, self.magnetic_block} or name.startswith("@")
                        or ":" in name or not spec["slot"].endswith(columns)):
                    raise ValueError("Unsupported named Flex module or placement")
                if spec["model"] == self.heater_shaker and spec["adapter"] != "opentrons_96_flat_bottom_adapter":
                    raise ValueError("Flex Heater-Shaker requires the standalone flat-bottom adapter")
            if spec["model"] == self.magnetic_block:
                from math import isfinite
                height = spec["aspirate_height_mm"]
                slots = {f"{r}{c}" for r in "ABCD" for c in range(1,4)} - {"A3", spec["slot"]}
                if (robot_type != "Flex" or type(height) not in {int, float} or not isfinite(height)
                        or not 0 < height < 40 or spec["release_destination"] not in slots):
                    raise ValueError("Magnetic Block requires Flex, supported aspiration height and release deck slot")
            if spec["model"] == self.thermocycler:
                self.validate_lid_settings(spec)
            if spec["model"] == self.thermocycler and spec["slot"] != "7":
                raise ValueError("OT-2 Thermocycler requires slots 7,8,10,11 with anchor 7")
            if robot_type != "Flex" and spec["model"] == self.heater_shaker and spec["slot"] not in {"1", "3", "4", "6", "7", "10"}:
                raise ValueError("Unsupported OT-2 Heater-Shaker slot")
            if spec["model"] == self.magnetic:
                from math import isfinite
                for field, maximum in (("engage_height_mm", 25), ("aspirate_height_mm", 14.78)):
                    value = spec[field]
                    if type(value) not in {int, float} or not isfinite(value) or not 0 < value < maximum:
                        raise ValueError("Explicit supported magnetic heights are required")
            result[name] = dict(spec)
        return result

    def valid_fields(self, spec, robot_type):
        required = {"model", "slot"}
        model = spec.get("model")
        if model == self.magnetic:
            required |= {"engage_height_mm", "aspirate_height_mm"}
        elif model == self.magnetic_block:
            required |= {"aspirate_height_mm", "release_destination"}
        elif model == self.heater_shaker and robot_type == "Flex":
            required.add("adapter")
        optional = {"lid_temperature_celsius", "keep_lid_closed"} if model == self.thermocycler else set()
        return required <= set(spec) and not set(spec) - required - optional

    def validate_lid_settings(self, spec):
        from math import isfinite
        if "lid_temperature_celsius" in spec:
            value = spec["lid_temperature_celsius"]
            if type(value) not in {int, float} or not isfinite(value) or not 37 <= value <= 110 or int(value) != value:
                raise ValueError("Thermocycler heated lid requires integral 37-110C")
        if type(spec.get("keep_lid_closed", False)) is not bool:
            raise ValueError("keep_lid_closed must be boolean")

    def labware(self, spec, modules):
        if (not isinstance(spec, dict) or not isinstance(spec.get("module"), str)
                or spec["module"] not in modules or not isinstance(spec.get("load_name"), str)):
            raise ValueError("Module labware requires load_name and a configured module")
        if modules[spec["module"]]["model"] == self.heater_shaker:
            if (set(spec) != {"load_name", "module", "adapter"}
                    or spec["adapter"] != "opentrons_96_flat_bottom_adapter"
                    or spec["load_name"] != "nest_96_wellplate_200ul_flat"):
                raise ValueError("Heater-Shaker currently requires the flat-bottom adapter and NEST 200uL plate")
            if modules[spec["module"]].get("adapter", spec["adapter"]) != spec["adapter"]:
                raise ValueError("Labware adapter differs from the configured module adapter")
        elif modules[spec["module"]]["model"] == self.magnetic_block:
            if set(spec) != {"load_name", "module"} or spec["load_name"] != "nest_96_wellplate_2ml_deep":
                raise ValueError("Magnetic Block profile requires the NEST 2mL deep plate")
        elif modules[spec["module"]]["model"] == self.magnetic:
            if set(spec) != {"load_name", "module"} or spec["load_name"] != "nest_96_wellplate_100ul_pcr_full_skirt":
                raise ValueError("Magnetic profile requires the NEST 100uL PCR plate")
        elif modules[spec["module"]]["model"] == self.thermocycler:
            if set(spec) != {"load_name", "module"} or spec["load_name"] not in {"nest_96_wellplate_100ul_pcr_full_skirt", "opentrons_96_wellplate_200ul_pcr_full_skirt"}:
                raise ValueError("Thermocycler requires a supported 100uL or 200uL full-skirt PCR plate")
        elif set(spec) != {"load_name", "module"} or not spec["load_name"]:
            raise ValueError("Temperature Module labware requires load_name and module")
        return dict(spec)

    def validate_placements(self, tiprack, labware, modules, robot_type="OT-2"):
        slots = [tiprack["slot"], *(s["slot"] for s in labware.values() if "slot" in s)]
        for spec in modules.values():
            slots.extend(("7", "8", "10", "11") if spec["model"] == self.thermocycler else (spec["slot"],))
        if len(slots) != len(set(slots)):
            raise ValueError("Deck slots must be unique")
        for spec in modules.values():
            if spec["model"] == self.magnetic_block and spec["release_destination"] in slots and not any(
                    item.get("slot") == spec["release_destination"] and item["load_name"] == "nest_96_wellplate_2ml_deep" for item in labware.values()):
                raise ValueError("Magnetic Block release destination must initially be empty")
        owners = [s["module"] for s in labware.values() if "module" in s]
        if len(owners) != len(set(owners)):
            raise ValueError("A module can hold only one configured labware")
        for spec in modules.values():
            if spec["model"] != self.heater_shaker or robot_type == "Flex":
                continue
            slot = int(spec["slot"])
            row, col = divmod(slot - 1, 3)
            adjacent = {str(n) for n in range(1, 12)
                        if abs((n - 1) // 3 - row) + abs((n - 1) % 3 - col) == 1}
            if adjacent & set(slots):
                raise ValueError("This Heater-Shaker profile requires empty adjacent deck slots")

    def capabilities(self, modules, labware, docks=None):
        result = {}
        for name, spec in modules.items():
            _, operations = self.module_drivers[spec["model"]]
            for operation in operations:
                result.setdefault(operation, {})[name] = {
                    "labware": [key for key, location in labware.items() if location.get("module") == name],
                }
        for dock in (docks or {}).values():
            module = dock["module_id"]
            _, operations = self.module_drivers[modules[module]["model"]]
            for operation in operations:
                result[operation][module]["labware"] = list(dock["labware"])
        for name, spec in modules.items():
            if spec["model"] == self.magnetic_block:
                result["magnetic_separation"][name]["release_destination"] = spec["release_destination"]
        for name, spec in modules.items():
            if spec["model"] == self.thermocycler:
                result["thermal_hold"][name]["temperature_range_celsius"] = [4, 99]
            elif spec["model"] == self.heater_shaker:
                result["orbital_shake"][name]["default_motion"] = "orbital"
                result["orbital_shake"][name]["environment_conditions"] = {
                    "thermal": {"unit": "C", "minimum": 37, "maximum": 95}
                }
        return result


class ModuleScopeInteractor:
    def __init__(self, backend, bindings):
        self.backend, self.bindings = backend, bindings

    def resolve(self, intent, bindings, model):
        addresses = {binding.reference: binding.address for binding in bindings}
        try:
            locations = [addresses[target] for target in intent.targets]
        except KeyError as error:
            raise DriverValidationError("DRV_OT_MODULE_BINDING", "Missing target binding") from error
        if len({p.labware for p in locations}) != 1:
            raise DriverValidationError("DRV_OT_MODULE_SCOPE", "One module labware per request")
        labware = locations[0].labware
        module_id = self.backend.module_for_labware(labware)
        spec = self.bindings.config.modules.get(module_id, {})
        models = (model,) if isinstance(model, str) else model
        if module_id not in self.backend.modules or spec.get("model") not in models:
            raise DriverValidationError("DRV_OT_MODULE_BINDING", "Target is not on a compatible configured module")
        if any(b.address.labware == labware and b.reference not in intent.targets for b in bindings):
            raise DriverValidationError("DRV_OT_MODULE_SCOPE", "Every declared container on affected labware must be included")
        return module_id, labware


class TemperatureModuleAccessInteractor:
    """Command ownership supplements SDK observations, which can stay idle in simulation."""
    def __init__(self, backend):
        self.backend = backend
        self.active = {}

    def require_idle(self, module_id):
        module = self.backend.modules[module_id]
        if module_id in self.active or module.status != "idle" or module.target is not None:
            raise DriverValidationError("DRV_OT_MODULE_BUSY", "Temperature module must be inactive")

    def validate_transfer(self, operation):
        labware = {address.labware for command in operation.commands
                   for address in (command.source_address, command.target_address)}
        for name in labware:
            module = self.backend.module_for_labware(name)
            if module is not None and self.backend.config.modules[module]["model"] == ModuleConfigurationInteractor.temperature:
                self.require_idle(module)

    def begin_hold(self, module_id, step_id):
        self.require_idle(module_id)
        self.active[module_id] = step_id

    def finish_hold(self, module_id, step_id):
        module = self.backend.modules[module_id]
        if self.active.get(module_id) != step_id or module.status != "idle" or module.target is not None:
            raise DriverValidationError("DRV_OT_MODULE_BUSY", "Temperature module release is unconfirmed")
        self.active.pop(module_id)


class TemperatureHoldInteractor:
    def __init__(self,backend,bindings):
        self.backend,self.bindings=backend,bindings
        self.preparations={}
        self.receipts=[]

    def prepare(self,intent,bindings):
        self.backend.check_ready()
        if not isinstance(intent,TemperatureHoldIntent):
            raise DriverValidationError('DRV_OT_MODULE_REQUEST','Typed temperature hold required')
        t=intent.temperature_celsius
        if not 4<=t<=95 or int(t)!=t:
            raise DriverValidationError('DRV_OT_MODULE_TEMPERATURE','Module requires exact integral Celsius setpoint from 4 to 95')
        module_id, labware = ModuleScopeInteractor(self.backend, self.bindings).resolve(
            intent, bindings, ModuleConfigurationInteractor.temperature)
        self.require_idle(module_id)
        prepared=PreparedTemperatureHold(str(uuid4()),intent,module_id,labware,self.bindings.config.fingerprint())
        self.preparations[prepared.preparation_id]=prepared
        return prepared

    def require_idle(self, module_id):
        self.backend.temperature_access.require_idle(module_id)

    def describe(self,prepared):
        return {'operation_kind':'thermal_hold','resource_requirements':{},
                'module_id':prepared.module_id,'affected_labware':prepared.labware,
                'request':asdict(prepared.intent),'postcondition':'module_idle'}

    def execute(self,prepared):
        self.backend.check_ready()
        if (not isinstance(prepared,PreparedTemperatureHold)
                or self.preparations.get(prepared.preparation_id) is not prepared):
            raise DriverValidationError('DRV_OT_MODULE_PREPARATION','Foreign, modified or consumed preparation')
        if self.bindings.config.fingerprint()!=prepared.configuration_revision:
            raise DriverValidationError('DRV_OT_MODULE_STALE','Configuration changed after preparation')
        if self.backend.module_for_labware(prepared.labware) != prepared.module_id:
            raise DriverValidationError("DRV_OT_MODULE_BINDING", "Plate moved after preparation")
        self.require_idle(prepared.module_id)
        self.preparations.pop(prepared.preparation_id)
        module=self.backend.modules[prepared.module_id]
        start=len(self.backend.protocol.commands());phases={};error=None
        try:
            self.backend.temperature_access.begin_hold(prepared.module_id, prepared.intent.step_id)
            phases['set_temperature']='started'
            module.set_temperature(prepared.intent.temperature_celsius)
            phases['set_temperature']='returned'
            phases['hold']='started'
            self.backend.protocol.delay(seconds=prepared.intent.duration_seconds)
            phases['hold']='returned'
            phases['deactivate']='started'
            module.deactivate()
            phases['deactivate']='returned'
            self.backend.temperature_access.finish_hold(prepared.module_id, prepared.intent.step_id)
        except Exception as failure:
            error=f'{type(failure).__name__}: {failure}'
            self.backend.poisoned=True
        payload={**self.describe(prepared),'commands':self.backend.protocol.commands()[start:],
                 'phases':phases,'evidence_scope':'virtual_module_command_observation',
                 'sample_temperature_measured':False,'duration_clock':'simulated',
                 'readiness_evidence':'blocking_set_temperature_returned' if phases.get('set_temperature')=='returned' else 'unknown',
                 'module_end_status':module.status,'failure':error}
        result=TransferFeedback('unknown' if error else 'completed','simulation',prepared.intent.step_id,
                                prepared.configuration_revision,{},payload)
        self.receipts.append(result)
        return result


class OrbitalShakeInteractor:
    def __init__(self, backend, bindings):
        self.backend, self.bindings = backend, bindings
        self.preparations = {}
        self.receipts = []

    def prepare(self, intent, bindings):
        self.backend.check_ready()
        if not isinstance(intent, OrbitalShakeIntent):
            raise DriverValidationError("DRV_OT_MODULE_REQUEST", "Typed orbital shake required")
        if not 200 <= intent.rate_rpm <= 3000:
            raise DriverValidationError("DRV_OT_MODULE_RATE", "Heater-Shaker requires integral 200-3000 rpm")
        module_id, labware = ModuleScopeInteractor(self.backend, self.bindings).resolve(
            intent, bindings, ModuleConfigurationInteractor.heater_shaker)
        if self.backend.pipette.channels != 1:
            raise DriverValidationError("DRV_OT_MODULE_PIPETTE", "This profile requires a single-channel pipette")
        self.backend.module_access.require_idle(module_id)
        if self.backend.pipette.has_tip:
            raise DriverValidationError("DRV_OT_TIP_ATTACHED", "Latching requires no attached tip")
        prepared = PreparedOrbitalShake(str(uuid4()), intent, module_id, labware, self.bindings.config.fingerprint())
        self.preparations[prepared.preparation_id] = prepared
        return prepared

    def describe(self, prepared):
        request = asdict(prepared.intent)
        heated = request["temperature_celsius"] is not None
        if not heated:
            request.pop("temperature_celsius")
        return {"operation_kind": "orbital_shake", "resource_requirements": {},
                "module_id": prepared.module_id, "affected_labware": prepared.labware,
                "request": request,
                "postcondition": "shaker_and_heater_deactivated" if heated else "shaker_deactivated"}

    def execute(self, prepared):
        self.backend.check_ready()
        if (not isinstance(prepared, PreparedOrbitalShake)
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
            temperature = prepared.intent.temperature_celsius
            if temperature is not None:
                phases["set_temperature"] = "started"
                module.set_and_wait_for_temperature(temperature)
                phases["set_temperature"] = "returned"
            phases["set_speed"] = "started"
            module.set_and_wait_for_shake_speed(prepared.intent.rate_rpm)
            phases["set_speed"] = "returned"
            phases["hold"] = "started"
            self.backend.protocol.delay(seconds=prepared.intent.duration_seconds)
            phases["hold"] = "returned"
            phases["deactivate"] = "started"
            module.deactivate_shaker()
            phases["deactivate"] = "returned"
            if temperature is not None:
                phases["deactivate_heater"] = "started"
                module.deactivate_heater()
                phases["deactivate_heater"] = "returned"
            self.backend.module_access.finish_processing(prepared.module_id, prepared.intent.step_id)
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            self.backend.poisoned = True
        payload = {**self.describe(prepared), "commands": self.backend.protocol.commands()[start:],
                   "phases": phases, "evidence_scope": "virtual_module_command_observation",
                   "speed_measured": False, "mixing_result_measured": False, "duration_clock": "simulated",
                   "latch_observed_status": module.labware_latch_status, "speed_observed_status": module.speed_status,
                   "readiness_evidence": "blocking_set_speed_returned" if phases.get("set_speed") == "returned" else "unknown",
                   "failure": failure}
        if prepared.intent.temperature_celsius is not None:
            payload["sample_temperature_measured"] = False
            payload["temperature_observed_status"] = module.temperature_status
        result = TransferFeedback("unknown" if failure else "completed", "simulation", prepared.intent.step_id,
                                  prepared.configuration_revision, {}, payload)
        self.receipts.append(result)
        return result


class TransferModuleAccessInteractor:
    """Shared Heater-Shaker ownership and authorized Transfer prerequisites."""
    def __init__(self, backend):
        self.backend = backend
        self.active = {}

    def modules_for(self, operation):
        labware = {address.labware for command in operation.commands
                   for address in (command.source_address, command.target_address)}
        modules = {self.backend.module_for_labware(name) for name in labware}
        return tuple(sorted(name for name in modules if name is not None
                            and self.backend.config.modules[name]["model"] == ModuleConfigurationInteractor.heater_shaker))

    def require_idle(self, module_id):
        if module_id in self.active:
            raise DriverValidationError("DRV_OT_MODULE_BUSY", "Heater-Shaker operation has not released ownership")
        self.require_observed_idle(module_id)

    def begin_processing(self, module_id, step_id):
        self.require_idle(module_id)
        self.active[module_id] = step_id

    def finish_processing(self, module_id, step_id):
        if self.active.get(module_id) != step_id:
            raise DriverValidationError("DRV_OT_MODULE_BUSY", "Heater-Shaker owner does not match")
        self.require_observed_idle(module_id)
        self.active.pop(module_id)

    def require_observed_idle(self, module_id):
        module = self.backend.modules[module_id]
        if module.speed_status != "idle" or module.target_speed not in (None, 0):
            raise DriverValidationError("DRV_OT_MODULE_BUSY", "Shaker must be idle before this operation")
        if module.temperature_status != "idle" or module.target_temperature is not None:
            raise DriverValidationError("DRV_OT_MODULE_BUSY", "Heater must be inactive before this operation")

    def require_closed(self, module_id):
        if self.backend.modules[module_id].labware_latch_status not in {"idle_closed", "idle_unknown"}:
            raise DriverValidationError("DRV_OT_LATCH_UNCONFIRMED", "Latch close is contradicted by module state")

    def validate(self, operation):
        self.backend.temperature_access.validate_transfer(operation)
        self.backend.magnetic_access.validate(operation)
        self.backend.thermocycler_access.validate(operation)
        for module_id in self.modules_for(operation):
            self.require_idle(module_id)

    def describe(self, operation):
        return ([{"module_id": name, "action": "close_labware_latch"} for name in self.modules_for(operation)]
                + self.backend.magnetic_access.describe(operation)
                + self.backend.thermocycler_access.describe(operation))

    def execute(self, operation):
        self.validate(operation)
        self.backend.magnetic_access.observe(operation)
        for item in [{"module_id": name, "action": "close_labware_latch"} for name in self.modules_for(operation)]:
            observation = {**item, "status": "completion_unknown"}
            self.backend.module_action_results.append(observation)
            module = self.backend.modules[item["module_id"]]
            module.close_labware_latch()
            self.require_closed(item["module_id"])
            observation.update(status="returned", latch_observed_status=module.labware_latch_status,
                               evidence="blocking_close_latch_returned")
        self.backend.thermocycler_access.execute(operation)
