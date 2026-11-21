"""Detailed transfer contracts, independent of raw PlanStep representation."""
from typing import Protocol
from culsma.driver.base import DriverCapabilityResult
from .models import PreparedTransfer, TransferFailure, TransferOperation, TransferReceipt, TransferRequest

class TransferOperationTranslator(Protocol):
    def supported_requirements(self) -> tuple[str, ...]: ...

    def prepare_transfer(self, request: TransferRequest) -> TransferOperation:
        """Validate every aliquot and plan strokes without issuing device actions."""
        ...

class TransferExecutor(Protocol):
    def check_ready(self) -> None:
        """Raise DriverValidationError if this session can no longer execute."""
        ...

    def reject_transfer(self, request: TransferRequest, failure: TransferFailure) -> TransferReceipt:
        """Construct a no-action rejection receipt."""
        ...

    def run_prepared(self, operation: TransferOperation) -> TransferReceipt:
        """Execute structured strokes; return typed success or failure observations."""
        ...

class TransferDriver(Protocol):
    def supported_requirements(self) -> tuple[str, ...]: ...

    def check_transfer(self, request: TransferRequest) -> DriverCapabilityResult:
        ...

    def prepare_transfer(self, request: TransferRequest) -> PreparedTransfer:
        """Prepare without actions using this session's injected device context."""
        ...

    def execute_transfer(self, prepared: PreparedTransfer) -> TransferReceipt:
        """Consume an issued preparation once; revalidate resources before device actions."""
        ...


class TransferConstraintMethodPort(Protocol):
    def available(self) -> bool: ...
    def apply(self, policy, commands): ...
