"""Route a common thermal intent through injected vendor implementations."""

from collections.abc import Mapping

from ..models import DriverValidationError
from .interactors import ModuleScopeInteractor
from .models import PreparedTemperatureHold, TemperatureHoldIntent, TemperatureHoldPort


class ThermalHoldRouterInteractor:
    def __init__(self, backend, bindings, implementations: Mapping[str, TemperatureHoldPort]):
        self.backend, self.bindings = backend, bindings
        self.implementations = dict(implementations)
        self.receipts = []
        self.owners = {}

    def prepare(self, intent, bindings):
        self.backend.check_ready()
        if not isinstance(intent, TemperatureHoldIntent):
            raise DriverValidationError("DRV_OT_MODULE_REQUEST", "Typed temperature hold required")
        module_id, _ = ModuleScopeInteractor(self.backend, self.bindings).resolve(
            intent, bindings, tuple(self.implementations))
        implementation = self.implementations[self.bindings.config.modules[module_id]["model"]]
        prepared = implementation.prepare(intent, bindings)
        self.owners[prepared.preparation_id] = (prepared, implementation)
        return prepared

    def owner(self, prepared):
        if not isinstance(prepared, PreparedTemperatureHold):
            raise DriverValidationError("DRV_OT_MODULE_PREPARATION", "Typed temperature preparation required")
        entry = self.owners.get(prepared.preparation_id)
        if entry is None or entry[0] is not prepared:
            raise DriverValidationError("DRV_OT_MODULE_PREPARATION", "Foreign or modified preparation")
        return entry[1]

    def describe(self, prepared):
        return self.owner(prepared).describe(prepared)

    def execute(self, prepared):
        receipt = self.owner(prepared).execute(prepared)
        self.receipts.append(receipt)
        return receipt
