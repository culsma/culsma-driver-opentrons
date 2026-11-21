"""Official SDK execution: one shared eight-channel wave, not eight scalar calls."""
from dataclasses import replace

import pytest
from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.models import DriverValidationError, SimulatorBinding, WellAddress
from culsma_opentrons.operations import TransferIntent, TransferAliquot, ResolvedContainerRef
from culsma_opentrons.plugin import SimulatorSessionFactory


class MultichannelFixture:
    def raw(self, pipette="p20_multi_gen2"):
        return {"robot_type": "OT-2", "api_level": "2.20", "pipette": pipette, "mount": "right",
                "tiprack": {"load_name": "opentrons_96_tiprack_20ul" if pipette.startswith("p20") else "opentrons_96_tiprack_300ul", "slot": "2"},
                "labware": {"source": {"load_name": "nest_12_reservoir_15ml", "slot": "1"},
                            "target": {"load_name": "corning_96_wellplate_360ul_flat", "slot": "3"}},
                "bindings": {"source": {"labware": "source", "well": "A1"},
                             **{f"t{i}": {"labware": "target", "well": f"{row}1"} for i, row in enumerate("ABCDEFGH")}}}

    def build(self, tmp_path, pipette="p20_multi_gen2", raw=None):
        config = DeckConfiguration.from_dict(raw or self.raw(pipette))
        session = SimulatorSessionFactory().create(config, tmp_path)
        bindings = tuple(SimulatorBinding(ResolvedContainerRef(k), v) for k, v in config.bindings.items())
        intents = tuple(TransferIntent(f"m{i}", ResolvedContainerRef(f"t{i}"),
                        (TransferAliquot(ResolvedContainerRef("source"), 20.0),)) for i in range(8))
        return session, bindings, intents


class TestMultichannel:
    @pytest.mark.parametrize("pipette", ["p20_multi_gen2", "p300_multi_gen2"])
    def test_real_eight_channel_wave(self, tmp_path, pipette, monkeypatch):
        raw = MultichannelFixture().raw(pipette)
        raw["mount"] = "left" if pipette.startswith("p300") else "right"
        raw["transfer_policy"] = {"aspirate_flow_rate_uL_s": 3, "dispense_flow_rate_uL_s": 4}
        session, bindings, intents = MultichannelFixture().build(tmp_path, pipette, raw)
        try:
            prepared = session.prepare_group("g", intents, bindings)
            before = session.backend.available_tips()
            seen = []
            aspirate, dispense = session.backend.pipette.aspirate, session.backend.pipette.dispense
            def observe_aspirate(volume, location):
                seen.append(("aspirate", volume, location.well_name, session.backend.pipette.flow_rate.aspirate))
                return aspirate(volume, location)
            def observe_dispense(volume, location):
                seen.append(("dispense", volume, location.well_name, session.backend.pipette.flow_rate.dispense))
                return dispense(volume, location)
            monkeypatch.setattr(session.backend.pipette, "aspirate", observe_aspirate)
            monkeypatch.setattr(session.backend.pipette, "dispense", observe_dispense)
            feedback = session.execute_group(prepared)
            assert feedback.outcome == "completed"
            assert session.backend.pipette.channels == 8
            assert len(session.backend.receipts) == 1
            receipt = session.backend.receipts[0]
            assert len(receipt.operation.commands) == 1 and len(receipt.action_results) == 4
            assert receipt.operation.commands[0].volume_uL == 20
            assert feedback.payload["total_source_volume_uL"] == 160
            assert feedback.consumed_resources == {"tips": 8}
            assert session.backend.available_tips() == before - 8
            assert [d["target_address"]["well"] for d in feedback.payload["member_descriptions"]] == [f"{r}1" for r in "ABCDEFGH"]
            assert session.describe_session().transfer_channels == 8
            assert feedback.payload["evidence_scope"] == "shared_eight_channel_wave"
            assert seen == [("aspirate", 20, "A1", 3), ("dispense", 20, "A1", 4)]
        finally:
            session.close()

    @pytest.mark.parametrize("case", ["missing", "reverse", "horizontal", "duplicate"])
    def test_invalid_column_rejected_before_actions(self, tmp_path, case):
        fixture = MultichannelFixture()
        raw = fixture.raw()
        if case == "horizontal":
            for i in range(8): raw["bindings"][f"t{i}"]["well"] = f"A{i+1}"
        session, bindings, intents = fixture.build(tmp_path, raw=raw)
        try:
            if case == "missing": intents = intents[:-1]
            if case == "reverse": intents = tuple(reversed(intents))
            if case == "duplicate": intents = intents[:-1] + intents[:1]
            with pytest.raises(DriverValidationError): session.prepare_group("g", intents, bindings)
            assert not session.backend.protocol.commands() and session.backend.available_tips() == 96
        finally:
            session.close()

    @pytest.mark.parametrize("case", ["tube", "unequal", "too-large", "multiple-sources", "split-policy"])
    def test_source_and_volume_rejected_before_actions(self, tmp_path, case):
        fixture = MultichannelFixture()
        raw = fixture.raw()
        if case == "tube": raw["labware"]["source"]["load_name"] = "opentrons_24_tuberack_eppendorf_2ml_safelock_snapcap"
        if case == "split-policy": raw["transfer_policy"] = {"stroke_mode": "balanced_split"}
        session, bindings, intents = fixture.build(tmp_path, raw=raw)
        try:
            if case in {"unequal", "too-large"}:
                i = 1 if case == "unequal" else 0
                if case == "too-large": intents = tuple(replace(m, aliquots=(replace(m.aliquots[0], volume_uL=21),)) for m in intents)
                else: intents = intents[:i] + (replace(intents[i], aliquots=(replace(intents[i].aliquots[0], volume_uL=10),)),) + intents[i+1:]
            if case == "multiple-sources": intents = (replace(intents[0], aliquots=intents[0].aliquots * 2),) + intents[1:]
            with pytest.raises(DriverValidationError): session.prepare_group("g", intents, bindings)
            assert not session.backend.protocol.commands()
        finally:
            session.close()

    def test_preparation_is_one_shot_and_versioned(self, tmp_path):
        session, bindings, intents = MultichannelFixture().build(tmp_path)
        try:
            prepared = session.prepare_group("g", intents, bindings)
            with pytest.raises(DriverValidationError): session.execute_group(replace(prepared))
            session.bindings.config = replace(session.bindings.config, mount="left")
            with pytest.raises(DriverValidationError, match="changed"): session.execute_group(prepared)
            with pytest.raises(DriverValidationError, match="consumed"): session.execute_group(prepared)
            assert not session.backend.protocol.commands()
        finally:
            session.close()

    def test_tip_columns_and_restore(self, tmp_path):
        session, bindings, intents = MultichannelFixture().build(tmp_path)
        try:
            with pytest.raises(DriverValidationError): session.restore_consumables({"tips": 1})
            session.restore_consumables({"tips": 88})
            assert session.backend.available_tips() == 8
            feedback = session.execute_group(session.prepare_group("g", intents, bindings))
            assert feedback.outcome == "completed" and session.backend.available_tips() == 0
            with pytest.raises(DriverValidationError): session.prepare_group("next", intents, bindings)
        finally:
            session.close()

    def test_scalar_path_cannot_move_extra_wells(self, tmp_path):
        session, bindings, intents = MultichannelFixture().build(tmp_path)
        try:
            with pytest.raises(DriverValidationError, match="complete-column"):
                session.prepare_transfer(intents[0], bindings)
            assert not session.backend.protocol.commands()
        finally:
            session.close()
