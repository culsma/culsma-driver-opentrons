"""Static separation refusal before optional vendor initialization."""
from .models import DriverValidationError

def validate_plan_support(plan) -> None:
    for protocol in plan.plans:
        for step in protocol.steps:
            if step.op == "sep":
                raise DriverValidationError("DRV_OT_SEPARATION_UNSUPPORTED",
                    f"DRV_OT_SEPARATION_UNSUPPORTED: {step.step_id}: separation requires a program-specific device executor")
