"""Interpret vendor evidence without language commits, persistence or OS imports."""

from copy import deepcopy
from .models import TransferFeedback


class OpentronsReceiptNormalizer:
    actions = ("pick_up_tip", "aspirate", "dispense", "drop_tip")

    def interpret(self, description, payload, step_id, configuration_revision):
        raw = deepcopy(payload) if isinstance(payload, dict) else {}
        transfers = description["transfers"]
        observations = raw.get("action_results", [])
        if not isinstance(observations, list) or not all(
            isinstance(a, dict) for a in observations
        ):
            observations = []
        identity_matches = (
            raw.get("schema") == "opentrons_transfer_receipt_v1"
            and raw.get("mode") == "simulation"
            and raw.get("step_id") == step_id
            and raw.get("config_sha256") == configuration_revision
        )
        expected = [
            (c["stroke_id"], action, f'{c["stroke_id"]}/{action}')
            for c in transfers
            for action in self.actions
        ]
        actual = [
            (a.get("stroke_id"), a.get("action"), a.get("action_id"))
            for a in observations
        ]
        module_expected = description.get("module_preconditions", [])
        module_actual = raw.get("module_action_results", [])
        module_matches = (isinstance(module_actual, list)
            and all(isinstance(item, dict) for item in module_actual)
            and [{"module_id": item.get("module_id"), "action": item.get("action")} for item in module_actual] == module_expected
            and all(item.get("status") == "returned" for item in module_actual))
        expected_methods = description.get('constraint_resolution', [])
        method_matches = raw.get('constraint_resolution', []) == expected_methods
        for method in expected_methods:
            if method['requirement'] == 'high_precision':
                parameters = dict(method['parameters'])
                method_matches = method_matches and all(raw.get('instrument', {}).get(key) == parameters[key]
                    for key in ('aspirate_flow_rate_uL_s','dispense_flow_rate_uL_s'))
        complete = (
            method_matches and
            module_matches and
            identity_matches
            and bool(expected)
            and raw.get("phase") == "simulated"
            and raw.get("operation_ok") is True
            and raw.get("transfers") == transfers
            and actual == expected
            and all(a.get("status") == "returned" for a in observations)
        )
        rejected = (
            identity_matches
            and raw.get("phase") == "rejected"
            and raw.get("operation_ok") is False
            and raw.get("action_results") == []
            and module_actual == []
        )
        outcome = "completed" if complete else "rejected" if rejected else "unknown"
        return TransferFeedback(
            outcome,
            raw.get("mode", "unknown"),
            raw.get("step_id", ""),
            raw.get("config_sha256", ""),
            {
                "tips": self.channel_count(raw) * sum(
                    a.get("action") == "pick_up_tip" and a.get("status") == "returned"
                    for a in observations
                )
            },
            raw,
        )

    def channel_count(self, payload):
        channels = payload.get("instrument", {}).get("channels", 1)
        return channels if type(channels) is int and channels in {1, 8} else 0
