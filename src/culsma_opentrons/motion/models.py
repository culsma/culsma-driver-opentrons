from dataclasses import dataclass


@dataclass(frozen=True)
class LabwareMoveIntent:
    step_id: str
    labware: str
    source: str
    destination: str
    position_revision: int


@dataclass(frozen=True)
class PreparedLabwareMove:
    preparation_id: str
    intent: LabwareMoveIntent
    configuration_revision: str
    position_revision: int
