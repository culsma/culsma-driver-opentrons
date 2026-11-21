"""One device session used by both the standalone driver and external hosts."""

from dataclasses import asdict, replace
from .operations import TransferIntent, ResolvedContainerRef
from .models import (
    ContainerHandle,
    DriverValidationError,
    SimulatorBinding,
    SimulatorSessionDescription,
    TransferAliquot,
    TransferRequest,
    WellAddress,
)
from .receipt import OpentronsReceiptNormalizer


class SimulatorSession:
    @property
    def fault_mapper(self):
        from .faults import OpentronsFaultMappingInteractor
        return OpentronsFaultMappingInteractor()

    def __init__(self, backend, bindings, driver, normalizer=None, thermal=None, motion=None):
        self.backend = backend
        self.motion = motion
        self.centrifuges = motion.reference_docks.drivers if motion is not None else {}
        self._completed_centrifuge_steps = {}
        self._supernatant_preparations = {}
        self.bindings = bindings
        self.driver = driver
        self.transfers = driver.transfers
        self.normalizer = normalizer or OpentronsReceiptNormalizer()
        self.closed = False
        from .modules.assembly import ModuleInteractorAssembler
        from .modules.interactors import OrbitalShakeInteractor
        self.thermal = thermal if thermal is not None else ModuleInteractorAssembler().thermal(backend, bindings)
        self.agitation = OrbitalShakeInteractor(backend, bindings)
        from .modules.magnetic_interactors import MagneticSeparationInteractor
        self.magnetic = backend.magnetic = MagneticSeparationInteractor(backend, bindings)
        from .interactors import MultichannelTransferInteractor, MultichannelBatchInteractor
        self.groups = MultichannelTransferInteractor(backend, bindings, self.normalizer)
        self.batches = MultichannelBatchInteractor(self.groups)

    @property
    def supports_collective_execution(self):
        return self.backend.pipette.channels == 8

    def describe_session(self):
        config = self.bindings.config
        return SimulatorSessionDescription(
            "opentrons",
            "simulation",
            config.fingerprint(),
            ("simulation:opentrons:gantry", "simulation:opentrons:pipette",
             *(f"simulation:opentrons:module:{name}" for name in sorted(config.modules.keys() | config.reference_devices.keys()))),
            {"tips": self.backend.tip_capacity()},
            dict(config.bindings),
            {
                (name, well): self.backend.well(WellAddress(name, well)).max_volume
                for name in config.labware
                for well in self.backend.labware[name].wells_by_name()
            },
            {
                name: (len(labware.rows()), len(labware.columns()))
                for name, labware in self.backend.labware.items()
                if not labware.is_tiprack
            },
            int(self.backend.pipette.channels),
            self.processing_capabilities(),
            ("operation", "batch", "wave") if self.supports_collective_execution else ("operation", "member"),
            self.motion_capabilities(),
            self.transfers.supported_requirements(),
        )

    def motion_capabilities(self):
        from .motion.interactors import MotionConfigurationInteractor
        return MotionConfigurationInteractor().capabilities(self.bindings.config)

    def prepare_labware_move(self, intent):
        self.check_ready()
        if self.motion is None:
            raise DriverValidationError("DRV_OT_MOTION_UNSUPPORTED", "No configured motion capability")
        return self.motion.prepare(intent)

    def describe_labware_move(self, prepared):
        return self.motion.describe(prepared)

    def execute_labware_move(self, prepared):
        self.check_ready()
        return self.motion.execute(prepared)

    def instrument_drivers(self):
        from .modules.interactors import ModuleConfigurationInteractor
        return {
            "pipetting": ("transfer",),
            "plate_transport": ("labware_move",),
            **dict(ModuleConfigurationInteractor.module_drivers.values()),
            **({"centrifuge": ("centrifuge",)} if self.centrifuges else {}),
        }

    def processing_capabilities(self):
        from .modules.interactors import ModuleConfigurationInteractor
        config = self.bindings.config
        docks = self.motion_capabilities().get("docks", {})
        capabilities = ModuleConfigurationInteractor().capabilities(
            config.modules, config.labware,
            {location: dock for location, dock in docks.items() if dock["module_id"] in config.modules},
        )
        if self.centrifuges:
            capabilities["centrifuge"] = {
                name: dock.capabilities() for name, dock in config.reference_devices.items()
            }
        return capabilities

    def transfer_selection_capabilities(self):
        return ("centrifuge_supernatant",) if self.centrifuges else ()

    def prepare_centrifuge(self, intent, bindings):
        from .centrifuge import CentrifugeIntent

        self.check_ready()
        if not isinstance(intent, CentrifugeIntent):
            raise DriverValidationError("DRV_OT_CENTRIFUGE_REQUEST", "A typed centrifuge request is required")
        addresses = self.bind_references(bindings)
        try:
            labwares = {addresses[ContainerHandle(target.container_id)].labware for target in intent.targets}
        except KeyError as error:
            raise DriverValidationError("DRV_OT_CENTRIFUGE_BINDING", "Missing target binding") from error
        if len(labwares) != 1 or not self.centrifuges:
            raise DriverValidationError("DRV_OT_CENTRIFUGE_LOAD", "One plate at a configured centrifuge is required")
        (labware,) = labwares
        location = self.motion.positions()[labware]
        driver = self.centrifuges.get(location.removeprefix("module:"))
        if not location.startswith("module:") or driver is None or self.motion.current_location(labware) != location:
            raise DriverValidationError("DRV_OT_CENTRIFUGE_LOAD", "Move the sample plate to its centrifuge before preparation")
        return self.motion.pin_processing(driver.prepare(intent, bindings))

    def describe_centrifuge(self, prepared):
        return self.centrifuges[prepared.module_id].describe(prepared)

    def execute_centrifuge(self, prepared):
        self.check_ready()
        self.motion.consume_processing(prepared)
        driver = self.centrifuges[prepared.module_id]
        self.motion.mark_uncertain()
        try:
            receipt = driver.execute(prepared)
        except DriverValidationError:
            self.motion.confirm_position()
            raise
        if receipt.outcome == "completed":
            self.motion.confirm_position()
            self._completed_centrifuge_steps[prepared.settings.labware] = prepared
        else:
            self.backend.poisoned = True
            self.motion.mark_uncertain(receipt.payload["faults"][0])
        return replace(
            receipt, configuration_revision=self.bindings.config.fingerprint(),
            payload={**receipt.payload, "centrifuge_profile_revision": receipt.configuration_revision},
        )

    def prepare_centrifuge_supernatant(self, intent, bindings, producer_step_id):
        self.check_ready()
        if len(intent.aliquots) != 1:
            raise DriverValidationError("DRV_OT_CENTRIFUGE_SELECTION", "A single supernatant source is required")
        source = intent.aliquots[0].source
        addresses = self.bind_references(bindings)
        address = addresses.get(ContainerHandle(source.container_id))
        producer = self._completed_centrifuge_steps.get(address.labware) if address is not None else None
        if (producer is None or producer.intent.step_id != producer_step_id
                or not any(binding.reference == source and binding.address == address for binding in producer.bindings)):
            raise DriverValidationError(
                "DRV_OT_CENTRIFUGE_SELECTION", "Supernatant source requires a confirmed centrifuge cycle and binding"
            )
        prepared = self.prepare_transfer(intent, bindings)
        self._supernatant_preparations[prepared.preparation_id] = producer_step_id
        return prepared

    def bind_references(self, bindings):
        positions = {}
        occupied = set()
        configured = set(self.bindings.config.bindings.values())
        for binding in bindings:
            if not isinstance(binding, SimulatorBinding) or not isinstance(
                binding.reference, ResolvedContainerRef
            ):
                raise DriverValidationError(
                    "DRV_OT_BINDING_INVALID", "Typed resolved bindings are required"
                )
            handle = ContainerHandle(binding.reference.container_id)
            if (
                handle in positions
                or binding.address in occupied
                or binding.address not in configured
            ):
                raise DriverValidationError(
                    "DRV_OT_BINDING_INVALID", "Duplicate or unconfigured binding"
                )
            self.backend.well(binding.address)
            positions[handle] = binding.address
            occupied.add(binding.address)
        self.bindings.addresses = positions
        return positions

    def prepare_transfer(self, intent, bindings):
        self.check_ready()
        self.bind_references(bindings)
        prepared = self.transfers.prepare_transfer(self.resolve_request(intent))
        return self.motion.pin_transfer(prepared) if self.motion is not None else prepared

    def resolve_request(self, intent):
        if not isinstance(intent, TransferIntent):
            raise DriverValidationError(
                "DRV_OT_REQUEST_INVALID", "Resolved TransferIntent is required"
            )
        if (
            intent.constraint_options
            or intent.environment_requirements
        ):
            raise DriverValidationError(
                "DRV_OT_GATE_UNSUPPORTED", "Transfer requirements are unsupported"
            )
        if any(r not in self.transfers.supported_requirements() for r in intent.requirements):
            raise DriverValidationError("DRV_OT_CONSTRAINT_METHOD", "No configured method for requested transfer constraint")
        positions = self.bindings.addresses
        try:
            target = ContainerHandle(intent.target.container_id)
            request = TransferRequest(
                intent.step_id,
                target,
                positions[target],
                tuple(
                    TransferAliquot(
                        ContainerHandle(a.source.container_id),
                        positions[ContainerHandle(a.source.container_id)],
                        a.volume_uL,
                    )
                    for a in intent.aliquots
                ),
                intent.span,
                requirements=intent.requirements,
            )
        except KeyError as error:
            raise DriverValidationError(
                "DRV_OT_BINDING_MISSING", "Missing resolved container binding"
            ) from error
        return request

    def prepare_group(self, group_id, intents, bindings):
        self.check_ready()
        self.bind_references(bindings)
        requests = tuple(self.resolve_request(intent) for intent in intents)
        if len(requests) == 96:
            return self.batches.prepare(group_id, tuple(intents), requests)
        return self.groups.prepare(group_id, intents, requests)

    def describe_group(self, prepared):
        from .models import PreparedMultichannelBatch
        if isinstance(prepared, PreparedMultichannelBatch):
            return self.batches.describe(prepared)
        return self.groups.describe(prepared)

    def wave_plans(self, prepared):
        return prepared.waves

    def execute_group_wave(self, prepared, wave):
        self.check_ready()
        return self.batches.execute_wave(prepared, wave)

    def execute_group(self, prepared):
        self.check_ready()
        return self.groups.execute(prepared)

    def describe_transfer(self, prepared):
        producer = self._supernatant_preparations.get(prepared.preparation_id)
        return {
            **({"selection": {"kind": "centrifuge_supernatant", "producer_step_id": producer}} if producer else {}),
            "transfers": [asdict(command) for command in prepared.operation.commands],
            "resource_requirements": {"tips": len(prepared.operation.commands)},
            **({"constraint_resolution": [c.to_payload() for c in prepared.operation.constraint_resolution]}
               if prepared.operation.constraint_resolution else {}),
            **({"module_preconditions": self.backend.module_access.describe(prepared.operation)}
               if self.backend.module_access.describe(prepared.operation) else {}),
        }

    def execute_transfer(self, prepared):
        self.check_ready()
        if self.motion is not None:
            self.motion.consume_transfer(prepared)
        description = self.describe_transfer(prepared)
        self._supernatant_preparations.pop(prepared.preparation_id, None)
        receipt = self.transfers.execute_transfer(prepared)
        feedback = self.normalizer.interpret(
            description,
            receipt.to_payload(),
            prepared.request.step_id,
            self.bindings.config.fingerprint(),
        )
        if "selection" in description:
            return replace(feedback, payload={**feedback.payload, "selection": description["selection"]})
        return feedback

    def check_ready(self):
        if self.closed:
            raise DriverValidationError(
                "DRV_OT_SESSION_CLOSED", "Simulator session is closed"
            )
        if self.motion is not None:
            self.motion.require_known()
        self.backend.check_ready()

    def restore_consumables(self, consumption):
        if self.motion is None:
            self.check_ready()
        elif self.closed:
            raise DriverValidationError("DRV_OT_SESSION_CLOSED", "Simulator session is closed")
        # Restoring bookkeeping does not authorize an uncertain motion or a new action.
        self.backend.restore_consumables(consumption)

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.backend.close()
            finally:
                if self.motion is not None:
                    self.motion.close()


    def prepare_temperature_hold(self,intent,bindings):
        self.check_ready()
        self.bind_references(bindings)
        prepared = self.thermal.prepare(intent,bindings)
        return self.motion.pin_processing(prepared) if self.motion is not None else prepared

    def describe_temperature_hold(self,prepared):
        return self.thermal.describe(prepared)

    def execute_temperature_hold(self,prepared):
        self.check_ready()
        if self.motion is not None:
            self.motion.consume_processing(prepared)
        return self.thermal.execute(prepared)


    def prepare_orbital_shake(self, intent, bindings):
        self.check_ready()
        self.bind_references(bindings)
        prepared = self.agitation.prepare(intent, bindings)
        return self.motion.pin_processing(prepared) if self.motion is not None else prepared

    def describe_orbital_shake(self, prepared):
        return self.agitation.describe(prepared)

    def execute_orbital_shake(self, prepared):
        self.check_ready()
        if self.motion is not None:
            self.motion.consume_processing(prepared)
        return self.agitation.execute(prepared)


    def prepare_magnetic_separation(self, intent, bindings):
        from .modules.models import MagneticReleaseIntent
        self.check_ready()
        if not isinstance(intent, MagneticReleaseIntent):
            self.bind_references(bindings)
        prepared = self.magnetic.prepare(intent, bindings)
        return self.motion.pin_processing(prepared) if self.motion is not None and not isinstance(intent, MagneticReleaseIntent) else prepared

    def describe_magnetic_operation(self, prepared):
        return self.magnetic.describe(prepared)

    def execute_magnetic_operation(self, prepared):
        from .modules.models import MagneticReleaseIntent
        self.check_ready()
        if self.motion is not None and not isinstance(prepared.intent, MagneticReleaseIntent):
            self.motion.consume_processing(prepared)
        return self.magnetic.execute(prepared)

    def prepare_magnetic_transfer(self, intent, bindings, selection):
        from dataclasses import replace
        from .modules.models import MagneticSourceSelection
        if not isinstance(selection, MagneticSourceSelection):
            raise DriverValidationError("DRV_OT_MAGNETIC_SELECTION", "Typed selection required")
        self.check_ready()
        self.bind_references(bindings)
        prepared = self.transfers.prepare_transfer(replace(self.resolve_request(intent), magnetic_selection=selection))
        return self.motion.pin_transfer(prepared) if self.motion is not None else prepared
