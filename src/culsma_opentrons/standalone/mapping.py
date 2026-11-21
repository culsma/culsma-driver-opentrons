"""Translate resolved core steps; the core remains the material authority."""
from dataclasses import dataclass
from culsma.driver.framework.capability import CapabilityPolicy
from culsma.runtime.material.compute import MaterialCompute
from culsma.runtime.material.refs import MaterialRefResolver
from culsma.runtime.material.args import arg_call, arg_quantity, call_arg_value
from ..models import DriverValidationError, SimulatorBinding, ContainerHandle
from ..operations import ResolvedContainerRef, TransferIntent, TransferAliquot
from ..centrifuge.models import CentrifugeIntent, DriveUnit
from ..environment import EnvironmentRequest
from ..mixing import PipetteMixRequest


@dataclass(frozen=True)
class MappedOperation:
    kind: str
    request: object = None
    bindings: tuple = ()
    environment: EnvironmentRequest | None = None
    targets: tuple[str, ...] = ()
    producer_step_id: str | None = None


def quantity(value, factors):
    parsed = arg_quantity(value)
    if parsed is None or parsed['unit'] not in factors:
        raise DriverValidationError('DRV_OT_QUANTITY', 'Unsupported or unresolved device quantity')
    return parsed['value'] * factors[parsed['unit']]


def environment_request(step, *, hold=False):
    env = (step.gate or {}).get('env', {})
    allowed = {'thermal', 'co2', 'duration'} if hold else {'thermal', 'co2'}
    if set(env) - allowed:
        raise DriverValidationError('DRV_OT_ENVIRONMENT_UNSUPPORTED', 'Unconsumed environment conditions')
    if not env:
        return None
    return EnvironmentRequest(quantity(env.get('thermal'), {'C': 1}),
        quantity(env['duration'], {'s': 1, 'min': 60, 'h': 3600}) if hold and 'duration' in env else 0,
        quantity(env['co2'], {'%': 1}) if 'co2' in env else None)


def method_requirements(step):
    """Accept implemented pipetting methods without dropping other constraints."""
    constraint = (step.gate or {}).get('constraint', {})
    if (not isinstance(constraint, dict)
            or set(constraint) - {'requirements', 'options', 'requirement_types'}
            or constraint.get('requirement_types')
            or not isinstance(constraint.get('options', {}), dict)):
        raise DriverValidationError('DRV_OT_REQUIREMENT_UNSUPPORTED', 'Unsupported constraint structure or requirement type')
    requirements = constraint.get('requirements', [])
    if not isinstance(requirements, list) or any(not isinstance(value, str) or not value for value in requirements):
        raise DriverValidationError('DRV_OT_REQUIREMENT_UNSUPPORTED', 'Constraint requirements must be named values')
    supported = {'low_carryover', 'high_precision'} if step.op == 'Mutation' else set()
    capability = CapabilityPolicy(supported, set()).evaluate(step)
    if not capability.ok:
        unsupported = (*capability.unsupported_requirements, *capability.unsupported_constraint_options)
        raise DriverValidationError('DRV_OT_REQUIREMENT_UNSUPPORTED',
            f'Unsupported requirements or options for {step.op}: {", ".join(unsupported)}')
    return tuple(dict.fromkeys(requirements))


class ProgramOperationMapper:
    setup_ops = frozenset({'AllocContainer', 'DefineContent', 'LoadContent'})

    def __init__(self, config, state):
        self.config, self.state = config, state
        self.compute = MaterialCompute()

    @property
    def material(self):
        return self.state.artifacts.get('material_state', {'containers': {}})

    def bindings(self, material=None):
        result = []
        for identity, container in (self.material if material is None else material).get('containers', {}).items():
            allocation = container.get('metadata', {}).get('allocation_step_id')
            if allocation not in self.config.bindings:
                raise DriverValidationError('DRV_OT_BINDING_MISSING', f'No physical binding for {identity}')
            result.append(SimulatorBinding(ResolvedContainerRef(identity), self.config.bindings[allocation]))
        return tuple(result)

    def resolve(self, expression):
        identity = MaterialRefResolver.resolve_target_ref(self.material, expression)
        if identity is None:
            raise DriverValidationError('DRV_OT_BINDING_MISSING', 'Unresolved container reference')
        return identity

    def map(self, step):
        method_requirements(step)
        result = self.compute.apply_step(step, self.material)
        if not result.ok:
            raise DriverValidationError('DRV_OT_MATERIAL_INVALID', 'Core material resolution failed')
        if step.op in self.setup_ops:
            if (step.gate or {}).get('env'):
                raise DriverValidationError('DRV_OT_ENVIRONMENT_UNSUPPORTED', 'Environment on setup is unsupported')
            return MappedOperation('setup', bindings=self.bindings(result.material_state))
        bindings = self.bindings()
        if step.op == 'Mutation':
            if set(step.args) - {'target', 'sources', '_runtime_material_resolution'}:
                raise DriverValidationError('DRV_OT_ARGS_UNSUPPORTED', 'Unconsumed mutation arguments')
            return self.mutation(step, result.delta, bindings)
        if step.op == 'env_hold':
            targets = tuple(self.resolve(t) for t in (step.gate or {}).get('env_targets', []))
            request = environment_request(step, hold=True)
            if step.args or not targets or request is None or request.duration_seconds <= 0:
                raise DriverValidationError('DRV_OT_HOLD_UNSUPPORTED', 'A timed environment hold with targets is required')
            return MappedOperation('hold', bindings=bindings, environment=request, targets=targets)
        if step.op == 'sep':
            program = arg_call(step.args.get('program'))
            env = (step.gate or {}).get('env', {})
            if (program is None or program.get('name') != 'centrifuge_program'
                    or set(step.args) - {'sample', 'program', 'component_fates'}
                    or {a.get('name') for a in program.get('args', [])} != {'drive'}
                    or set(env) - {'thermal', 'duration'}):
                raise DriverValidationError('DRV_OT_SEPARATION_UNSUPPORTED', 'Unsupported separation request')
            drive = arg_quantity(call_arg_value(program, 'drive'))
            if drive is None or drive['unit'] not in {'g', 'rpm'}:
                raise DriverValidationError('DRV_OT_CENTRIFUGE_DRIVE', 'Centrifuge drive requires g or rpm')
            target = self.resolve(step.args['sample'])
            request = CentrifugeIntent(step.step_id, (ResolvedContainerRef(target),), drive['value'],
                DriveUnit(drive['unit']), quantity(env.get('duration'), {'s': 1, 'min': 60, 'h': 3600}),
                quantity(env['thermal'], {'C': 1}) if 'thermal' in env else None)
            return MappedOperation('centrifuge', request, bindings, targets=(target,))
        raise DriverValidationError('DRV_OT_OP_UNSUPPORTED', f'Unsupported standalone operation: {step.op}')

    def mutation(self, step, delta, bindings):
        requirements = method_requirements(step)
        sources = delta.get('sources', [])
        target = delta.get('target')
        if not sources or target is None:
            raise DriverValidationError('DRV_OT_TRANSFER_UNSUPPORTED', 'No resolved material transfer')
        environment = environment_request(step)
        if len(sources) == 1 and sources[0].get('mode') == 'quantified_self_noop':
            source = sources[0]
            address = next(b.address for b in bindings if b.reference.container_id == target)
            request = PipetteMixRequest(step.step_id, ContainerHandle(target), address,
                source['qty']['value'] * {'uL': 1, 'mL': 1000, 'L': 1e6}[source['qty']['unit']],
                self.material['containers'][target]['volume_uL'], step.span, requirements)
            return MappedOperation('mix', request, bindings, environment, (target,))
        aliquots, producer = [], None
        for source in sources:
            if source.get('mode') == 'quantified':
                transfer = source.get('transfer_delta', {})
                volume = transfer.get('moved_uL')
            elif source.get('op') == 'contents_state_transfer':
                content = self.material.get('contents_states', {}).get(source['source'], {})
                if (len(sources) != 1 or source.get('selected_slot') != '0' or not content.get('valid')
                        or content.get('program_kind') != 'centrifuge_program'
                        or content.get('slot_contract', {}).get('0') != 'supernatant'):
                    raise DriverValidationError('DRV_OT_SELECTION_UNSUPPORTED', 'Only centrifuge supernatant selection is supported')
                volume, producer = source.get('moved_uL'), content.get('step_id')
            else:
                raise DriverValidationError('DRV_OT_TRANSFER_UNSUPPORTED', 'Unsupported material transfer mode')
            if volume is None:
                raise DriverValidationError('DRV_OT_VOLUME_UNSUPPORTED', 'Transfer needs an explicit resolved liquid volume')
            aliquots.append(TransferAliquot(ResolvedContainerRef(source['source']), volume))
        request = TransferIntent(step.step_id, ResolvedContainerRef(target), tuple(aliquots), step.span,
            requirements=requirements)
        return MappedOperation('transfer', request, bindings, environment,
            tuple(dict.fromkeys([target, *(a.source.container_id for a in aliquots)])), producer)
