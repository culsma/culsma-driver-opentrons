from dataclasses import asdict, dataclass
from typing import Any, Literal
from .operations import ResolvedContainerRef
from .modules.models import MagneticSourceSelection
from copy import deepcopy
from culsma.common.source import Span
from .policy import TransferPolicy
from dataclasses import field
from pathlib import Path
from culsma.pipeline.plan_nodes import PlanProgram
from culsma.runtime.state import RuntimeState

"""Typed driver identities, physical addresses and immutable commands."""


@dataclass(frozen=True)
class ContainerHandle:
    id: str


@dataclass(frozen=True)
class WellAddress:
    labware: str
    well: str


@dataclass(frozen=True)
class TransferAliquot:
    source: ContainerHandle
    source_address: WellAddress
    volume_uL: float


@dataclass(frozen=True)
class TransferRequest:
    step_id: str
    target: ContainerHandle
    target_address: WellAddress
    aliquots: tuple[TransferAliquot, ...]
    span: Span | None = None
    magnetic_selection: MagneticSourceSelection | None = None
    requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppliedTransferConstraint:
    requirement: str
    method_id: str
    revision: str
    parameters: tuple[tuple[str, str | float], ...]
    evidence_scope: str = "configured_method_execution"
    status: str | None = None
    limitation: str | None = None

    def to_payload(self):
        payload = {"requirement": self.requirement, "method_id": self.method_id, "revision": self.revision,
                "parameters": dict(self.parameters), "evidence_scope": self.evidence_scope}
        if self.status is not None:
            payload.update(status=self.status, limitation=self.limitation)
        return payload


@dataclass(frozen=True)
class TransferCommand:
    step_id: str
    source: ContainerHandle
    target: ContainerHandle
    source_address: WellAddress
    target_address: WellAddress
    volume_uL: float
    aliquot_id: str
    stroke_id: str


@dataclass(frozen=True)
class TransferOperation:
    step_id: str
    commands: tuple[TransferCommand, ...]
    policy: TransferPolicy
    span: Span | None = None
    magnetic_selection: MagneticSourceSelection | None = None
    constraint_resolution: tuple[AppliedTransferConstraint, ...] = ()


@dataclass(frozen=True)
class PreparedTransfer:
    """One-shot preparation owned by the issuing transfer session."""

    preparation_id: str
    request: TransferRequest
    operation: TransferOperation


@dataclass(frozen=True)
class PreparedMultichannelTransfer:
    preparation_id: str
    group_id: str
    intents: tuple
    requests: tuple[TransferRequest, ...]
    operation: TransferOperation
    configuration_revision: str


@dataclass(frozen=True)
class PreparedMultichannelBatch:
    preparation_id: str
    group_id: str
    requests: tuple[TransferRequest, ...]
    waves: tuple[PreparedMultichannelTransfer, ...]
    configuration_revision: str


@dataclass(frozen=True)
class TransferFailure:
    code: str
    message: str
    exception_type: str | None = None


@dataclass(frozen=True)
class TransferActionObservation:
    action_id: str
    stroke_id: str
    action: Literal["pick_up_tip", "aspirate", "dispense", "drop_tip"]
    status: Literal["not_started", "completion_unknown", "returned"]


@dataclass(frozen=True)
class TransferReceipt:
    step_id: str
    phase: Literal["simulated", "rejected", "failed"]
    operation: TransferOperation | None
    action_results: tuple[TransferActionObservation, ...]
    commands: tuple[str, ...]
    config_sha256: str
    effective_config: dict[str, Any]
    versions: dict[str, str]
    instrument: dict[str, float]
    failure: TransferFailure | None = None
    span: Span | None = None
    module_action_results: tuple[dict, ...] = ()

    @property
    def ok(self) -> bool:
        return self.phase == "simulated" and self.failure is None

    @property
    def code(self) -> str:
        return self.failure.code if self.failure else "DRV_OT_SIMULATED"

    def to_payload(self) -> dict[str, Any]:
        operation = self.operation
        payload = {
            "schema": "opentrons_transfer_receipt_v1",
            "mode": "simulation",
            "backend": "official-opentrons",
            "robot_type": self.effective_config["robot_type"],
            "step_id": self.step_id,
            "phase": self.phase,
            "operation_ok": self.ok,
            "span": asdict(self.span) if self.span else None,
            "versions": dict(self.versions),
            "instrument": dict(self.instrument),
            "config_sha256": self.config_sha256,
            "effective_config": deepcopy(self.effective_config),
            "policy": (
                asdict(operation.policy)
                if operation
                else self.effective_config["transfer_policy"]
            ),
            "transfers": [asdict(c) for c in operation.commands] if operation else [],
            "action_results": [asdict(a) for a in self.action_results],
            "commands": list(self.commands),
            "evidence": "virtual_api_observation",
            "retry_safe": None if self.ok else False,
        }
        if operation and operation.constraint_resolution:
            payload["constraint_resolution"] = [c.to_payload() for c in operation.constraint_resolution]
        if self.module_action_results:
            payload["module_action_results"] = deepcopy(list(self.module_action_results))
        if self.failure:
            payload.update(
                message=self.failure.message,
                exception_type=self.failure.exception_type,
                error_severity="fatal",
            )
        return payload


class DriverValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SimulatorBinding:
    reference: ResolvedContainerRef
    address: WellAddress


@dataclass(frozen=True)
class SimulatorSessionDescription:
    device_id: str
    mode: str
    configuration_revision: str
    shared_resources: tuple[str, ...]
    consumable_capacities: dict[str, int]
    positions: dict[str, WellAddress]
    capacities: dict[tuple[str, str], float]
    labware_geometries: dict[str, tuple[int, int]] = field(default_factory=dict)
    transfer_channels: int = 1
    processing_capabilities: dict = field(default_factory=dict)
    feedback_completion_units: tuple[str, ...] | None = None
    motion_capabilities: dict = field(default_factory=dict)
    transfer_requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class TransferFeedback:
    outcome: str
    mode: str
    step_id: str
    configuration_revision: str
    consumed_resources: dict[str, int]
    payload: dict[str, Any]


@dataclass(frozen=True)
class DriverBuildContext:
    plan: PlanProgram
    state: RuntimeState
    config_path: Path | None = None
    artifacts_dir: Path = Path("run-artifacts/drivers")
    fail_ops: frozenset[str] = field(default_factory=frozenset)
