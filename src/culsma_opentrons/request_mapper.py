"""Map one runtime-ready Mutation into a typed transfer request without actions."""
from culsma.pipeline.plan_nodes import PlanStep
from culsma.driver.framework.capability import CapabilityPolicy
from .binding import OpentronsBindingResolver
from .models import DriverValidationError, TransferAliquot, TransferRequest
from .translator import volume_in_uL

class TransferRequestMapper:
    def __init__(self, bindings: OpentronsBindingResolver):
        self.bindings = bindings

    def from_step(self, step: PlanStep) -> TransferRequest:
        if step.op != "Mutation":
            raise DriverValidationError("DRV_OT_OP_UNSUPPORTED", "Transfer mapping requires Mutation")
        capability = CapabilityPolicy(set(), set()).evaluate(step)
        if not capability.ok:
            raise DriverValidationError(capability.code, "Unsupported transfer requirements or options")
        gate = step.gate or {}
        constraint = gate.get("constraint", {})
        if constraint.get("requirement_types"):
            raise DriverValidationError("DRV_REQ_TYPE_UNSUPPORTED", "No transfer requirement types are supported")
        if set(constraint) - {"requirements", "options", "requirement_types"} or any(key in gate for key in ("env", "env_targets")):
            raise DriverValidationError("DRV_OT_GATE_UNSUPPORTED", "Unconsumed transfer gate")
        if set(step.args) - {"target", "sources", "_runtime_material_resolution"}:
            raise DriverValidationError("DRV_OT_ARGS_UNSUPPORTED", "Unconsumed mutation arguments")
        sources = step.args.get("sources")
        if not isinstance(sources, list) or not sources:
            raise DriverValidationError("DRV_OT_VOLUME_UNSUPPORTED", "Explicit aliquots are required")
        target, address = self.bindings.resolve(step.args.get("target"))
        aliquots = []
        for pair in sources:
            if not isinstance(pair, dict) or pair.get("kind") != "IRPair":
                raise DriverValidationError("DRV_OT_VOLUME_UNSUPPORTED", "Whole-container and ratio transfers are unsupported")
            source, source_address = self.bindings.resolve(pair.get("left"))
            aliquots.append(TransferAliquot(source, source_address, volume_in_uL(pair.get("right"))))
        return TransferRequest(step.step_id, target, address, tuple(aliquots), step.span)
