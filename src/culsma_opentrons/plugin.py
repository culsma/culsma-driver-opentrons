"""Plugin entry point and shared device session factory; no OS dependency."""

from contextlib import contextmanager
from importlib import import_module
import json
import os
import sys
from .models import DriverBuildContext
from culsma.runtime.state import RuntimeState
from .config import DeckConfiguration
from .support import validate_plan_support


class SimulatorSessionFactory:
    @staticmethod
    def execution_options():
        return ({'mode': 'simulation', 'simulator': 'opentrons', 'label': 'Opentrons simulator'},)

    def load_configuration(self, config_path):
        if config_path is None:
            raise ValueError("--driver-config deck.json is required")
        return DeckConfiguration.from_dict(
            json.loads(config_path.read_text(encoding="utf-8"))
        )

    def configure_vendor(self, artifacts_dir, strict_path=False):
        config_dir = (artifacts_dir / "vendor-config").resolve()
        if "opentrons.config" in sys.modules:
            if strict_path and os.environ.get("OT_API_CONFIG_DIR") != str(config_dir):
                raise ValueError(
                    "Opentrons configuration is already initialized; use the same artifact directory or a fresh process"
                )
            return
        config_dir.mkdir(parents=True, exist_ok=True)
        os.environ["OT_API_CONFIG_DIR"] = str(config_dir)

    def create(self, config, artifacts_dir, state=None, strict_path=False, state_dir=None):
        self.configure_vendor(artifacts_dir, strict_path)
        assembly = import_module("culsma_opentrons.assembly")
        return assembly.SimulatorSessionAssembler().build(
            state if state is not None else RuntimeState(), config,
            motion_path=(state_dir or artifacts_dir) / "motion-state.json",
            legacy_motion_path=artifacts_dir / "motion-state.json" if state_dir is not None else None
        )

    def create_from_file(self, config_path, artifacts_dir, state_dir=None):
        return self.create(self.load_configuration(config_path), artifacts_dir, state_dir=state_dir)


class OpentronsSimulatorFactory:
    def configuration_profile(self):
        from .profiles import ConfigurationProfileInteractor
        return ConfigurationProfileInteractor()

    def __init__(self, sessions=None):
        self.sessions = sessions or SimulatorSessionFactory()

    @contextmanager
    def open(self, context: DriverBuildContext):
        if context.config_path is None:
            raise ValueError("--driver-config deck.json is required")
        validate_plan_support(context.plan)
        config = self.sessions.load_configuration(context.config_path)
        if config.labware_moves:
            raise ValueError("Configured labware moves require OS execution")
        allocation_ids = {
            step.step_id
            for protocol in context.plan.plans
            for step in protocol.steps
            if step.op == "AllocContainer"
        }
        if set(config.bindings) != allocation_ids:
            raise ValueError(
                "Deck bindings must exactly match plan allocation IDs; inspect plan.json"
            )
        session = self.sessions.create(
            config, context.artifacts_dir, context.state, strict_path=True
        )
        try:
            yield session.driver
        finally:
            session.close()
