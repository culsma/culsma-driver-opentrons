from dataclasses import dataclass
from math import isfinite
from typing import Protocol
from ..operations import ResolvedContainerRef


@dataclass(frozen=True)
class TemperatureTargetVolume:
    reference: ResolvedContainerRef
    volume_uL: float | None

    def __post_init__(self):
        if not isinstance(self.reference, ResolvedContainerRef):
            raise TypeError("Typed material reference required")
        if self.volume_uL is not None and (type(self.volume_uL) not in {int, float}
                or not isfinite(self.volume_uL) or self.volume_uL < 0):
            raise ValueError("Known finite nonnegative volume or explicit unknown required")


@dataclass(frozen=True)
class TemperatureHoldIntent:
    step_id: str
    targets: tuple[ResolvedContainerRef, ...]
    temperature_celsius: float
    duration_seconds: float
    material_volumes: tuple[TemperatureTargetVolume, ...] = ()

    def __post_init__(self):
        if (not isinstance(self.step_id, str) or not self.step_id.strip()
                or not isinstance(self.targets, tuple) or not self.targets
                or len(set(self.targets)) != len(self.targets)):
            raise ValueError("A hold requires a step and distinct typed targets")
        if any(not isinstance(t, ResolvedContainerRef) for t in self.targets):
            raise TypeError("Typed container references are required")
        if any(type(v) not in {int,float} or not isfinite(v) for v in (self.temperature_celsius,self.duration_seconds)) or self.duration_seconds <= 0:
            raise ValueError("Finite temperature and positive duration are required")
        if (not isinstance(self.material_volumes, tuple)
                or any(not isinstance(value, TemperatureTargetVolume) for value in self.material_volumes)):
            raise TypeError("Typed material volumes required")


@dataclass(frozen=True)
class PreparedTemperatureHold:
    preparation_id: str
    intent: TemperatureHoldIntent
    module_id: str
    labware: str
    configuration_revision: str
    block_max_volume_uL: float | None = None


class TemperatureHoldPort(Protocol):
    def prepare(self, intent: TemperatureHoldIntent, bindings) -> PreparedTemperatureHold: ...
    def describe(self, prepared: PreparedTemperatureHold) -> dict: ...
    def execute(self, prepared: PreparedTemperatureHold): ...


@dataclass(frozen=True)
class OrbitalShakeIntent:
    step_id: str
    targets: tuple[ResolvedContainerRef, ...]
    rate_rpm: int
    duration_seconds: float
    temperature_celsius: float | None = None

    def __post_init__(self):
        if (not isinstance(self.step_id, str) or not self.step_id.strip()
                or not isinstance(self.targets, tuple) or not self.targets
                or len(set(self.targets)) != len(self.targets)):
            raise ValueError("A shake requires a step and distinct targets")
        if any(not isinstance(target, ResolvedContainerRef) for target in self.targets):
            raise TypeError("Typed container references are required")
        if type(self.rate_rpm) is not int:
            raise ValueError("Integral rpm is required")
        if type(self.duration_seconds) not in {int, float} or not isfinite(self.duration_seconds) or self.duration_seconds <= 0:
            raise ValueError("Positive finite duration is required")
        if self.temperature_celsius is not None and (
            type(self.temperature_celsius) not in {int, float}
            or not 37 <= self.temperature_celsius <= 95
        ):
            raise ValueError("Heater-Shaker temperature must be between 37 and 95 C")


@dataclass(frozen=True)
class PreparedOrbitalShake:
    preparation_id: str
    intent: OrbitalShakeIntent
    module_id: str
    labware: str
    configuration_revision: str


@dataclass(frozen=True)
class MagneticSeparationIntent:
    step_id: str
    targets: tuple[ResolvedContainerRef, ...]
    duration_seconds: float

    def __post_init__(self):
        if not isinstance(self.step_id, str) or not self.step_id.strip():
            raise ValueError("A separation step identity is required")
        if (not isinstance(self.targets, tuple) or not self.targets
                or any(not isinstance(t, ResolvedContainerRef) for t in self.targets)
                or len(set(self.targets)) != len(self.targets)):
            raise ValueError("Distinct typed targets are required")
        if type(self.duration_seconds) not in {int, float} or not isfinite(self.duration_seconds) or self.duration_seconds <= 0:
            raise ValueError("Positive finite duration is required")


@dataclass(frozen=True)
class MagneticReleaseIntent:
    step_id: str
    targets: tuple[ResolvedContainerRef, ...]
    producer_step_id: str

    def __post_init__(self):
        if (not isinstance(self.step_id, str) or not self.step_id.strip()
                or not isinstance(self.producer_step_id, str) or not self.producer_step_id.strip()
                or not isinstance(self.targets, tuple) or not self.targets
                or any(not isinstance(t, ResolvedContainerRef) for t in self.targets)
                or len(set(self.targets)) != len(self.targets)):
            raise ValueError("A release requires identities and distinct typed targets")


@dataclass(frozen=True)
class PreparedMagneticOperation:
    preparation_id: str
    intent: MagneticSeparationIntent | MagneticReleaseIntent
    module_id: str
    labware: str
    configuration_revision: str


@dataclass(frozen=True)
class MagneticSourceSelection:
    source_id: str
    producer_step_id: str
    output_slot: str = "1"
