"""Plugin-owned native diagnostic conversion; no recovery authority or OS imports."""
import json
from copy import deepcopy
from importlib.resources import files


class OpentronsFaultMappingInteractor:
    def __init__(self, mode=None, schema_version=1):
        if schema_version not in (1,2):
            raise ValueError('Unsupported fault schema')
        self.mode, self.schema_version = mode, schema_version
        self.catalog = json.loads(files('culsma_opentrons').joinpath('fault_catalog.json').read_text())

    def with_fault_schema(self, version):
        return type(self)(self.mode, version)

    def fault(self, code, operation_id, state, details=None, context=None):
        entry = self.catalog.get(code, {'code':'DEVICE_ERROR','reason':'culsma.device.unclassified'})
        source = 'simulation'
        value = {'code':entry['code'],'source':source,'execution_state':state,
                 'operation_id':operation_id,'location':None,'native_details':{'code':code, **(details or {})}}
        if self.schema_version == 2:
            value.update(schema='culsma_device_fault_v2', reason=entry['reason'],
                basis='declared_configuration' if state == 'not_started' else 'model_state', context=context or {}, stop_observation={'state':'unknown'})
        return value

    def exception_identity(self, error):
        """Read SDK exception causes and typed ErrorOccurrence trees."""
        seen, pending = set(), [error]
        identity = getattr(error,'code',type(error).__name__)
        if not isinstance(identity,str):
            identity = type(error).__name__
        while pending:
            current = pending.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            name = getattr(current,'errorType',type(current).__name__)
            if name in self.catalog:
                identity = name
            pending.extend(getattr(current,'wrappedErrors',[]) or [])
            pending.extend([getattr(current,'original_error',None),
                            getattr(current,'__cause__',None),getattr(current,'__context__',None)])
        return identity

    def preparation(self, error, operation_id):
        return [self.fault(self.exception_identity(error),operation_id,'not_started',
            {'exception_type':type(error).__name__})]

    def completion_fault(self, error, operation_id, operation_kind, context):
        """Record an unconfirmed outcome and keep the underlying exception separate."""
        return self.fault(
            "DRV_OT_COMPLETION_UNCONFIRMED", operation_id, "unknown",
            {
                "operation_kind": operation_kind,
                "exception_type": type(error).__name__,
                "exception_code": self.exception_identity(error),
                "message": str(error),
            },
            context,
        )

    def normalize(self, payload):
        if payload.get('outcome') == 'completed':
            return []
        raw = payload.get('payload',{})
        if raw.get("faults"):
            faults = deepcopy(raw["faults"])
            if self.schema_version == 1:
                for fault in faults:
                    for key in ("schema", "reason", "basis", "context", "stop_observation"):
                        fault.pop(key, None)
            return faults
        code = raw.get('exception_type') or raw.get('code') or 'DRV_OT_DEVICE_ERROR'
        return [self.fault(code,payload.get('step_id'),
            'not_started' if payload.get('outcome') == 'rejected' else 'unknown')]
