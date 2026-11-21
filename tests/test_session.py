from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import ast
import pytest
from culsma_opentrons.models import DriverBuildContext
from culsma_opentrons.operations import (
    ResolvedContainerRef,
    TransferAliquot,
    TransferIntent,
)
from culsma.runtime.state import init_state
from culsma_opentrons.assembly import SimulatorSessionAssembler
from culsma_opentrons.models import DriverValidationError, SimulatorBinding
from culsma_opentrons.plugin import OpentronsSimulatorFactory, SimulatorSessionFactory
from culsma_opentrons.receipt import OpentronsReceiptNormalizer
from culsma_opentrons.session import SimulatorSession
from .test_operations import prepare_fixture, configuration
from .test_integration import plan


class TestSimulatorSession:
    @pytest.fixture
    def backend(self):
        from culsma_opentrons.backend import OfficialSimulatorBackend

        backend = OfficialSimulatorBackend(configuration("multi-source"))
        yield backend
        backend.close()

    def fixture(self, backend):
        driver, step = prepare_fixture("multi-source", backend)
        request = driver.mapper.from_step(step)
        intent = TransferIntent(
            request.step_id,
            ResolvedContainerRef(request.target.id),
            tuple(
                TransferAliquot(ResolvedContainerRef(a.source.id), a.volume_uL)
                for a in request.aliquots
            ),
            request.span,
        )
        bindings = tuple(
            SimulatorBinding(ResolvedContainerRef(h.id), a)
            for h, a in driver.bindings.addresses.items()
        )
        return SimulatorSession(backend, driver.bindings, driver), intent, bindings

    def test_installed_drivers_remain_discoverable_without_configured_modules(self, backend):
        session, _, _ = self.fixture(backend)
        assert not session.processing_capabilities()
        drivers = session.instrument_drivers()
        assert "thermal_hold" in drivers["temperature"]
        assert "magnetic_separation" in drivers["magnet"]
        assert "orbital_shake" in drivers["shaker"]
        assert "transfer" in drivers["pipetting"]

    def test_two_entrypoints_share_device_implementation(self, config, tmp_path):
        created = []

        class SharedFactory:
            def load_configuration(self, path):
                return config

            def create(self, config, artifacts_dir, state, strict_path):
                assert strict_path is True
                session = SimulatorSessionAssembler().build(state, config)
                created.append(session)
                return session

        compiled = plan()
        context = DriverBuildContext(
            compiled, init_state(compiled), Path("deck.json"), tmp_path
        )
        with OpentronsSimulatorFactory(SharedFactory()).open(context) as driver:
            assert driver is created[0].driver
            assert driver.transfers is created[0].transfers
        assert created[0].closed
        hosted = SimulatorSessionFactory().create(config, tmp_path)
        try:
            assert type(hosted.transfers) is type(created[0].transfers)
            assert type(hosted.transfers.translator) is type(
                created[0].transfers.translator
            )
            assert type(hosted.backend) is type(created[0].backend)
        finally:
            hosted.close()

    def test_preparation_reports_requirements_without_actions(self, backend):
        session, intent, bindings = self.fixture(backend)
        intent = replace(intent, aliquots=(replace(intent.aliquots[0], volume_uL=610),))
        snapshot = session.bindings.config.snapshot()
        from culsma_opentrons.config import DeckConfiguration

        snapshot["transfer_policy"]["stroke_mode"] = "balanced_split"
        session.bindings.config = DeckConfiguration.from_dict(snapshot)
        prepared = session.prepare_transfer(intent, bindings)
        assert session.describe_transfer(prepared)["resource_requirements"] == {
            "tips": 3
        }
        assert not backend.protocol.commands()
        assert len(prepared.operation.commands) == 3

    @pytest.mark.parametrize(
        "corruption", ["volume", "identity", "action", "configuration", "malformed"]
    )
    def test_receipt_mismatch_is_unknown(self, backend, corruption):
        session, intent, bindings = self.fixture(backend)
        prepared = session.prepare_transfer(intent, bindings)
        description = session.describe_transfer(prepared)
        raw = session.transfers.execute_transfer(prepared).to_payload()
        if corruption == "volume":
            raw["transfers"][0]["volume_uL"] = 999
        elif corruption == "identity":
            raw["step_id"] = "other"
        elif corruption == "action":
            raw["action_results"][0]["action_id"] = "other"
        elif corruption == "configuration":
            raw["config_sha256"] = "other"
        else:
            raw["action_results"] = None
        assert (
            OpentronsReceiptNormalizer()
            .interpret(
                description, raw, intent.step_id, session.bindings.config.fingerprint()
            )
            .outcome
            == "unknown"
        )

    def test_restored_consumption_limits_preparation(self, backend):
        session, intent, bindings = self.fixture(backend)
        session.restore_consumables({"tips": 95})
        assert backend.available_tips() == 1
        assert not backend.protocol.commands()
        with pytest.raises(DriverValidationError, match="Need 2 fresh tips"):
            session.prepare_transfer(intent, bindings)
        one = replace(intent, aliquots=intent.aliquots[:1])
        feedback = session.execute_transfer(session.prepare_transfer(one, bindings))
        assert feedback.outcome == "completed" and feedback.consumed_resources == {
            "tips": 1
        }
        assert backend.available_tips() == 0
        session.restore_consumables({"tips": 96})
        assert backend.available_tips() == 0
        assert any(
            "H12" in command
            for command in feedback.payload["commands"]
            if "Picking up" in command
        )
        with pytest.raises(DriverValidationError):
            session.prepare_transfer(one, bindings)
        with pytest.raises(DriverValidationError):
            session.restore_consumables({"tips": 0})

    def test_invalid_consumption_never_changes_session(self, backend):
        session, _, _ = self.fixture(backend)
        for consumption in [{"tips": -1}, {"tips": True}, {"tips": 97}, {"other": 1}]:
            with pytest.raises(DriverValidationError):
                session.restore_consumables(consumption)
        assert backend.available_tips() == 96 and not backend.protocol.commands()

    def test_binding_failure_is_atomic(self, backend):
        session, intent, bindings = self.fixture(backend)
        before = dict(session.bindings.addresses)
        with pytest.raises(DriverValidationError):
            session.prepare_transfer(intent, bindings + bindings[:1])
        assert session.bindings.addresses == before
        assert not backend.protocol.commands()

    def test_feedback_is_detached_from_evidence(self, backend):
        session, intent, bindings = self.fixture(backend)
        prepared = session.prepare_transfer(intent, bindings)
        feedback = session.execute_transfer(prepared)
        assert feedback.outcome == "completed"
        feedback.payload["transfers"][0]["volume_uL"] = 999
        assert backend.receipts[0].operation.commands[0].volume_uL == 50

    def test_plugin_never_imports_os(self):
        import culsma_opentrons

        for path in Path(culsma_opentrons.__file__).parent.rglob("*.py"):
            tree = ast.parse(path.read_text())
            modules = [
                n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
            ]
            modules += [
                a.name
                for n in ast.walk(tree)
                if isinstance(n, ast.Import)
                for a in n.names
            ]
            assert not any(m.startswith("culsma_os") for m in modules), path
