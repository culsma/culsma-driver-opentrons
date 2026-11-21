"""Transfer is a typed specialization of the common preparation protocol."""

from typing import Protocol, TypeVar
from .contracts import OperationTranslator
from .models import TransferIntent, TransferPlan

ContextT = TypeVar("ContextT", contravariant=True)
PayloadT = TypeVar("PayloadT", covariant=True)


class TransferOperationTranslator(
    OperationTranslator[TransferIntent, ContextT, TransferPlan[PayloadT]],
    Protocol[ContextT, PayloadT],
):
    """Inherits prepare(intent, context); no second transfer-specific entry."""
