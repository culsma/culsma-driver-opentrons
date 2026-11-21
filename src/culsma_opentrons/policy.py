"""Explicit, validated transfer execution strategy."""
from dataclasses import dataclass
import math
from numbers import Real

@dataclass(frozen=True)
class PrecisionMethod:
    method_id: str
    revision: str
    pipette: str
    minimum_volume_uL: float
    maximum_volume_uL: float
    aspirate_flow_rate_uL_s: float
    dispense_flow_rate_uL_s: float

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict) or set(raw) != set(cls.__dataclass_fields__):
            raise ValueError("precision_method requires identity, pipette, volume bounds and flow rates")
        if any(not isinstance(raw[k], str) or not raw[k].strip() for k in ('method_id','revision','pipette')):
            raise ValueError("Precision method identity, revision and pipette must be nonempty")
        for key in ('minimum_volume_uL','maximum_volume_uL','aspirate_flow_rate_uL_s','dispense_flow_rate_uL_s'):
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or value <= 0:
                raise ValueError("Precision method numeric parameters must be finite and positive")
        if raw['minimum_volume_uL'] > raw['maximum_volume_uL']:
            raise ValueError("Precision method volume bounds are reversed")
        return cls(**raw)


@dataclass(frozen=True)
class TransferPolicy:
    tip_policy: str = "fresh_per_stroke"
    stroke_mode: str = "single"
    aspirate_flow_rate_uL_s: float | None = None
    dispense_flow_rate_uL_s: float | None = None

    precision_method: PrecisionMethod | None = None

    @classmethod
    def from_dict(cls, raw):
        keys = {"tip_policy", "stroke_mode", "aspirate_flow_rate_uL_s", "dispense_flow_rate_uL_s", "precision_method"}
        if not isinstance(raw, dict) or set(raw) - keys:
            raise ValueError("Unknown transfer_policy fields")
        if raw.get("tip_policy", "fresh_per_stroke") != "fresh_per_stroke":
            raise ValueError("Only fresh_per_stroke tip policy is supported")
        if raw.get("stroke_mode", "single") not in {"single", "balanced_split"}:
            raise ValueError("stroke_mode must be single or balanced_split")
        for key in ("aspirate_flow_rate_uL_s", "dispense_flow_rate_uL_s"):
            if key in raw and raw[key] is not None:
                value = raw[key]
                if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or value <= 0:
                    raise ValueError(f"{key} must be a positive finite number")
        value = dict(raw)
        if value.get('precision_method') is not None:
            value['precision_method'] = PrecisionMethod.from_dict(value['precision_method'])
        return cls(**value)
