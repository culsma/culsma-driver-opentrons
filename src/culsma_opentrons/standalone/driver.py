"""Core Driver implementation for a standalone simulated workstation."""
from copy import deepcopy
from culsma.driver.base import DriverCapabilityResult, DriverResult
from ..models import DriverValidationError, ContainerHandle, TransferCommand
from ..interactors import TransferConstraintMethodInteractor
from ..faults import OpentronsFaultMappingInteractor
from ..motion.interactors import MotionConfigurationInteractor
from ..motion.models import LabwareMoveIntent
from ..centrifuge.models import CycleSettings
from ..environment import ReferenceEnvironmentInteractor
from ..mixing import PipetteMixInteractor
from ..translator import plan_strokes
from .mapping import ProgramOperationMapper


class StandaloneDriver:
    def __init__(self, state, config, session, *, preflight=False):
        self.state, self.config, self.session = state, config, session
        self.preflight = preflight
        self.mapper = ProgramOperationMapper(config, state)
        self.environment = ReferenceEnvironmentInteractor(config)
        self.mixer = PipetteMixInteractor(session.bindings)
        self.positions = MotionConfigurationInteractor().initial_locations(config)
        self.tips_required = 0
        self.prepared = {}
        self.receipts = []
        self.state.artifacts['device_execution'] = {'mode': 'preflight' if preflight else 'simulation',
            'configuration_revision': config.fingerprint(), 'receipts': self.receipts}

    def projected_positions(self, step_id):
        positions = dict(self.positions)
        for route in self.config.labware_moves.get(step_id, []):
            destination = route['destination']
            if destination in positions.values():
                raise DriverValidationError('DRV_OT_MOTION_LOCATION', f'Movement destination {destination} is occupied')
            positions[route['labware']] = destination
        return positions

    def pipette_commands(self, operation):
        """Plan stroke volumes for method checks without moving or binding labware."""
        request = operation.request
        addresses = {b.reference.container_id: b.address for b in operation.bindings}
        if operation.kind == 'mix':
            target = request.target.id
            aliquots, mode = [(target, request.volume_uL)], 'single'
        elif operation.kind == 'transfer':
            target = request.target.container_id
            aliquots = [(a.source.container_id, a.volume_uL) for a in request.aliquots]
            mode = self.config.transfer_policy.stroke_mode
        else:
            raise DriverValidationError('DRV_OT_OP_UNSUPPORTED', 'Pipette commands require transfer or mixing')
        pipette = self.session.backend.pipette
        commands = []
        for index, (source, volume) in enumerate(aliquots):
            strokes = plan_strokes(volume, pipette.min_volume, pipette.max_volume, mode)
            aliquot_id = f'{request.step_id}/aliquot/{index}'
            for stroke, amount in enumerate(strokes):
                commands.append(TransferCommand(request.step_id, ContainerHandle(source), ContainerHandle(target),
                    addresses[source], addresses[target], amount, aliquot_id, f'{aliquot_id}/stroke/{stroke}'))
        return tuple(commands)

    def validate(self, step, operation, positions):
        backend = self.session.backend
        result = self.mapper.compute.apply_step(step, self.mapper.material)
        addresses = {b.reference.container_id: b.address for b in operation.bindings}
        for identity, address in addresses.items():
            container = result.material_state['containers'][identity]
            if container.get('metadata', {}).get('kind') not in {'well', 'tube'}:
                raise DriverValidationError('DRV_OT_LABWARE_UNSUPPORTED', 'Only well/tube allocations are mapped')
            if container.get('volume_uL', 0) > backend.well(address).max_volume:
                raise DriverValidationError('DRV_OT_CAPACITY', 'Resolved liquid exceeds physical well capacity')
            capacity = container.get('metadata', {}).get('capacity_uL')
            if capacity is None or capacity > backend.well(address).max_volume:
                raise DriverValidationError('DRV_OT_CAPACITY', 'An explicit capacity within the physical well limit is required')
        labwares = {addresses[t].labware for t in operation.targets}
        tips = 0
        if operation.environment is not None:
            self.environment.select(operation.environment, labwares, positions,
                transfer=operation.kind in {'mix', 'transfer'})
            # One plate shares its environment; all bound samples must be declared.
            if operation.kind == 'hold' and {b.reference.container_id for b in operation.bindings if b.address.labware in labwares} != set(operation.targets):
                raise DriverValidationError('DRV_OT_ENVIRONMENT_TARGETS', 'Declare all affected samples on the incubated plate')
        if operation.kind in {'mix', 'transfer'}:
            if any(positions[lw].startswith('module:') for lw in labwares):
                raise DriverValidationError('DRV_OT_TRANSFER_LOCATION', 'Move the plate to an accessible deck location before pipetting')
            if operation.kind == 'mix':
                request = operation.request
                if not 0 < request.volume_uL <= request.available_uL:
                    raise DriverValidationError('DRV_OT_MIX_VOLUME', 'Mixing exceeds available liquid')
            commands = self.pipette_commands(operation)
            TransferConstraintMethodInteractor(self.session.bindings).resolve(operation.request.requirements, commands)
            tips = len(commands)
            if self.tips_required + tips > sum(len(r.wells()) for r in backend.pipette.tip_racks):
                raise DriverValidationError('DRV_OT_TIPS_INSUFFICIENT', 'The complete program exceeds configured tip capacity')
        if operation.kind == 'centrifuge':
            if len(labwares) != 1:
                raise DriverValidationError('DRV_OT_CENTRIFUGE_BINDING', 'One sample plate is required')
            labware = next(iter(labwares))
            location = positions[labware]
            dock = self.config.reference_devices.get(location.removeprefix('module:'))
            if not location.startswith('module:') or dock is None or labware not in dock.sample_masses_grams:
                raise DriverValidationError('DRV_OT_CENTRIFUGE_LOAD', 'Move the sample plate to a compatible centrifuge')
            if {b.reference.container_id for b in operation.bindings if b.address.labware == labware} != set(operation.targets):
                raise DriverValidationError('DRV_OT_CENTRIFUGE_BINDING', 'Declare all samples on the centrifuged plate')
            p, r = dock.profile, operation.request
            p.validate(CycleSettings(labware, p.convert_drive(r.drive_value, r.drive_unit), p.drive_unit,
                                     r.duration_seconds, r.temperature_celsius))
            if abs(dock.sample_masses_grams[labware] - dock.counterweight.mass_grams) > p.max_imbalance_grams:
                raise DriverValidationError('DRV_OT_CENTRIFUGE_IMBALANCE', 'Configured rotor loads are unbalanced')
        return tips

    def check(self, step):
        try:
            if not self.preflight:
                self.session.check_ready()
            operation = self.mapper.map(step)
            positions = self.projected_positions(step.step_id)
            tips = self.validate(step, operation, positions)
            self.prepared[step.step_id] = (step, operation, positions, tips)
            return DriverCapabilityResult(True, 'DRV_OT_SUPPORTED')
        except (ValueError, TypeError, KeyError) as error:
            return DriverCapabilityResult(False, getattr(error, 'code', 'DRV_OT_MAPPING_INVALID'),
                payload={'message': str(error), 'step_id': step.step_id, 'phase': 'preflight' if self.preflight else 'check', 'retry_safe': self.preflight})

    def execute_routes(self, step_id):
        for index, route in enumerate(self.config.labware_moves.get(step_id, [])):
            motion = self.session.motion
            intent = LabwareMoveIntent(f'{step_id}/move/{index}', route['labware'],
                motion.positions()[route['labware']], route['destination'], motion.state['revision'])
            feedback = self.session.execute_labware_move(self.session.prepare_labware_move(intent))
            self.receipts.append({'step_id': step_id, 'outcome': feedback.outcome, **feedback.payload})
            if feedback.outcome != 'completed':
                return DriverResult(False, 'DRV_OT_MOTION_FAILED', {**feedback.payload, 'error_severity': 'fatal'})
        return None

    def execute_operation(self, step, operation):
        addresses = {b.reference.container_id: b.address for b in operation.bindings}
        payload = {'operation_kind': operation.kind, 'step_id': step.step_id, 'mode': 'simulation'}
        if operation.environment is not None:
            payload['environment'] = self.environment.execute(step.step_id, operation.environment,
                {addresses[t].labware for t in operation.targets}, self.positions, operation.targets,
                transfer=operation.kind in {'mix', 'transfer'})
        if operation.kind == 'transfer':
            if operation.producer_step_id:
                prepared = self.session.prepare_centrifuge_supernatant(operation.request, operation.bindings, operation.producer_step_id)
            else:
                prepared = self.session.prepare_transfer(operation.request, operation.bindings)
            feedback = self.session.execute_transfer(prepared)
            return DriverResult(feedback.outcome == 'completed', 'DRV_OT_SIMULATED' if feedback.outcome == 'completed' else 'DRV_OT_TRANSFER_FAILED',
                                {**payload, **feedback.payload, 'outcome': feedback.outcome, 'error_severity': 'fatal'})
        if operation.kind == 'mix':
            self.session.bind_references(operation.bindings)
            receipt = self.mixer.execute(self.mixer.prepare(operation.request))
            return DriverResult(receipt.ok, receipt.code, {**receipt.to_payload(), **payload,
                'operation_kind': 'pipette_mix', 'mixed_volume_uL': operation.request.volume_uL,
                'material_net_transfer_uL': 0, 'error_severity': 'fatal'})
        if operation.kind == 'centrifuge':
            prepared = self.session.prepare_centrifuge(operation.request, operation.bindings)
            feedback = self.session.execute_centrifuge(prepared)
            return DriverResult(feedback.outcome == 'completed', 'DRV_OT_SIMULATED' if feedback.outcome == 'completed' else 'DRV_OT_CENTRIFUGE_FAILED',
                                {**payload, **feedback.payload, 'outcome': feedback.outcome, 'error_severity': 'fatal'})
        if operation.kind == 'setup':
            payload['evidence_scope'] = 'declared_preloaded_materials'
        return DriverResult(True, 'DRV_OT_SIMULATED', payload)

    def execute(self, step):
        prepared = self.prepared.pop(step.step_id, None)
        if prepared is None or prepared[0] != step:
            return DriverResult(False, 'DRV_OT_PREPARATION', {'message': 'Check must precede execution', 'error_severity': 'fatal'})
        _, operation, positions, tips = prepared
        if self.preflight:
            self.positions, self.tips_required = positions, self.tips_required + tips
            return DriverResult(True, 'DRV_OT_PREFLIGHT', {'operation_kind': operation.kind, 'mode': 'preflight'})
        try:
            route_failure = self.execute_routes(step.step_id)
            if route_failure is not None:
                return route_failure
            self.positions = positions
            self.tips_required += tips
            result = self.execute_operation(step, operation)
        except Exception as error:
            result = DriverResult(False, getattr(error, 'code', 'DRV_OT_EXECUTION_UNKNOWN'),
                {'message': str(error), 'exception_type': type(error).__name__, 'step_id': step.step_id,
                 'operation_kind': operation.kind, 'error_severity': 'fatal', 'retry_safe': False,
                 'faults': [OpentronsFaultMappingInteractor(schema_version=2).completion_fault(
                     error, step.step_id, operation.kind, {})]})
        if not result.ok:
            self.session.backend.poisoned = True
        self.receipts.append(deepcopy(result.payload))
        return result
