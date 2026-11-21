"""Composition only: the session consumes one thermal capability boundary."""

from .interactors import ModuleConfigurationInteractor, TemperatureHoldInteractor
from .thermal_router_interactors import ThermalHoldRouterInteractor
from .thermocycler_interactors import ThermocyclerHoldInteractor
from .incubation_interactors import HeaterShakerHoldInteractor


class ModuleInteractorAssembler:
    def thermal(self, backend, bindings):
        return ThermalHoldRouterInteractor(backend, bindings, {
            ModuleConfigurationInteractor.temperature: TemperatureHoldInteractor(backend, bindings),
            ModuleConfigurationInteractor.thermocycler: ThermocyclerHoldInteractor(backend, bindings),
            ModuleConfigurationInteractor.heater_shaker: HeaterShakerHoldInteractor(backend, bindings),
        })
