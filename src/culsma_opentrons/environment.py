"""Configured environment behavior models, distinct from Opentrons SDK modules."""
from dataclasses import dataclass, asdict
from math import isfinite
from .models import DriverValidationError
from .centrifuge.models import NumericRange


@dataclass(frozen=True)
class EnvironmentProfile:
    locations: tuple[str, ...]
    labware_types: tuple[str, ...]
    temperature_range_celsius: NumericRange
    maximum_hold_seconds: float
    allow_transfer: bool = False
    co2_range_percent: NumericRange | None = None

    @classmethod
    def from_dict(cls, value):
        data = dict(value)
        data['locations'] = tuple(data['locations'])
        data['labware_types'] = tuple(data['labware_types'])
        data['temperature_range_celsius'] = NumericRange(**data['temperature_range_celsius'])
        if data.get('co2_range_percent') is not None:
            data['co2_range_percent'] = NumericRange(**data['co2_range_percent'])
        return cls(**data)

    def __post_init__(self):
        if (not isinstance(self.temperature_range_celsius, NumericRange)
                or self.co2_range_percent is not None and not isinstance(self.co2_range_percent, NumericRange)
                or not self.locations or not self.labware_types or type(self.allow_transfer) is not bool
                or any(not isinstance(x, str) or not x for x in (*self.locations, *self.labware_types))
                or type(self.maximum_hold_seconds) not in (int, float)
                or not isfinite(self.maximum_hold_seconds) or self.maximum_hold_seconds <= 0):
            raise ValueError('Invalid environment profile')
        if self.co2_range_percent is not None and not 0 <= self.co2_range_percent.minimum <= self.co2_range_percent.maximum <= 100:
            raise ValueError('CO2 capability must be within 0–100 percent')


@dataclass(frozen=True)
class EnvironmentRequest:
    temperature_celsius: float
    duration_seconds: float = 0
    co2_percent: float | None = None

    def __post_init__(self):
        values = (self.temperature_celsius, self.duration_seconds)
        if any(type(v) not in (float, int) or not isfinite(v) for v in values) or self.duration_seconds < 0:
            raise ValueError('Finite environment values required')
        if self.co2_percent is not None and (type(self.co2_percent) not in (float, int)
                or not isfinite(self.co2_percent) or not 0 <= self.co2_percent <= 100):
            raise ValueError('CO2 must be within 0–100 percent')


class ReferenceEnvironmentInteractor:
    def __init__(self, config):
        self.config = config
        self.virtual_seconds = 0

    def select(self, request, labwares, positions, *, transfer=False):
        if not labwares or not set(labwares) <= self.config.labware.keys():
            raise DriverValidationError("DRV_OT_ENVIRONMENT_TARGETS", "Configured target labware is required")
        matches = []
        for name, profile in self.config.reference_environments.items():
            if (all(positions.get(lw) in profile.locations
                    and self.config.labware[lw]['load_name'] in profile.labware_types for lw in labwares)
                    and profile.temperature_range_celsius.includes(request.temperature_celsius)
                    and request.duration_seconds <= profile.maximum_hold_seconds
                    and (not transfer or profile.allow_transfer)
                    and (request.co2_percent is None or profile.co2_range_percent is not None
                         and profile.co2_range_percent.includes(request.co2_percent))):
                matches.append(name)
        if len(matches) != 1:
            raise DriverValidationError('DRV_OT_ENVIRONMENT_UNSUPPORTED',
                'Exactly one configured environment must support the location, temperature, CO2 and duration')
        return matches[0]

    def execute(self, step_id, request, labwares, positions, targets, *, transfer=False):
        name = self.select(request, labwares, positions, transfer=transfer)
        self.virtual_seconds += request.duration_seconds
        return {'operation_kind': 'environment', 'step_id': step_id, 'profile': name,
                'request': asdict(request), 'targets': list(targets), 'labware': sorted(labwares),
                'evidence_scope': 'reference_environment_model', 'mode': 'simulation',
                'temperature_measured': False, 'co2_measured': False,
                'virtual_elapsed_seconds': request.duration_seconds,
                'virtual_time_seconds': self.virtual_seconds, 'outcome': 'completed'}
