"""Pipette mixing uses real virtual SDK strokes, without a material transfer."""
from dataclasses import dataclass
from uuid import uuid4
from math import isfinite
from culsma.common.source import Span
from .models import (TransferCommand, TransferOperation, ContainerHandle, WellAddress, DriverValidationError)
from .translator import validate_endpoint, plan_strokes
from .interactors import TransferConstraintMethodInteractor


@dataclass(frozen=True)
class PipetteMixRequest:
    step_id: str
    target: ContainerHandle
    address: WellAddress
    volume_uL: float
    available_uL: float
    span: Span | None = None
    requirements: tuple[str, ...] = ()


    def __post_init__(self):
        if (not isinstance(self.step_id, str) or not self.step_id.strip()
                or not isinstance(self.target, ContainerHandle) or not isinstance(self.address, WellAddress)
                or any(type(v) not in (float, int) or not isfinite(v) or v <= 0
                       for v in (self.volume_uL, self.available_uL))):
            raise ValueError('Mixing requires a typed target and finite positive volumes')


class PipetteMixInteractor:
    def __init__(self, bindings):
        self.bindings = bindings
        self.preparations = {}

    def validate(self, request):
        backend = self.bindings.backend
        backend.check_ready()
        validate_endpoint(self.bindings, request.target, request.address)
        if not 0 < request.volume_uL <= request.available_uL <= backend.well(request.address).max_volume:
            raise DriverValidationError('DRV_OT_MIX_VOLUME', 'Mix stroke exceeds available liquid')
        plan_strokes(request.volume_uL, backend.pipette.min_volume, backend.pipette.max_volume, 'single')
        if backend.pipette.has_tip or backend.available_tips() < 1:
            raise DriverValidationError('DRV_OT_MIX_TIP', 'Mixing requires an available fresh tip')

    def prepare(self, request):
        self.validate(request)
        command = TransferCommand(request.step_id, request.target, request.target, request.address,
            request.address, request.volume_uL, request.step_id + '/mix', request.step_id + '/mix/0')
        policy, resolutions = TransferConstraintMethodInteractor(self.bindings).resolve(request.requirements, [command])
        operation = TransferOperation(request.step_id, (command,), policy, request.span, None, resolutions)
        self.bindings.backend.module_access.validate(operation)
        token = str(uuid4())
        motion = getattr(self.bindings.backend, "motion", None)
        revision = motion.state["revision"] if motion is not None else None
        self.preparations[token] = (request, operation, self.bindings.config.fingerprint(), revision)
        return token

    def execute(self, token):
        prepared = self.preparations.pop(token, None)
        if prepared is None:
            raise DriverValidationError("DRV_OT_MIX_PREPARATION", "Foreign or consumed mix preparation")
        request, operation, fingerprint, revision = prepared
        motion = getattr(self.bindings.backend, "motion", None)
        current = motion.state["revision"] if motion is not None else None
        if fingerprint != self.bindings.config.fingerprint() or revision != current:
            raise DriverValidationError("DRV_OT_MIX_STALE", "Configuration or plate position changed after preparation")
        self.validate(request)
        result = self.bindings.backend.run_prepared(operation)
        return result
