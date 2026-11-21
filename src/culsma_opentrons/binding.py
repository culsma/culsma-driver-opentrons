"""Resolve semantic references using Culsma's owning runtime resolver."""
from typing import Any

from culsma.runtime.material.args import arg_string
from culsma.runtime.material.container_content import allocation_container_id
from culsma.runtime.material.ledger import normalize_capacity_uL
from culsma.runtime.material.refs import MaterialRefResolver
from culsma.runtime.state import RuntimeState

from .backend import OfficialSimulatorBackend
from .config import DeckConfiguration
from .models import ContainerHandle, DriverValidationError, WellAddress


class OpentronsBindingResolver:
    def __init__(self, state: RuntimeState, config: DeckConfiguration, backend: OfficialSimulatorBackend):
        self.state = state
        self.config = config
        self.backend = backend
        self.addresses: dict[ContainerHandle, WellAddress] = {}

    def allocation(self, step) -> tuple[ContainerHandle, WellAddress]:
        address = self.config.bindings.get(step.step_id)
        if address is None:
            raise DriverValidationError("DRV_OT_BINDING_MISSING", f"No physical binding for {step.step_id}")
        namespace = arg_string(step.args.get("container_namespace"))
        name = arg_string(step.args.get("container_name"))
        if namespace is None or name is None:
            raise DriverValidationError("DRV_OT_IDENTITY_MISSING", "Allocation identity is required")
        if arg_string(step.args.get("kind")) != "tube":
            raise DriverValidationError("DRV_OT_LABWARE_UNSUPPORTED", "This slice binds tube allocations only")
        capacity = normalize_capacity_uL(step.args.get("capacity"))
        if capacity is None:
            raise DriverValidationError("DRV_OT_CAPACITY_MISMATCH", "Explicit physical volume capacity is required")
        if capacity > self.backend.well(address).max_volume:
            raise DriverValidationError("DRV_OT_CAPACITY_MISMATCH", "Language capacity exceeds physical well capacity")
        return ContainerHandle(allocation_container_id(namespace=namespace, invocation_id=step.step_id, name=name)), address

    def resolve(self, reference: Any) -> tuple[ContainerHandle, WellAddress]:
        # No label lookup or stringification: authoritative semantic resolver first.
        material = self.state.artifacts.get("material_state", {})
        identity = MaterialRefResolver.resolve_target_ref(material, reference)
        if identity is None:
            raise DriverValidationError("DRV_OT_REFERENCE_UNRESOLVED", "Semantic container reference could not be resolved")
        handle = ContainerHandle(identity)
        if handle not in self.addresses:
            raise DriverValidationError("DRV_OT_BINDING_MISSING", f"No physical binding for container {identity}")
        return handle, self.addresses[handle]
