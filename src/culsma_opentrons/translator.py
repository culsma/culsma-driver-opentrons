"""Translate a narrow, explicitly supported semantic transfer subset."""
import math
from numbers import Real

from culsma.runtime.material.units import VOLUME_TO_UL

from .binding import OpentronsBindingResolver
from .models import DriverValidationError, TransferCommand, TransferOperation, TransferRequest


def volume_in_uL(quantity) -> float:
    if not isinstance(quantity, dict) or quantity.get("kind") != "IRQuantity":
        raise DriverValidationError("DRV_OT_VOLUME_UNSUPPORTED", "An explicit volume aliquot is required")
    value, unit = quantity.get("value"), quantity.get("unit")
    if isinstance(value, bool) or not isinstance(value, Real) or unit not in VOLUME_TO_UL:
        raise DriverValidationError("DRV_OT_VOLUME_UNSUPPORTED", "Only numeric volume quantities are supported")
    volume = float(value) * VOLUME_TO_UL[unit]
    if not math.isfinite(volume) or volume <= 0:
        raise DriverValidationError("DRV_OT_VOLUME_RANGE", "Volume must be positive and finite")
    return volume


class OpentronsTransferTranslator:
    def __init__(self, bindings: OpentronsBindingResolver, constraints=None):
        from .interactors import TransferConstraintMethodInteractor
        self.bindings = bindings
        self.constraints = constraints if constraints is not None else TransferConstraintMethodInteractor(bindings)

    def supported_requirements(self):
        return self.constraints.capabilities()

    def prepare_transfer(self, request: TransferRequest) -> TransferOperation:
        if self.bindings.backend.pipette.channels != 1:
            raise DriverValidationError("DRV_OT_MULTICHANNEL_GROUP_REQUIRED", "Use an explicit complete-column group with this pipette")
        if not isinstance(request, TransferRequest) or not request.step_id or not request.aliquots:
            raise DriverValidationError("DRV_OT_REQUEST_INVALID", "A step identity and nonempty aliquots are required")
        target, target_address = request.target, request.target_address
        validate_endpoint(self.bindings, target, target_address)
        commands = []
        policy = self.bindings.config.transfer_policy
        for aliquot_index, aliquot in enumerate(request.aliquots):
            source, source_address = aliquot.source, aliquot.source_address
            validate_endpoint(self.bindings, source, source_address)
            if source == target:
                raise DriverValidationError("DRV_OT_SELF_TRANSFER_UNSUPPORTED", "Source and destination must differ")
            volume = aliquot.volume_uL
            if isinstance(volume, bool) or not isinstance(volume, Real) or not math.isfinite(volume) or volume <= 0:
                raise DriverValidationError("DRV_OT_VOLUME_RANGE", "Volume must be positive and finite")
            pipette = self.bindings.backend.pipette
            volumes = plan_strokes(volume, pipette.min_volume, pipette.max_volume, policy.stroke_mode)
            aliquot_id = f"{request.step_id}/aliquot/{aliquot_index}"
            for stroke_index, stroke_volume in enumerate(volumes):
                commands.append(TransferCommand(request.step_id, source, target, source_address, target_address,
                    stroke_volume, aliquot_id, f"{aliquot_id}/stroke/{stroke_index}"))
        if self.bindings.backend.pipette.has_tip:
            raise DriverValidationError("DRV_OT_TIP_ATTACHED", "Fresh-tip execution requires no attached tip")
        available = self.bindings.backend.available_tips()
        if len(commands) > available:
            raise DriverValidationError("DRV_OT_TIPS_INSUFFICIENT", f"Need {len(commands)} fresh tips; {available} available")
        policy, resolutions = self.constraints.resolve(request.requirements, commands)
        operation = TransferOperation(request.step_id, tuple(commands), policy, request.span, request.magnetic_selection, resolutions)
        self.bindings.backend.module_access.validate(operation)
        return operation


def plan_strokes(volume: float, minimum: float, maximum: float, mode: str) -> tuple[float, ...]:
    if volume < minimum or (mode == "single" and volume > maximum):
        raise DriverValidationError("DRV_OT_VOLUME_RANGE", "Aliquot does not fit selected pipette/stroke policy")
    if mode not in {"single", "balanced_split"}:
        raise DriverValidationError("DRV_OT_POLICY_UNSUPPORTED", "Unknown stroke policy")
    count = math.ceil(volume / maximum)
    stroke = volume / count
    if stroke < minimum:
        raise DriverValidationError("DRV_OT_VOLUME_RANGE", "Balanced stroke is below pipette minimum")
    volumes = (stroke,) * (count - 1) + (volume - stroke * (count - 1),)
    if not all(minimum <= v <= maximum for v in volumes):
        raise DriverValidationError("DRV_OT_VOLUME_RANGE", "Stroke is outside instrument range")
    return volumes


def validate_endpoint(bindings, handle, address) -> None:
    if bindings.addresses.get(handle) != address:
        raise DriverValidationError("DRV_OT_BINDING_STALE", "Request binding is missing or changed; remap current references")
    backend = bindings.backend
    if backend.config.reference_devices:
        slot = str(backend.labware[address.labware].parent)
        if any(dock.slot == slot for dock in backend.config.reference_devices.values()):
            raise DriverValidationError("DRV_OT_CENTRIFUGE_PIPETTING", "Return the plate from the centrifuge before pipetting")
