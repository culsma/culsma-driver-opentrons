"""Pure operation preparation protocols, independent of delivery capability."""

from typing import Protocol, TypeVar

IntentT = TypeVar("IntentT", contravariant=True)
ContextT = TypeVar("ContextT", contravariant=True)
PlanT = TypeVar("PlanT", covariant=True)


class OperationTranslator(Protocol[IntentT, ContextT, PlanT]):
    def prepare(self, intent: IntentT, context: ContextT) -> PlanT:
        """Return a plan without device actions, IO or language-state changes."""
        ...
