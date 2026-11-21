"""Prepare and consume transfer plans; the device port only runs prepared operations."""
from uuid import uuid4
from culsma.driver.base import DriverCapabilityResult
from .contracts import TransferExecutor, TransferOperationTranslator
from .models import DriverValidationError, PreparedTransfer, TransferFailure, TransferReceipt, TransferRequest


class OpentronsTransferDriver:
    def __init__(self, translator: TransferOperationTranslator, executor: TransferExecutor):
        self.translator = translator
        self.executor = executor
        self.preparations: dict[str, PreparedTransfer] = {}

    def supported_requirements(self):
        return self.translator.supported_requirements()

    def check_transfer(self, request: TransferRequest) -> DriverCapabilityResult:
        try:
            self.executor.check_ready()
            operation = self.translator.prepare_transfer(request)
        except DriverValidationError as error:
            return DriverCapabilityResult(False, error.code, payload={"message": str(error)})
        return DriverCapabilityResult(True, "DRV_OT_SUPPORTED", payload={"stroke_count": len(operation.commands)})

    def prepare_transfer(self, request: TransferRequest) -> PreparedTransfer:
        self.executor.check_ready()
        operation = self.translator.prepare_transfer(request)
        prepared = PreparedTransfer(str(uuid4()), request, operation)
        self.preparations[prepared.preparation_id] = prepared
        return prepared

    def execute_transfer(self, prepared: PreparedTransfer) -> TransferReceipt:
        if not isinstance(prepared, PreparedTransfer):
            raise DriverValidationError("DRV_OT_PREPARATION_INVALID", "execute_transfer requires PreparedTransfer")
        issued = self.preparations.get(prepared.preparation_id)
        if issued is not prepared:
            return self.executor.reject_transfer(prepared.request, TransferFailure(
                "DRV_OT_PREPARATION_INVALID", "Preparation is foreign, modified or already consumed"))
        # Every submission consumes its preparation, including a no-action rejection.
        del self.preparations[prepared.preparation_id]
        try:
            self.executor.check_ready()
            current = self.translator.prepare_transfer(prepared.request)
            if current != prepared.operation:
                raise DriverValidationError("DRV_OT_PREPARATION_STALE", "Device plan changed; prepare again")
        except DriverValidationError as error:
            return self.executor.reject_transfer(prepared.request, TransferFailure(error.code, str(error)))
        return self.executor.run_prepared(prepared.operation)
