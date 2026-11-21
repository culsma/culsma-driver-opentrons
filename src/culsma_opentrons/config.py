"""Validate the explicit physical setup independently of language names."""
from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any

from .centrifuge.configuration import ReferenceCentrifugeConfiguration
from .models import WellAddress
from .policy import TransferPolicy


@dataclass(frozen=True)
class DeckConfiguration:
    robot_type: str
    api_level: str
    pipette: str
    mount: str
    tiprack: dict[str, str]
    labware: dict[str, dict[str, str]]
    bindings: dict[str, WellAddress]

    transfer_policy: TransferPolicy = field(default_factory=TransferPolicy)
    schema_version: int = 1
    modules: dict = field(default_factory=dict)
    labware_moves: dict = field(default_factory=dict)
    reference_devices: dict[str, ReferenceCentrifugeConfiguration] = field(default_factory=dict)
    reference_environments: dict = field(default_factory=dict)

    def snapshot(self) -> dict:
        value=asdict(self)
        if value["transfer_policy"].get("precision_method") is None:
            value["transfer_policy"].pop("precision_method", None)
        if not self.modules:value.pop("modules")
        if not self.labware_moves:value.pop("labware_moves")
        if not self.reference_environments:value.pop("reference_environments")
        if self.reference_devices:
            value["reference_devices"] = {name: dock.snapshot() for name, dock in self.reference_devices.items()}
        else:
            value.pop("reference_devices")
        return value

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.snapshot(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DeckConfiguration":
        raw = dict(raw) if isinstance(raw, dict) else raw
        if isinstance(raw, dict) and 'execution_selection' in raw:
            selection = raw.pop('execution_selection')
            if selection != {'mode': 'simulation', 'simulator': 'opentrons'}:
                raise ValueError('EXECUTION_IMPLEMENTATION_UNSUPPORTED')
        required = {"robot_type", "api_level", "pipette", "mount", "tiprack", "labware", "bindings"}
        if not isinstance(raw, dict) or not required <= set(raw) or set(raw) - required - {"transfer_policy", "schema_version", "modules", "labware_moves", "reference_devices", "reference_environments"}:
            raise ValueError(f"Deck keys must be {sorted(required)}")
        schema = raw.get("schema_version", 1)
        if type(schema) is not int or schema not in {1, 2} or (schema == 1 and "reference_devices" in raw):
            raise ValueError("Reference devices require schema_version 2")
        if raw["robot_type"] not in {"OT-2", "Flex"} or raw["api_level"] != "2.20":
            raise ValueError("Only OT-2 or Flex / API 2.20 is supported")
        racks = {"p300_single_gen2": "opentrons_96_tiprack_300ul", "p20_single_gen2": "opentrons_96_tiprack_20ul",
                 "p300_multi_gen2": "opentrons_96_tiprack_300ul", "p20_multi_gen2": "opentrons_96_tiprack_20ul"}
        if raw["robot_type"] == "Flex":
            racks = {"flex_1channel_1000": "opentrons_flex_96_tiprack_1000ul", "flex_1channel_50": "opentrons_flex_96_tiprack_50ul"}
        if raw["pipette"] not in racks:
            raise ValueError("Unsupported pipette")
        policy = TransferPolicy.from_dict(raw.get("transfer_policy", {}))
        if raw["mount"] not in {"left", "right"}:
            raise ValueError("mount must be left or right")
        tiprack = validate_labware_spec(raw["tiprack"], raw["robot_type"])
        if tiprack["load_name"] != racks[raw["pipette"]]:
            raise ValueError("Pipette and tiprack must be a supported matching pair")
        if not isinstance(raw["labware"], dict) or not raw["labware"]:
            raise ValueError("labware must be a non-empty object")
        from .modules.interactors import ModuleConfigurationInteractor
        module_configuration=ModuleConfigurationInteractor()
        modules=module_configuration.parse(raw.get("modules",{}), raw["robot_type"])
        labware = {name: (module_configuration.labware(spec,modules) if isinstance(spec,dict) and 'module' in spec else validate_labware_spec(spec, raw['robot_type'])) for name,spec in raw['labware'].items()}
        if raw["robot_type"] == "Flex" and (tiprack["slot"] == "A3" or any(spec.get("slot") == "A3" for spec in (*labware.values(), *modules.values()))):
            raise ValueError("Flex profile reserves A3 for trash")
        if any(not isinstance(name, str) or not name or name.startswith("@") for name in labware):
            raise ValueError("Labware requires nonempty distinct public identifiers")
        module_configuration.validate_placements(tiprack,labware,modules,raw["robot_type"])
        if any(m["model"] in {module_configuration.heater_shaker, module_configuration.magnetic, module_configuration.thermocycler} for m in modules.values()) and "multi" in raw["pipette"]:
            raise ValueError("These module profiles currently require a single-channel pipette")
        if not isinstance(raw["bindings"], dict) or not raw["bindings"]:
            raise ValueError("bindings must map allocation step IDs to wells")
        bindings = {}
        for step_id, address in raw["bindings"].items():
            if not isinstance(address, dict) or set(address) != {"labware", "well"}:
                raise ValueError("Each binding must contain labware and well")
            if address["labware"] not in labware or not isinstance(address["well"], str):
                raise ValueError("Binding refers to unknown labware or invalid well")
            bindings[step_id] = WellAddress(**address)
        if len(set(bindings.values())) != len(bindings):
            raise ValueError("Two allocations cannot occupy the same physical well")
        from .motion.interactors import MotionConfigurationInteractor
        motion = MotionConfigurationInteractor()
        reference_devices = ReferenceCentrifugeConfiguration.parse_all(
            raw.get("reference_devices", {}), raw["robot_type"], labware, modules,
            tiprack, motion.slots, motion.plate_profiles,
        )
        moves = motion.parse(raw.get("labware_moves", {}), raw["robot_type"], labware, modules, reference_devices)
        if reference_devices and not moves:
            raise ValueError("Reference centrifuges require configured labware routes")
        if any(m["model"] == module_configuration.magnetic_block for m in modules.values()) and not moves:
            raise ValueError("Magnetic Block requires OS-managed docking and removal routes")
        from .environment import EnvironmentProfile
        environment_specs = raw.get('reference_environments', {})
        if not isinstance(environment_specs, dict):
            raise ValueError('reference_environments must be a configuration object')
        environments = {name: EnvironmentProfile.from_dict(value)
                        for name, value in environment_specs.items()}
        allowed_locations = motion.destinations(motion.docks(modules, labware, reference_devices), reference_devices)
        if any(not isinstance(name, str) or not name or not set(p.locations) <= allowed_locations for name, p in environments.items()):
            raise ValueError('Invalid reference environment location')
        return cls(raw["robot_type"], raw["api_level"], raw["pipette"], raw["mount"], tiprack, labware, bindings, policy, schema_version=schema, modules=modules, labware_moves=moves, reference_devices=reference_devices, reference_environments=environments)


def validate_labware_spec(spec: Any, robot_type="OT-2") -> dict[str, str]:
    if not isinstance(spec, dict) or set(spec) != {"load_name", "slot"}:
        raise ValueError("Labware requires exactly load_name and slot")
    if not isinstance(spec["load_name"], str) or not spec["load_name"]:
        raise ValueError("Labware load_name must be non-empty")
    slots = {f"{r}{c}" for r in "ABCD" for c in range(1,4)} if robot_type == "Flex" else {str(n) for n in range(1,12)}
    if spec["slot"] not in slots:
        raise ValueError("Unsupported deck slot for robot profile")
    return dict(spec)
