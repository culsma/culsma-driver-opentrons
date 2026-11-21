"""Reference centrifuge profiles are separate from SDK module definitions."""

from dataclasses import asdict, dataclass, replace

from .models import CentrifugeProfile, DriveUnit, NumericRange, RotorGeometry, RotorLoad


@dataclass(frozen=True)
class ReferenceCentrifugeConfiguration:
    slot: str
    profile: CentrifugeProfile
    sample_masses_grams: dict[str, float]
    counterweight: RotorLoad

    def snapshot(self):
        value = asdict(self)
        value["profile"].pop("module_id")
        value["counterweight"].pop("labware_type")
        return value

    def sample_load(self, labware):
        return RotorLoad(labware, self.profile.labware_type, self.sample_masses_grams[labware])

    def capabilities(self):
        profile = self.profile
        limits = profile.drive_range
        ranges = {profile.drive_unit.value: [limits.minimum, limits.maximum]}
        if profile.rotor is not None:
            other = DriveUnit.RCF if profile.drive_unit is DriveUnit.RPM else DriveUnit.RPM
            conversion = replace(profile, drive_unit=other)
            ranges[other.value] = [conversion.convert_drive(bound, profile.drive_unit)
                                   for bound in ranges[profile.drive_unit.value]]
        result = {
            "labware": sorted(self.sample_masses_grams),
            "instrument_kind": "centrifuge",
            "drive_ranges": ranges,
            "maximum_hold_seconds": profile.max_duration_seconds,
        }
        if profile.temperature_range_celsius is not None:
            result["environment_conditions"] = {
                "thermal": {"unit": "C", **asdict(profile.temperature_range_celsius)}
            }
        return result

    @classmethod
    def parse_all(cls, raw, robot_type, labware, modules, tiprack, slots, plate_profiles):
        if not isinstance(raw, dict) or (raw and robot_type != "Flex"):
            raise ValueError("Reference centrifuges require a Flex configuration")
        occupied = {"A3", tiprack["slot"], *(spec["slot"] for spec in modules.values()),
                    *(spec["slot"] for spec in labware.values() if "slot" in spec)}
        result = {}
        for name, spec in raw.items():
            if not isinstance(name, str) or not name.isidentifier() or name in modules:
                raise ValueError("A distinct reference centrifuge identity is required")
            dock = cls.parse(name, spec, labware, plate_profiles)
            if dock.slot not in slots or dock.slot in occupied:
                raise ValueError("Reference centrifuge slot must be empty and unique")
            occupied.add(dock.slot)
            result[name] = dock
        return result

    @classmethod
    def parse(cls, name, spec, labware, plate_profiles):
        fields = {"slot", "profile", "sample_masses_grams", "counterweight"}
        if not isinstance(spec, dict) or set(spec) != fields or not isinstance(spec["slot"], str):
            raise ValueError("A reference centrifuge requires slot, profile and rotor loads")
        try:
            values = dict(spec["profile"])
            values["drive_unit"] = DriveUnit(values["drive_unit"])
            values["drive_range"] = NumericRange(**values["drive_range"])
            if values.get("temperature_range_celsius") is not None:
                values["temperature_range_celsius"] = NumericRange(**values["temperature_range_celsius"])
            if values.get("rotor") is not None:
                values["rotor"] = RotorGeometry(**values["rotor"])
            profile = CentrifugeProfile(module_id=name, **values)
            counterweight = RotorLoad(labware_type=profile.labware_type, **spec["counterweight"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Invalid reference centrifuge profile for {name}: {error}") from error
        masses = spec["sample_masses_grams"]
        if profile.labware_type not in plate_profiles or not isinstance(masses, dict) or not masses:
            raise ValueError("The reference centrifuge needs compatible movable plates")
        for plate, mass in masses.items():
            if labware.get(plate, {}).get("load_name") != profile.labware_type:
                raise ValueError("Reference centrifuge load does not match configured labware")
            RotorLoad(plate, profile.labware_type, mass)
        if counterweight.labware in labware:
            raise ValueError("The reference counterweight must be an independent inert load")
        return cls(spec["slot"], profile, dict(masses), counterweight)
