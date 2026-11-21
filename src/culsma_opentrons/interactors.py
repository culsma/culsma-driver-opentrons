"""Conservative full-column planning and shared-wave evidence interpretation."""
from dataclasses import asdict, replace
from collections.abc import Mapping
from .contracts import TransferConstraintMethodPort
from uuid import uuid4

from .models import (DriverValidationError, PreparedMultichannelTransfer,
                     PreparedMultichannelBatch, TransferCommand, TransferOperation, TransferFeedback)
from .translator import plan_strokes


class MultichannelTransferInteractor:
    target_profiles = frozenset({"corning_96_wellplate_360ul_flat"})
    source_profiles = frozenset({"nest_12_reservoir_15ml"})

    def __init__(self, backend, bindings, normalizer):
        self.backend, self.bindings, self.normalizer = backend, bindings, normalizer
        self.preparations = {}

    def plan(self, group_id, requests):
        self.backend.check_ready()
        config = self.bindings.config
        if self.backend.pipette.channels != 8 or config.transfer_policy.stroke_mode != "single":
            raise DriverValidationError("DRV_OT_MULTICHANNEL_PROFILE", "Eight channels and single stroke are required")
        if not group_id or len(requests) != 8 or len({r.step_id for r in requests}) != 8:
            raise DriverValidationError("DRV_OT_MULTICHANNEL_COVERAGE", "Exactly eight distinct members are required")
        if any(len(r.aliquots) != 1 for r in requests):
            raise DriverValidationError("DRV_OT_MULTICHANNEL_SOURCE", "One common source aliquot per member is required")
        first = requests[0]
        source = first.aliquots[0]
        target_name = first.target_address.labware
        targets = [r.target_address for r in requests]
        if (config.labware[target_name]["load_name"] not in self.target_profiles
                or any(a.labware != target_name for a in targets)
                or targets[0].well not in {f"A{i}" for i in range(1, 13)}
                or [a.well for a in targets] != [f"{row}{targets[0].well[1:]}" for row in "ABCDEFGH"]
                or len({r.target for r in requests}) != 8):
            raise DriverValidationError("DRV_OT_MULTICHANNEL_ALIGNMENT", "Targets must be one complete ordered A-H column")
        if (config.labware[source.source_address.labware]["load_name"] not in self.source_profiles
                or any(a.source != source.source or a.source_address != source.source_address
                       for r in requests for a in r.aliquots)
                or source.source_address in targets or source.source in {r.target for r in requests}):
            raise DriverValidationError("DRV_OT_MULTICHANNEL_SOURCE", "A common compatible reservoir well is required")
        if any(a.volume_uL != source.volume_uL for r in requests for a in r.aliquots):
            raise DriverValidationError("DRV_OT_MULTICHANNEL_VOLUME", "Coupled channels require equal volumes")
        # Validate numeric domain with the same scalar volume planner.
        from math import isfinite
        volume = source.volume_uL
        if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not isfinite(volume) or volume <= 0:
            raise DriverValidationError("DRV_OT_VOLUME_RANGE", "Volume must be positive and finite")
        plan_strokes(volume, self.backend.pipette.min_volume, self.backend.pipette.max_volume, "single")
        for request in requests:
            for handle, address in [(request.target, request.target_address),
                                    (request.aliquots[0].source, request.aliquots[0].source_address)]:
                if self.bindings.addresses.get(handle) != address:
                    raise DriverValidationError("DRV_OT_BINDING_STALE", "Group reference binding changed")
                self.backend.well(address)
        if self.backend.pipette.has_tip:
            raise DriverValidationError("DRV_OT_TIP_ATTACHED", "Fresh complete tip column is required")
        if self.backend.available_tips() < 8:
            raise DriverValidationError("DRV_OT_TIPS_INSUFFICIENT", "Need eight fresh tips")
        command = TransferCommand(group_id, source.source, first.target, source.source_address,
                                  first.target_address, volume, f"{group_id}/aliquot/0", f"{group_id}/wave/0")
        return TransferOperation(group_id, (command,), config.transfer_policy, first.span)

    def prepare(self, group_id, intents, requests):
        operation = self.plan(group_id, requests)
        prepared = PreparedMultichannelTransfer(str(uuid4()), group_id, tuple(intents), tuple(requests),
                                                operation, self.bindings.config.fingerprint())
        self.preparations[prepared.preparation_id] = prepared
        return prepared

    def describe(self, prepared):
        return {
            "resource_requirements": {"tips": 8},
            "member_step_ids": [r.step_id for r in prepared.requests],
            "member_descriptions": [{"resource_requirements": {}, "channel": channel,
                                      "source_address": asdict(r.aliquots[0].source_address),
                                      "target_address": asdict(r.target_address),
                                      "volume_uL": r.aliquots[0].volume_uL}
                                     for channel, r in enumerate(prepared.requests)],
            "wave_count": 1, "channels": 8,
        }

    def execute(self, prepared):
        if not isinstance(prepared, PreparedMultichannelTransfer):
            raise DriverValidationError("DRV_OT_PREPARATION_INVALID", "Prepared group is required")
        issued = self.preparations.get(prepared.preparation_id)
        if issued is not prepared:
            raise DriverValidationError("DRV_OT_PREPARATION_INVALID", "Preparation is foreign, modified or consumed")
        del self.preparations[prepared.preparation_id]
        if (prepared.configuration_revision != self.bindings.config.fingerprint()
                or self.plan(prepared.group_id, prepared.requests) != prepared.operation):
            raise DriverValidationError("DRV_OT_PREPARATION_STALE", "Group preparation changed")
        receipt = self.backend.run_prepared(prepared.operation)
        feedback = self.normalizer.interpret(
            {"transfers": [asdict(c) for c in prepared.operation.commands]},
            receipt.to_payload(), prepared.group_id, prepared.configuration_revision)
        return TransferFeedback(feedback.outcome, feedback.mode, feedback.step_id,
                                feedback.configuration_revision, feedback.consumed_resources,
                                {**feedback.payload, **self.describe(prepared),
                                 "evidence_scope": "shared_eight_channel_wave",
                                 "total_source_volume_uL": prepared.operation.commands[0].volume_uL * 8})


class MultichannelBatchInteractor:
    """Declare physical order; semantic equivalence admission belongs to the host."""

    def __init__(self, columns):
        self.columns = columns
        self.preparations = {}
        self.next_wave = {}

    def prepare(self, group_id, intents, requests):
        backend = self.columns.backend
        backend.check_ready()
        if len(requests) != 96 or len({r.step_id for r in requests}) != 96:
            raise DriverValidationError("DRV_OT_MULTICHANNEL_COVERAGE", "A whole plate needs 96 distinct members")
        first = requests[0]
        expected = [f"{row}{col}" for row in "ABCDEFGH" for col in range(1, 13)]
        if (any(r.target_address.labware != first.target_address.labware for r in requests)
                or [r.target_address.well for r in requests] != expected
                or len({r.target for r in requests}) != 96):
            raise DriverValidationError("DRV_OT_MULTICHANNEL_ALIGNMENT", "Whole plate input must retain row-major A1-H12 order")
        if (len(first.aliquots) != 1 or any(len(r.aliquots) != 1 or r.aliquots != first.aliquots for r in requests)):
            raise DriverValidationError("DRV_OT_MULTICHANNEL_SOURCE", "Whole plate requires one common source and equal volume")
        if backend.available_tips() < 96:
            raise DriverValidationError("DRV_OT_TIPS_INSUFFICIENT", "Whole plate needs 96 fresh tips")
        waves = []
        try:
            for column in range(12):
                ordinals = tuple(row * 12 + column for row in range(8))
                wave = self.columns.prepare(f"{group_id}/wave/{column}", tuple(intents[i] for i in ordinals),
                                            tuple(requests[i] for i in ordinals))
                waves.append(wave)
        except BaseException:
            for wave in waves:
                self.columns.preparations.pop(wave.preparation_id, None)
            raise
        batch = PreparedMultichannelBatch(str(uuid4()), group_id, tuple(requests), tuple(waves),
                                         self.columns.bindings.config.fingerprint())
        self.preparations[batch.preparation_id] = batch
        self.next_wave[batch.preparation_id] = 0
        return batch

    def describe(self, batch):
        ordinal_by_id = {r.step_id: i for i, r in enumerate(batch.requests)}
        waves, members = [], {}
        for wave in batch.waves:
            description = self.columns.describe(wave)
            ids = description["member_step_ids"]
            waves.append({"step_id": wave.group_id, "member_step_ids": ids,
                          "semantic_ordinals": [ordinal_by_id[i] for i in ids],
                          "resource_requirements": {"tips": 8}})
            members.update(zip(ids, description["member_descriptions"]))
        return {"resource_requirements": {"tips": 96}, "channels": 8, "wave_count": 12,
                "member_step_ids": [r.step_id for r in batch.requests],
                "member_descriptions": [members[r.step_id] for r in batch.requests],
                "waves": waves, "reordering_required": True}

    def execute_wave(self, batch, wave):
        if not isinstance(batch, PreparedMultichannelBatch) or self.preparations.get(batch.preparation_id) is not batch:
            raise DriverValidationError("DRV_OT_PREPARATION_INVALID", "Foreign or modified batch")
        index = self.next_wave.get(batch.preparation_id)
        if index is None or index >= len(batch.waves) or batch.waves[index] is not wave:
            raise DriverValidationError("DRV_OT_PREPARATION_INVALID", "Wave already consumed, blocked or out of order")
        # Any attempted wave blocks retries and later waves unless its receipt completes.
        self.next_wave[batch.preparation_id] = None
        if self.columns.bindings.config.fingerprint() != batch.configuration_revision:
            raise DriverValidationError("DRV_OT_PREPARATION_STALE", "Batch configuration changed")
        feedback = self.columns.execute(wave)
        if feedback.outcome == "completed":
            self.next_wave[batch.preparation_id] = index + 1
        return feedback


class TransferConstraintMethodInteractor:
    """Resolve qualitative requirements through injected device method implementations."""
    def __init__(self, bindings, methods: Mapping[str, TransferConstraintMethodPort] | None = None):
        self.bindings = bindings
        self.methods = dict(methods) if methods is not None else {
            'low_carryover': LowCarryoverMethodInteractor(bindings),
            'high_precision': PrecisionTransferMethodInteractor(bindings),
            'aseptic': AsepticTransferMethodInteractor(bindings),
            'spread': FixedPositionSpreadFallbackInteractor(bindings),
        }

    def capabilities(self):
        return tuple(name for name, method in self.methods.items() if method.available())

    def resolve(self, requirements, commands):
        policy = self.bindings.config.transfer_policy
        if any(req not in self.capabilities() for req in requirements):
            raise DriverValidationError('DRV_OT_CONSTRAINT_METHOD', 'No configured method for requested transfer constraint')
        resolutions = []
        for requirement in dict.fromkeys(requirements):
            policy, resolution = self.methods[requirement].apply(policy, commands)
            if resolution.requirement != requirement:
                raise DriverValidationError('DRV_OT_CONSTRAINT_METHOD', 'Method resolution does not match requirement')
            resolutions.append(resolution)
        return policy, tuple(resolutions)


class LowCarryoverMethodInteractor:
    def __init__(self, bindings):
        self.bindings = bindings

    def available(self):
        return self.bindings.backend.pipette.channels == 1 and self.bindings.config.transfer_policy.tip_policy == 'fresh_per_stroke'

    def apply(self, policy, commands):
        from .models import AppliedTransferConstraint
        if not self.available() or policy.tip_policy != 'fresh_per_stroke':
            raise DriverValidationError('DRV_OT_CONSTRAINT_METHOD', 'Low carryover requires a fresh tip for every aspiration')
        return policy, AppliedTransferConstraint('low_carryover', 'fresh-tip-per-stroke', '1', (('tip_policy', policy.tip_policy),))


class AsepticTransferMethodInteractor(LowCarryoverMethodInteractor):
    def apply(self, policy, commands):
        policy, resolution = super().apply(policy, commands)
        return policy, replace(resolution, requirement='aseptic', status='partial',
            limitation='Fresh tips reduce carryover. Sterility is unverified.')


class FixedPositionSpreadFallbackInteractor:
    def __init__(self, bindings):
        self.bindings = bindings

    def available(self):
        return self.bindings.backend.pipette.channels == 1

    def apply(self, policy, commands):
        from .models import AppliedTransferConstraint
        return policy, AppliedTransferConstraint(
            'spread', 'dispense-at-configured-location', '1', (),
            status='unmet', limitation='Liquid is dispensed at the configured location. No surface spreading is performed.',
        )


class PrecisionTransferMethodInteractor:
    def __init__(self, bindings):
        self.bindings = bindings

    def available(self):
        method = self.bindings.config.transfer_policy.precision_method
        if self.bindings.backend.pipette.channels != 1 or method is None:
            return False
        self.validate_method(method)
        return True

    def validate_method(self, method):
        pipette = self.bindings.backend.pipette
        if (method.pipette != self.bindings.config.pipette
                or not pipette.min_volume <= method.minimum_volume_uL <= method.maximum_volume_uL <= pipette.max_volume):
            raise DriverValidationError('DRV_OT_CONSTRAINT_METHOD', 'Precision method does not match configured pipette and operating range')

    def apply(self, policy, commands):
        from dataclasses import replace
        from .models import AppliedTransferConstraint
        if not self.available():
            raise DriverValidationError('DRV_OT_CONSTRAINT_METHOD', 'Precision method is not configured')
        method = self.bindings.config.transfer_policy.precision_method
        if any(not method.minimum_volume_uL <= c.volume_uL <= method.maximum_volume_uL for c in commands):
            raise DriverValidationError('DRV_OT_CONSTRAINT_METHOD', 'Stroke is outside the configured precision method range')
        policy = replace(policy, aspirate_flow_rate_uL_s=method.aspirate_flow_rate_uL_s,
            dispense_flow_rate_uL_s=method.dispense_flow_rate_uL_s)
        return policy, AppliedTransferConstraint('high_precision', method.method_id, method.revision,
            tuple((k, getattr(method,k)) for k in ('pipette','minimum_volume_uL','maximum_volume_uL',
                'aspirate_flow_rate_uL_s','dispense_flow_rate_uL_s')))
