"""Thermocycler capability and access contracts against the official virtual API."""

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.models import DriverValidationError, SimulatorBinding, WellAddress
from culsma_opentrons.modules.models import TemperatureHoldIntent, TemperatureTargetVolume
from culsma_opentrons.operations import ResolvedContainerRef, TransferAliquot, TransferIntent
from culsma_opentrons.plugin import SimulatorSessionFactory


class ThermocyclerFixture:
    def raw(self, config):
        raw = config.snapshot()
        raw["modules"] = {"cycler": {"model": "thermocyclerModuleV2", "slot": "7"}}
        raw["labware"]["assay"] = {"load_name": "nest_96_wellplate_100ul_pcr_full_skirt", "module": "cycler"}
        raw["bindings"] = {"source": {"labware": "tubes", "well": "A1"},
                           "target": {"labware": "assay", "well": "A1"}}
        return raw

    def build(self, config, directory):
        raw = self.raw(config)
        session = SimulatorSessionFactory().create(DeckConfiguration.from_dict(raw), directory)
        bindings = tuple(SimulatorBinding(ResolvedContainerRef(key), WellAddress(**value))
                         for key, value in raw["bindings"].items())
        intent = TemperatureHoldIntent("hold", (ResolvedContainerRef("target"),), 37, 60,
            (TemperatureTargetVolume(ResolvedContainerRef("target"), 50),))
        return session, intent, bindings

    def transfer(self):
        return TransferIntent("transfer", ResolvedContainerRef("target"),
                              (TransferAliquot(ResolvedContainerRef("source"), 50),))


class TestThermocyclerModule:
    @pytest.mark.parametrize("temperature", [4, 37, 95, 98, 99])
    def test_hold_uses_official_blocking_api(self, config, tmp_path, monkeypatch, temperature):
        session, intent, bindings = ThermocyclerFixture().build(config, tmp_path)
        try:
            module = session.backend.modules["cycler"]
            calls = []
            original = module.set_block_temperature
            def observe(*args, **kwargs):
                calls.append((args, kwargs))
                return original(*args, **kwargs)
            monkeypatch.setattr(module, "set_block_temperature", observe)
            assert session.describe_session().processing_capabilities == {"thermal_hold": {"cycler": {"labware": ["assay"], "temperature_range_celsius": [4, 99]}}}
            prepared = session.prepare_temperature_hold(replace(intent, temperature_celsius=temperature), bindings)
            assert not session.backend.protocol.commands()
            receipt = session.execute_temperature_hold(prepared)
            assert receipt.outcome == "completed" and receipt.consumed_resources == {}
            assert calls == [((temperature,), {"hold_time_seconds": 60, "block_max_volume": 50})]
            assert receipt.payload["phases"] == dict.fromkeys(
                ["close_lid", "block_hold", "deactivate_block", "open_lid"], "returned")
            commands = receipt.payload["commands"]
            assert len(commands) == 4 and commands[0] == "Closing Thermocycler lid"
            assert f"{temperature}.0" in commands[1] and "hold time" in commands[1]
            assert "Deactivating" in commands[2] and "Opening" in commands[3]
            assert not any("lid temperature" in command for command in commands)
            assert receipt.payload["sample_temperature_measured"] is False
            assert receipt.payload["lid_observed_position"] == module.lid_position
            assert receipt.payload["volume_basis"] == "language_material_state"
            assert session.backend.available_tips() == 96
            with pytest.raises(DriverValidationError): session.execute_temperature_hold(prepared)
        finally:
            session.close()

    @pytest.mark.parametrize("kind", ["foreign", "modified", "stale"])
    def test_preparation_guard(self, config, tmp_path, kind):
        session, intent, bindings = ThermocyclerFixture().build(config, tmp_path)
        other, _, _ = ThermocyclerFixture().build(config, tmp_path)
        try:
            prepared = session.prepare_temperature_hold(intent, bindings)
            if kind == "modified": prepared = replace(prepared, intent=replace(intent, temperature_celsius=38))
            if kind == "stale": session.bindings.config.modules["cycler"]["slot"] = "8"
            with pytest.raises(DriverValidationError):
                (other if kind == "foreign" else session).execute_temperature_hold(prepared)
            assert not session.backend.protocol.commands() and not other.backend.protocol.commands()
        finally:
            session.close()
            other.close()

    @pytest.mark.parametrize("phase,method", [("close_lid", "close_lid"), ("block_hold", "set_block_temperature"),
        ("deactivate_block", "deactivate_block"), ("open_lid", "open_lid")])
    @pytest.mark.parametrize("after_return", [False, True])
    def test_unknown_phase_blocks_session(self, config, tmp_path, monkeypatch, phase, method, after_return):
        session, intent, bindings = ThermocyclerFixture().build(config, tmp_path)
        try:
            prepared = session.prepare_temperature_hold(intent, bindings)
            module = session.backend.modules["cycler"]
            original = getattr(module, method)
            def fail(*args, **kwargs):
                if after_return: original(*args, **kwargs)
                raise RuntimeError("uncertain thermocycler phase")
            monkeypatch.setattr(module, method, fail)
            receipt = session.execute_temperature_hold(prepared)
            assert receipt.outcome == "unknown" and receipt.payload["phases"][phase] == "started"
            assert session.backend.poisoned
            with pytest.raises(DriverValidationError): session.prepare_transfer(ThermocyclerFixture().transfer(), bindings)
        finally:
            session.close()

    @pytest.mark.parametrize("kind", ["anchor", "slot7", "slot8", "slot10", "slot11", "module-collision", "plate", "multichannel"])
    def test_configuration_rejects(self, config, kind):
        raw = ThermocyclerFixture().raw(config)
        if kind == "anchor": raw["modules"]["cycler"]["slot"] = "8"
        elif kind.startswith("slot"): raw["tiprack"]["slot"] = kind.removeprefix("slot")
        elif kind == "module-collision": raw["modules"]["other"] = {"model": "temperature module gen2", "slot": "10"}
        elif kind == "plate": raw["labware"]["assay"]["load_name"] = "corning_96_wellplate_360ul_flat"
        elif kind == "multichannel": raw["pipette"] = "p300_multi_gen2"
        with pytest.raises(ValueError): DeckConfiguration.from_dict(raw)

    @pytest.mark.parametrize("kind", ["fractional", "range", "collateral", "wrong-module", "active-block", "active-lid", "attached-tip"])
    def test_preparation_rejects_before_actions(self, config, tmp_path, monkeypatch, kind):
        session, intent, bindings = ThermocyclerFixture().build(config, tmp_path)
        try:
            if kind == "fractional": intent = replace(intent, temperature_celsius=37.5)
            if kind == "range": intent = replace(intent, temperature_celsius=100)
            if kind == "wrong-module": intent = replace(intent, targets=(ResolvedContainerRef("source"),))
            if kind == "collateral":
                session.bindings.config.bindings["other"] = WellAddress("assay", "B1")
                bindings += (SimulatorBinding(ResolvedContainerRef("other"), WellAddress("assay", "B1")),)
            if kind.startswith("active"):
                prop = "block_target_temperature" if kind == "active-block" else "lid_target_temperature"
                monkeypatch.setattr(type(session.backend.modules["cycler"]), prop, property(lambda self: 37))
            if kind == "attached-tip":
                session.backend.pipette.pick_up_tip()
            before = list(session.backend.protocol.commands())
            with pytest.raises(DriverValidationError): session.prepare_temperature_hold(intent, bindings)
            assert session.backend.protocol.commands() == before
        finally:
            session.close()

    def test_transfer_opens_lid_under_authorized_execution(self, config, tmp_path):
        session, _, bindings = ThermocyclerFixture().build(config, tmp_path)
        try:
            prepared = session.prepare_transfer(ThermocyclerFixture().transfer(), bindings)
            assert not session.backend.protocol.commands()
            description = session.describe_transfer(prepared)
            assert description["module_preconditions"] == [{"module_id": "cycler", "action": "open_lid"}]
            receipt = session.execute_transfer(prepared)
            assert receipt.outcome == "completed" and receipt.consumed_resources == {"tips": 1}
            assert receipt.payload["commands"][0] == "Opening Thermocycler lid"
            assert receipt.payload["module_action_results"][0]["status"] == "returned"
            missing = dict(receipt.payload, module_action_results=[])
            assert session.normalizer.interpret(description, missing, "transfer", session.bindings.config.fingerprint()).outcome == "unknown"
        finally:
            session.close()

    def test_transfer_lid_failure_never_pipettes(self, config, tmp_path, monkeypatch):
        session, _, bindings = ThermocyclerFixture().build(config, tmp_path)
        try:
            prepared = session.prepare_transfer(ThermocyclerFixture().transfer(), bindings)
            def fail(): raise RuntimeError("uncertain lid")
            monkeypatch.setattr(session.backend.modules["cycler"], "open_lid", fail)
            receipt = session.execute_transfer(prepared)
            assert receipt.outcome == "unknown" and receipt.consumed_resources == {"tips": 0}
            assert all(item["status"] == "not_started" for item in receipt.payload["action_results"])
            assert session.backend.poisoned
        finally:
            session.close()

    @pytest.mark.parametrize("kind", ["hold", "transfer"])
    @pytest.mark.parametrize("timing", ["before_prepare", "before_execute"])
    def test_active_module_blocks_new_actions(self, config, tmp_path, monkeypatch, kind, timing):
        session, intent, bindings = ThermocyclerFixture().build(config, tmp_path)
        try:
            request = intent if kind == "hold" else ThermocyclerFixture().transfer()
            prepare = session.prepare_temperature_hold if kind == "hold" else session.prepare_transfer
            execute = session.execute_temperature_hold if kind == "hold" else session.execute_transfer
            if timing == "before_execute": prepared = prepare(request, bindings)
            monkeypatch.setattr(type(session.backend.modules["cycler"]), "block_target_temperature", property(lambda self: 37))
            if timing == "before_prepare":
                with pytest.raises(DriverValidationError): prepare(request, bindings)
            else:
                if kind == "hold":
                    with pytest.raises(DriverValidationError): execute(prepared)
                else:
                    assert execute(prepared).outcome == "rejected"
            assert not session.backend.protocol.commands()
            assert not session.backend.poisoned
        finally:
            session.close()

    def test_architecture(self):
        root = Path(__file__).resolve().parents[1] / "src/culsma_opentrons/modules"
        for name in ("thermal_router_interactors.py", "thermocycler_interactors.py", "incubation_interactors.py", "assembly.py"):
            tree = ast.parse((root / name).read_text())
            assert not any(isinstance(node, ast.FunctionDef) and node.name.startswith("_")
                           and not node.name.startswith("__") for node in ast.walk(tree))
            assert not any(isinstance(node, ast.ImportFrom) and (node.module or "").startswith(("opentrons", "culsma_os")) for node in ast.walk(tree))
        router = (root / "thermal_router_interactors.py").read_text()
        assert "Mapping[str, TemperatureHoldPort]" in router
        assert "ThermocyclerHoldInteractor" not in router

    @pytest.mark.parametrize("kind", ["missing", "unknown", "duplicate", "foreign", "too-large"])
    def test_volume_contract(self, config, tmp_path, kind):
        session, intent, bindings = ThermocyclerFixture().build(config, tmp_path)
        try:
            target = ResolvedContainerRef("target")
            values = {"missing": (), "unknown": (TemperatureTargetVolume(target, None),),
                      "duplicate": (TemperatureTargetVolume(target, 30), TemperatureTargetVolume(target, 50)),
                      "foreign": (TemperatureTargetVolume(ResolvedContainerRef("source"), 50),),
                      "too-large": (TemperatureTargetVolume(target, 101),)}[kind]
            with pytest.raises(DriverValidationError) as error:
                session.prepare_temperature_hold(replace(intent, material_volumes=values), bindings)
            assert error.value.code == "DRV_OT_MODULE_VOLUME"
            assert not session.backend.protocol.commands()
        finally:
            session.close()

    @pytest.mark.parametrize("volume", [0, 30, 50, 100])
    def test_current_volume_reaches_sdk(self, config, tmp_path, monkeypatch, volume):
        session, intent, bindings = ThermocyclerFixture().build(config, tmp_path)
        try:
            seen = []
            module = session.backend.modules["cycler"]
            original = module.set_block_temperature
            def capture(*args, **kwargs):
                seen.append(kwargs["block_max_volume"])
                return original(*args, **kwargs)
            monkeypatch.setattr(module, "set_block_temperature", capture)
            intent = replace(intent, material_volumes=(TemperatureTargetVolume(intent.targets[0], volume),))
            receipt = session.execute_temperature_hold(session.prepare_temperature_hold(intent, bindings))
            assert receipt.outcome == "completed" and seen == [volume]
            assert receipt.payload["block_max_volume_uL"] == volume
        finally:
            session.close()
