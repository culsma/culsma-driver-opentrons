"""Whole-plate geometry and one-shot ordered physical waves."""
from dataclasses import replace
import pytest
from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.models import DriverValidationError, SimulatorBinding
from culsma_opentrons.operations import TransferIntent, TransferAliquot, ResolvedContainerRef
from culsma_opentrons.plugin import SimulatorSessionFactory
from tests.test_multichannel import MultichannelFixture


class PlateBatchFixture:
    def build(self, tmp_path, pipette="p20_multi_gen2"):
        raw = MultichannelFixture().raw(pipette)
        wells = [f"{row}{col}" for row in "ABCDEFGH" for col in range(1, 13)]
        raw["bindings"] = {"source": {"labware": "source", "well": "A1"},
                           **{f"t{i}": {"labware": "target", "well": well} for i, well in enumerate(wells)}}
        config = DeckConfiguration.from_dict(raw)
        session = SimulatorSessionFactory().create(config, tmp_path)
        bindings = tuple(SimulatorBinding(ResolvedContainerRef(k), v) for k, v in config.bindings.items())
        intents = tuple(TransferIntent(f"m{i}", ResolvedContainerRef(f"t{i}"),
                         (TransferAliquot(ResolvedContainerRef("source"), 20.0),)) for i in range(96))
        return session, bindings, intents


class TestMultichannelPlate:
    @pytest.mark.parametrize("pipette", ["p20_multi_gen2", "p300_multi_gen2"])
    def test_plate_maps_row_major_members_to_twelve_column_waves(self, tmp_path, pipette, monkeypatch):
        session, bindings, intents = PlateBatchFixture().build(tmp_path, pipette)
        try:
            prepared = session.prepare_group("plate", intents, bindings)
            description = session.describe_group(prepared)
            assert description["resource_requirements"] == {"tips": 96}
            assert description["member_step_ids"] == [f"m{i}" for i in range(96)]
            assert len(description["waves"]) == 12
            assert description["waves"][0]["semantic_ordinals"] == list(range(0, 96, 12))
            assert description["waves"][-1]["semantic_ordinals"] == list(range(11, 96, 12))
            observed = []
            original = session.backend.pipette.dispense
            def observe(volume, location):
                observed.append((volume, location.well_name))
                return original(volume, location)
            monkeypatch.setattr(session.backend.pipette, "dispense", observe)
            feedback = [session.execute_group_wave(prepared, wave) for wave in session.wave_plans(prepared)]
            assert all(f.outcome == "completed" for f in feedback)
            assert observed == [(20, f"A{col}") for col in range(1, 13)]
            assert len(session.backend.receipts) == 12 and session.backend.available_tips() == 0
            assert sum(f.consumed_resources["tips"] for f in feedback) == 96
            assert all(len(r.operation.commands) == 1 and len(r.action_results) == 4 for r in session.backend.receipts)
        finally: session.close()

    @pytest.mark.parametrize("case", ["missing", "reversed", "unequal", "duplicate-id", "tip-budget"])
    def test_invalid_plate_rejected_before_actions(self, tmp_path, case):
        session, bindings, intents = PlateBatchFixture().build(tmp_path)
        try:
            if case == "missing": intents = intents[:-1]
            if case == "reversed": intents = tuple(reversed(intents))
            if case == "unequal": intents = (replace(intents[0], aliquots=(replace(intents[0].aliquots[0], volume_uL=10),)),) + intents[1:]
            if case == "duplicate-id": intents = intents[:-1] + (replace(intents[-1], step_id=intents[0].step_id),)
            if case == "tip-budget": session.restore_consumables({"tips": 8})
            with pytest.raises(DriverValidationError): session.prepare_group("plate", intents, bindings)
            assert not session.backend.protocol.commands()
            assert not session.groups.preparations
        finally: session.close()

    def test_wave_objects_cannot_be_reordered_modified_or_reused(self, tmp_path):
        session, bindings, intents = PlateBatchFixture().build(tmp_path)
        try:
            prepared = session.prepare_group("plate", intents, bindings)
            with pytest.raises(DriverValidationError): session.execute_group_wave(prepared, prepared.waves[1])
            with pytest.raises(DriverValidationError): session.execute_group_wave(replace(prepared), prepared.waves[0])
            assert not session.backend.protocol.commands()
            assert session.execute_group_wave(prepared, prepared.waves[0]).outcome == "completed"
            with pytest.raises(DriverValidationError): session.execute_group_wave(prepared, prepared.waves[0])
            assert len(session.backend.receipts) == 1
        finally: session.close()

    def test_unknown_wave_blocks_later_columns(self, tmp_path, monkeypatch):
        session, bindings, intents = PlateBatchFixture().build(tmp_path)
        try:
            prepared = session.prepare_group("plate", intents, bindings)
            assert session.execute_group_wave(prepared, prepared.waves[0]).outcome == "completed"
            def fail(*args): raise RuntimeError("second column dispense uncertain")
            monkeypatch.setattr(session.backend.pipette, "dispense", fail)
            feedback = session.execute_group_wave(prepared, prepared.waves[1])
            assert feedback.outcome == "unknown" and feedback.consumed_resources == {"tips": 8}
            with pytest.raises(DriverValidationError): session.execute_group_wave(prepared, prepared.waves[2])
            assert len(session.backend.receipts) == 1 and session.backend.available_tips() == 80
        finally: session.close()
