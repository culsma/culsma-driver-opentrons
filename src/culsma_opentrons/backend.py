"""Execute commands through Opentrons' official virtual ProtocolContext."""

from dataclasses import replace
from importlib.metadata import version
from opentrons.simulate import get_protocol_api

from .config import DeckConfiguration
from .models import (
    TransferActionObservation,
    TransferFailure,
    TransferOperation,
    TransferReceipt,
    TransferRequest,
    DriverValidationError,
    WellAddress,
)


class OfficialSimulatorBackend:
    def native_fault_identity(self, error):
        from .faults import OpentronsFaultMappingInteractor
        return OpentronsFaultMappingInteractor().exception_identity(error)

    def __init__(self, config: DeckConfiguration, restored_locations=None):
        self.config = config
        restored_locations = restored_locations or {}
        self.action_results: list[TransferActionObservation] = []
        self.protocol = get_protocol_api(config.api_level, robot_type=config.robot_type)
        try:
            if not self.protocol.is_simulating():
                raise RuntimeError("A virtual protocol context is required")
            self.modules={name:self.protocol.load_module(spec['model'],spec['slot']) for name,spec in config.modules.items()}
            self.module_adapters = {name: self.modules[name].load_adapter(spec["adapter"])
                                    for name, spec in config.modules.items() if "adapter" in spec}
            self.labware = self.load_labware(config, restored_locations)
            tiprack = self.protocol.load_labware(
                config.tiprack["load_name"], config.tiprack["slot"]
            )
            if config.robot_type == "Flex":
                self.protocol.load_trash_bin("A3")
            self.pipette = self.protocol.load_instrument(
                config.pipette, config.mount, tip_racks=[tiprack]
            )
            self.default_flow_rates = (self.pipette.flow_rate.aspirate, self.pipette.flow_rate.dispense)
            for address in config.bindings.values():
                if address.well not in self.labware[address.labware].wells_by_name():
                    raise DriverValidationError(
                        "DRV_OT_WELL_UNKNOWN",
                        f"Configured well {address.labware}:{address.well} does not exist",
                    )
                self.well(address)
        except Exception:
            self.close()
            raise
        self.receipts: list[TransferReceipt] = []
        self.poisoned = False
        self.module_action_results = []
        from .modules.interactors import TransferModuleAccessInteractor, TemperatureModuleAccessInteractor
        self.module_access = TransferModuleAccessInteractor(self)
        self.temperature_access = TemperatureModuleAccessInteractor(self)
        from .modules.magnetic_interactors import MagneticTransferAccessInteractor
        self.magnetic_access = MagneticTransferAccessInteractor(self)
        from .modules.thermocycler_interactors import ThermocyclerAccessInteractor
        self.thermocycler_access = ThermocyclerAccessInteractor(self)
        self.magnetic = None
        self.restored_tip_count = 0

    def load_labware(self, config, restored_locations):
        labware = {}
        for name, spec in config.labware.items():
            if config.robot_type == "Flex":
                location = restored_locations.get(name, f"module:{spec['module']}" if "module" in spec else spec["slot"])
                destination = self.motion_destination(location)
                labware[name] = (self.protocol.load_labware(spec["load_name"], destination)
                                 if isinstance(destination, str) else destination.load_labware(spec["load_name"]))
            elif "adapter" in spec:
                labware[name] = self.modules[spec["module"]].load_adapter(spec["adapter"]).load_labware(spec["load_name"])
            elif "module" in spec:
                labware[name] = self.modules[spec["module"]].load_labware(spec["load_name"])
            else:
                labware[name] = self.protocol.load_labware(spec["load_name"], spec["slot"])
        return labware

    def module_for_labware(self, labware):
        if self.config.robot_type != "Flex":
            return self.config.labware[labware].get("module")
        parent = self.labware[labware].parent
        return next((name for name, module in self.modules.items()
                     if parent is self.module_adapters.get(name, module)), None)

    def motion_destination(self, location):
        if not location.startswith("module:"):
            return location
        module = location.split(":", 1)[1]
        if module in self.config.reference_devices:
            return self.config.reference_devices[module].slot
        return self.module_adapters.get(module, self.modules[module])

    def well(self, address: WellAddress):
        return self.labware[address.labware].wells_by_name()[address.well]

    def run_prepared(self, operation: TransferOperation) -> TransferReceipt:
        if self.poisoned:
            return self.build_receipt(
                operation,
                len(self.protocol.commands()),
                "rejected",
                TransferFailure("DRV_OT_SESSION_FAILED", "Simulator session failed"),
            )
        start = len(self.protocol.commands())
        self.action_results = [
            TransferActionObservation(
                f"{command.stroke_id}/{name}", command.stroke_id, name, "not_started"
            )
            for command in operation.commands
            for name in ("pick_up_tip", "aspirate", "dispense", "drop_tip")
        ]
        action_index = 0
        self.module_action_results = []
        try:
            self.module_access.execute(operation)
            self.pipette.flow_rate.aspirate = (operation.policy.aspirate_flow_rate_uL_s
                if operation.policy.aspirate_flow_rate_uL_s is not None else self.default_flow_rates[0])
            self.pipette.flow_rate.dispense = (operation.policy.dispense_flow_rate_uL_s
                if operation.policy.dispense_flow_rate_uL_s is not None else self.default_flow_rates[1])
            for command in operation.commands:
                calls = (
                    (self.pipette.pick_up_tip, ()),
                    (
                        self.pipette.aspirate,
                        (command.volume_uL, self.magnetic_access.aspiration_location(operation, command)),
                    ),
                    (
                        self.pipette.dispense,
                        (command.volume_uL, self.well(command.target_address)),
                    ),
                    (self.pipette.drop_tip, ()),
                )
                for function, args in calls:
                    self.action_results[action_index] = replace(
                        self.action_results[action_index], status="completion_unknown"
                    )
                    function(*args)
                    self.action_results[action_index] = replace(
                        self.action_results[action_index], status="returned"
                    )
                    action_index += 1
        except Exception as error:
            self.poisoned = True
            return self.build_receipt(
                operation,
                start,
                "failed",
                TransferFailure(
                    "DRV_OT_SIMULATION_ERROR",
                    str(error) or type(error).__name__,
                    self.native_fault_identity(error),
                ),
            )
        receipt = self.build_receipt(operation, start, "simulated")
        self.receipts.append(receipt)
        return receipt

    def check_ready(self) -> None:
        if self.poisoned:
            raise DriverValidationError(
                "DRV_OT_SESSION_FAILED",
                "Simulator session failed; create a new session",
            )

    def reject_transfer(
        self, request: TransferRequest, failure: TransferFailure
    ) -> TransferReceipt:
        return replace(
            self.build_receipt(
                None,
                len(self.protocol.commands()),
                "rejected",
                failure,
                request.step_id,
            ),
            span=request.span,
        )

    def tip_capacity(self) -> int:
        return sum(len(rack.wells()) for rack in self.pipette.tip_racks)

    def available_tips(self) -> int:
        actual = sum(
            well.has_tip for rack in self.pipette.tip_racks for well in rack.wells()
        )
        return max(0, actual - self.restored_tip_count)

    def restore_consumables(self, consumption) -> None:
        if not isinstance(consumption, dict) or set(consumption) - {"tips"}:
            raise DriverValidationError(
                "DRV_OT_CONSUMPTION_INVALID", "Only known tip consumption is supported"
            )
        consumed = consumption.get("tips", 0)
        if type(consumed) is not int or not 0 <= consumed <= self.tip_capacity():
            raise DriverValidationError(
                "DRV_OT_CONSUMPTION_INVALID",
                "Tip consumption must fit the configured rack",
            )
        if consumed % self.pipette.channels:
            raise DriverValidationError("DRV_OT_CONSUMPTION_INVALID", "Restore must use complete nozzle columns")
        if consumed == self.tip_capacity() - self.available_tips():
            return
        if self.protocol.commands():
            raise DriverValidationError(
                "DRV_OT_CONSUMPTION_STALE", "Restore requires a fresh simulator session"
            )
        self.restored_tip_count = consumed
        tips = [well for rack in self.pipette.tip_racks for well in rack.wells()]
        self.pipette.starting_tip = tips[consumed] if consumed < len(tips) else None

    def build_receipt(
        self,
        operation: TransferOperation | None,
        start: int,
        phase: str,
        failure: TransferFailure | None = None,
        step_id: str = "",
    ) -> TransferReceipt:
        return TransferReceipt(
            operation.step_id if operation else step_id,
            phase,
            operation,
            tuple(self.action_results) if phase != "rejected" else (),
            tuple(self.protocol.commands()[start:]),
            self.config.fingerprint(),
            self.config.snapshot(),
            {
                "culsma": version("culsma"),
                "opentrons": version("opentrons"),
                "api_level": self.config.api_level,
            },
            {
                "channels": int(self.pipette.channels),
                "min_volume_uL": self.pipette.min_volume,
                "max_volume_uL": self.pipette.max_volume,
                "aspirate_flow_rate_uL_s": self.pipette.flow_rate.aspirate,
                "dispense_flow_rate_uL_s": self.pipette.flow_rate.dispense,
            },
            failure,
            operation.span if operation else None,
            module_action_results=tuple(self.module_action_results) if phase != "rejected" else (),
        )

    def close(self) -> None:
        self.protocol.cleanup()
