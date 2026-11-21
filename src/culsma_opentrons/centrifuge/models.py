"""Typed requests and device capabilities for a centrifuge."""

from dataclasses import asdict, dataclass
from enum import Enum
from hashlib import sha256
import json
from math import isfinite, sqrt, tau

from ..models import DriverValidationError, SimulatorBinding
from ..operations import ResolvedContainerRef


class DriveUnit(str, Enum):
    RCF = "g"
    RPM = "rpm"


class RotorPosition(str, Enum):
    SAMPLE = "sample"
    COUNTERWEIGHT = "counterweight"


def finite(value):
    if type(value) not in (int, float):
        return False
    try:
        return isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class NumericRange:
    minimum: float
    maximum: float

    def __post_init__(self):
        if not (finite(self.minimum) and finite(self.maximum) and self.minimum <= self.maximum):
            raise ValueError("An ordered finite range is required")

    def includes(self, value):
        return finite(value) and self.minimum <= value <= self.maximum


@dataclass(frozen=True)
class RotorGeometry:
    rotor_id: str
    radius_cm: float

    def __post_init__(self):
        if not isinstance(self.rotor_id, str) or not self.rotor_id.strip():
            raise ValueError("A rotor identity is required")
        if not finite(self.radius_cm) or self.radius_cm <= 0:
            raise ValueError("A positive finite rotor radius is required")

    @property
    def rcf_per_rpm_squared(self):
        return (tau / 60) ** 2 * (self.radius_cm / 100) / 9.80665


@dataclass(frozen=True)
class CentrifugeProfile:
    module_id: str
    profile_id: str
    drive_unit: DriveUnit
    drive_range: NumericRange
    max_duration_seconds: float
    labware_type: str
    max_imbalance_grams: float
    temperature_range_celsius: NumericRange | None = None
    rotor: RotorGeometry | None = None

    def __post_init__(self):
        for value in (self.module_id, self.profile_id, self.labware_type):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Module, profile and labware identities are required")
        if not isinstance(self.drive_unit, DriveUnit):
            raise ValueError("A typed drive unit is required")
        if not isinstance(self.drive_range, NumericRange) or self.drive_range.minimum <= 0:
            raise ValueError("A positive drive range is required")
        if not finite(self.max_duration_seconds) or self.max_duration_seconds <= 0:
            raise ValueError("A positive duration limit is required")
        if not finite(self.max_imbalance_grams) or self.max_imbalance_grams < 0:
            raise ValueError("A nonnegative imbalance limit is required")
        if self.temperature_range_celsius is not None and not isinstance(
            self.temperature_range_celsius, NumericRange
        ):
            raise ValueError("Temperature capability requires a typed range")
        if self.rotor is not None and not isinstance(self.rotor, RotorGeometry):
            raise ValueError("A typed rotor geometry is required")

    def fingerprint(self):
        return sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    def convert_drive(self, value, unit):
        if unit is self.drive_unit:
            return value
        if self.rotor is None:
            raise DriverValidationError(
                "DRV_OT_CENTRIFUGE_ROTOR_REQUIRED", "Drive conversion requires configured rotor geometry"
            )
        factor = self.rotor.rcf_per_rpm_squared
        if not finite(factor) or factor <= 0:
            raise DriverValidationError("DRV_OT_CENTRIFUGE_ROTOR_INVALID", "Invalid rotor conversion factor")
        if unit is DriveUnit.RCF:
            return sqrt(value) / sqrt(factor)
        return factor * value * value

    def validate(self, settings):
        if settings.drive_unit is not self.drive_unit or not self.drive_range.includes(settings.drive_value):
            raise DriverValidationError(
                "DRV_OT_CENTRIFUGE_DRIVE", f"Drive must be within the configured {self.drive_unit.value} range"
            )
        if not finite(settings.duration_seconds) or not 0 < settings.duration_seconds <= self.max_duration_seconds:
            raise DriverValidationError("DRV_OT_CENTRIFUGE_DURATION", "Duration exceeds the configured limits")
        temperature = settings.temperature_celsius
        if temperature is None:
            return
        if not finite(temperature):
            raise DriverValidationError("DRV_OT_CENTRIFUGE_TEMPERATURE_RANGE", "A finite temperature is required")
        limits = self.temperature_range_celsius
        if limits is None:
            raise DriverValidationError(
                "DRV_OT_CENTRIFUGE_TEMPERATURE_UNSUPPORTED",
                f"{self.module_id} does not provide temperature control for {temperature:g} °C",
            )
        if not limits.includes(temperature):
            raise DriverValidationError(
                "DRV_OT_CENTRIFUGE_TEMPERATURE_RANGE",
                f"Requested {temperature:g} °C is outside {limits.minimum:g}–{limits.maximum:g} °C",
            )


@dataclass(frozen=True)
class CentrifugeIntent:
    step_id: str
    targets: tuple[ResolvedContainerRef, ...]
    drive_value: float
    drive_unit: DriveUnit
    duration_seconds: float
    temperature_celsius: float | None = None

    def __post_init__(self):
        if not isinstance(self.step_id, str) or not self.step_id.strip():
            raise ValueError("A step identity is required")
        if (
            not isinstance(self.targets, tuple)
            or not self.targets
            or any(not isinstance(target, ResolvedContainerRef) for target in self.targets)
            or len(set(self.targets)) != len(self.targets)
        ):
            raise ValueError("Distinct resolved targets are required")
        if not isinstance(self.drive_unit, DriveUnit):
            raise ValueError("A typed drive unit is required")
        for value in (self.drive_value, self.duration_seconds):
            if not finite(value) or value <= 0:
                raise ValueError("Drive and duration must be positive and finite")
        if self.temperature_celsius is not None and not finite(self.temperature_celsius):
            raise ValueError("A finite temperature is required")


@dataclass(frozen=True)
class RotorLoad:
    labware: str
    labware_type: str
    mass_grams: float

    def __post_init__(self):
        for value in (self.labware, self.labware_type):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Labware identity and type are required")
        if not finite(self.mass_grams) or self.mass_grams <= 0:
            raise ValueError("A positive finite loaded mass is required")


@dataclass(frozen=True)
class CycleSettings:
    labware: str
    drive_value: float
    drive_unit: DriveUnit
    duration_seconds: float
    temperature_celsius: float | None


@dataclass(frozen=True)
class CentrifugeState:
    revision: int = 0
    lid_open: bool = False
    sample: RotorLoad | None = None
    counterweight: RotorLoad | None = None
    active_cycle: CycleSettings | None = None
    uncertain: bool = False
    virtual_time_seconds: float = 0
    modeled_temperature_celsius: float | None = None

    def require_idle(self):
        if self.uncertain or self.active_cycle is not None:
            raise DriverValidationError("DRV_OT_CENTRIFUGE_NOT_IDLE", "Centrifuge must be stopped and in a known state")

    def validate_cycle(self, settings, profile):
        self.require_idle()
        profile.validate(settings)
        if self.lid_open:
            raise DriverValidationError("DRV_OT_CENTRIFUGE_LID", "Close the lid before starting a cycle")
        if self.sample is None or self.sample.labware != settings.labware:
            raise DriverValidationError("DRV_OT_CENTRIFUGE_LOAD", "The requested labware is not loaded")
        if self.counterweight is None or abs(self.sample.mass_grams - self.counterweight.mass_grams) > profile.max_imbalance_grams:
            raise DriverValidationError("DRV_OT_CENTRIFUGE_BALANCE", "The loaded rotor is outside its balance limit")


@dataclass(frozen=True)
class PreparedCentrifuge:
    preparation_id: str
    intent: CentrifugeIntent
    bindings: tuple[SimulatorBinding, ...]
    module_id: str
    configuration_revision: str
    rotor_revision: int
    settings: CycleSettings


@dataclass(frozen=True)
class CycleObservation:
    virtual_elapsed_seconds: float
    virtual_time_seconds: float
    modeled_temperature_celsius: float | None
    rotor_revision: int
