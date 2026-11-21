"""Resolved explicit-volume boundary snapshots; not an offline program model.

Container identities must come from the owning core resolver. These values do not
resolve author variable names, evaluate expressions or establish material validity.
"""

from dataclasses import dataclass
from math import isfinite
from typing import Generic, TypeVar
from culsma.common.source import Span

PayloadT = TypeVar("PayloadT", covariant=True)


@dataclass(frozen=True)
class ResolvedContainerRef:
    container_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.container_id, str) or not self.container_id.strip():
            raise ValueError("container_id must be a resolved nonempty identity")


@dataclass(frozen=True)
class TransferAliquot:
    source: ResolvedContainerRef
    volume_uL: float

    def __post_init__(self) -> None:
        if not isinstance(self.source, ResolvedContainerRef):
            raise TypeError("source must be ResolvedContainerRef")
        if (
            isinstance(self.volume_uL, bool)
            or not isinstance(self.volume_uL, (int, float))
            or not isfinite(self.volume_uL)
            or self.volume_uL <= 0
        ):
            raise ValueError("volume_uL must be positive and finite")


@dataclass(frozen=True)
class TransferIntent:
    step_id: str
    target: ResolvedContainerRef
    aliquots: tuple[TransferAliquot, ...]
    span: Span | None = None
    requirements: tuple[str, ...] = ()
    constraint_options: tuple[str, ...] = ()
    environment_requirements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.step_id, str) or not self.step_id.strip():
            raise ValueError("step_id must be nonempty")
        if not isinstance(self.target, ResolvedContainerRef):
            raise TypeError("target must be ResolvedContainerRef")
        if (
            not isinstance(self.aliquots, tuple)
            or not self.aliquots
            or not all(isinstance(a, TransferAliquot) for a in self.aliquots)
        ):
            raise TypeError("aliquots must be a nonempty tuple of TransferAliquot")
        if self.span is not None and not isinstance(self.span, Span):
            raise TypeError("span must be Span")
        for values in (
            self.requirements,
            self.constraint_options,
            self.environment_requirements,
        ):
            if not isinstance(values, tuple) or not all(
                isinstance(v, str) and v.strip() for v in values
            ):
                raise TypeError(
                    "support requirements must be immutable nonempty string tuples"
                )


@dataclass(frozen=True)
class ActionMapping:
    action_id: str
    aliquot_index: int


@dataclass(frozen=True)
class DeviceResourceEffect:
    resource_id: str
    consumed: int


@dataclass(frozen=True)
class TransferPlan(Generic[PayloadT]):
    step_id: str
    payload: PayloadT
    source_mapping: tuple[ActionMapping, ...]
    resource_effects: tuple[DeviceResourceEffect, ...]
    environment_id: str
    span: Span | None = None


class DriverPlanningError(ValueError):
    def __init__(self, code: str, message: str, intent: TransferIntent):
        super().__init__(message)
        self.code = code
        self.step_id = intent.step_id
        self.span = intent.span
