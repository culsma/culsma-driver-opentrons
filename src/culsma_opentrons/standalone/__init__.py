"""Independent CLI program execution through the language Driver contract."""
from contextlib import contextmanager
from pathlib import Path
import json
from culsma.cli import execute_pipeline
from culsma.pipeline.plan_nodes import PlanStepTree
from ..plugin import SimulatorSessionFactory
from .driver import StandaloneDriver


class StandaloneProgramExecutor:
    def __init__(self, sessions=None):
        self.sessions = sessions or SimulatorSessionFactory()

    def validate_plan(self, plan, config):
        if 'multi' in config.pipette:
            raise ValueError('Standalone scalar requests require a single-channel pipette')
        steps = [s for p in plan.plans for s in PlanStepTree.leaves(p.steps)]
        if set(config.bindings) != {s.step_id for s in steps if s.op == 'AllocContainer'}:
            raise ValueError('Deck bindings must exactly match plan allocation IDs')
        executable = {s.step_id for s in steps if s.op in {'Mutation', 'sep', 'env_hold'}}
        if set(config.labware_moves) - executable:
            raise ValueError('Movement routes must name device operation step IDs')

    def execute(self, request):
        config = self.sessions.load_configuration(request.config_path)
        artifacts = request.artifacts_dir or Path('run-artifacts')
        device_dir = artifacts / 'opentrons'
        device_dir.mkdir(parents=True, exist_ok=True)
        marker = device_dir / 'execution-started.json'
        if marker.exists():
            raise ValueError('DRV_OT_RUN_EXISTS: use a new artifact directory; automatic resume is unsupported')
        sessions = []

        @contextmanager
        def preflight_factory(plan, state):
            self.validate_plan(plan, config)
            session = self.sessions.create(config, device_dir)
            sessions.append(session)
            yield StandaloneDriver(state, config, session, preflight=True)

        @contextmanager
        def execution_factory(plan, state):
            self.validate_plan(plan, config)
            # Exclusive creation prevents accidental replay of an interrupted run.
            with marker.open('x') as stream:
                json.dump({'mode': 'simulation', 'configuration_revision': config.fingerprint()}, stream)
            driver = StandaloneDriver(state, config, sessions[0])
            try:
                yield driver
            finally:
                (device_dir / 'evidence.json').write_text(json.dumps(state.artifacts['device_execution'], indent=2))

        options = dict(input_paths=list(request.input_paths), fail_ops=set(request.fail_ops),
                       material_state_path=request.material_state_path, inventory_check=request.inventory_check,
                       library_roots=list(request.library_roots))
        try:
            preview = execute_pipeline(**options, driver_factory=preflight_factory)
            if not preview['summary']['runtime_ok']:
                preview['summary']['device_preflight_ok'] = False
                preview['summary']['device_execution_started'] = False
                return preview
            result = execute_pipeline(**options, driver_factory=execution_factory)
            result['summary'].update(device_preflight_ok=True, device_execution_started=True)
            return result
        finally:
            for session in sessions:
                session.close()


class StandaloneProgramExecutorFactory:
    def build(self):
        return StandaloneProgramExecutor()
