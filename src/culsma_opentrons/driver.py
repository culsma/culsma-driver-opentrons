"""Culsma Driver protocol implementation with explicit simulated receipts."""
from culsma.driver.base import DriverCapabilityResult, DriverResult
from culsma.driver.framework.capability import CapabilityPolicy

from .contracts import TransferDriver
from .binding import OpentronsBindingResolver
from .models import DriverValidationError
from .request_mapper import TransferRequestMapper

SETUP_OPS = frozenset({"AllocContainer", "DefineContent", "LoadContent", "AnnotateContent"})


class OpentronsSimulatorDriver:
    supported_requirement_types = frozenset()

    def __init__(self, bindings: OpentronsBindingResolver, mapper: TransferRequestMapper, transfers: TransferDriver):
        self.bindings = bindings
        self.mapper = mapper
        self.transfers = transfers
        self.backend = bindings.backend
        self.capability = CapabilityPolicy(supported_requirements=set(), supported_constraint_option_keys=set())

    def check(self, step) -> DriverCapabilityResult:
        if self.backend.poisoned:
            return DriverCapabilityResult(False, "DRV_OT_SESSION_FAILED", payload={"retry_safe": False})
        result = self.capability.evaluate(step)
        if not result.ok:
            return result
        gate = step.gate or {}
        constraint = gate.get("constraint", {})
        if set(constraint) - {"requirements", "options", "requirement_types"}:
            return DriverCapabilityResult(False, "DRV_OT_GATE_UNSUPPORTED")
        if any(key in gate for key in ("env", "env_targets")):
            return DriverCapabilityResult(False, "DRV_OT_GATE_UNSUPPORTED")
        if step.op not in SETUP_OPS | {"Mutation"}:
            return DriverCapabilityResult(False, "DRV_OT_OP_UNSUPPORTED")
        try:
            if step.op == "AllocContainer":
                self.bindings.allocation(step)
            elif step.op == "Mutation":
                return self.transfers.check_transfer(self.mapper.from_step(step))
        except DriverValidationError as error:
            return DriverCapabilityResult(False, error.code, payload={"message": str(error)})
        return DriverCapabilityResult(True, "DRV_OT_SUPPORTED")

    def execute(self, step) -> DriverResult:
        supported = self.check(step)
        if not supported.ok:
            return DriverResult(False, supported.code, {**supported.payload, "error_severity": "fatal", "mode": "simulation"})
        if step.op in SETUP_OPS:
            if step.op == "AllocContainer":
                handle, address = self.bindings.allocation(step)
                self.bindings.addresses[handle] = address
            return DriverResult(True, "DRV_OT_RUNTIME_SETUP", {"step_id": step.step_id, "op": step.op,
                                "mode": "simulation", "physical_action": False})
        try:
            prepared = self.transfers.prepare_transfer(self.mapper.from_step(step))
        except DriverValidationError as error:
            return DriverResult(False, error.code, {"step_id": step.step_id, "message": str(error),
                "mode": "simulation", "error_severity": "fatal", "physical_action": False})
        receipt = self.transfers.execute_transfer(prepared)
        return DriverResult(receipt.ok, receipt.code, {**receipt.to_payload(),
                           "material_resolution": step.args.get("_runtime_material_resolution")})
