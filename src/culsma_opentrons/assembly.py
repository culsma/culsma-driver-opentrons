"""Compose and close a simulator driver session."""

from contextlib import contextmanager
from .backend import OfficialSimulatorBackend
from .binding import OpentronsBindingResolver
from .driver import OpentronsSimulatorDriver
from .request_mapper import TransferRequestMapper
from .translator import OpentronsTransferTranslator
from .transfer import OpentronsTransferDriver
from .session import SimulatorSession


@contextmanager
def open_simulator(state, config):
    session = SimulatorSessionAssembler().build(state, config)
    try:
        yield session.driver
    finally:
        session.close()


def build_driver(bindings):
    return OpentronsSimulatorDriver(
        bindings,
        TransferRequestMapper(bindings),
        OpentronsTransferDriver(
            OpentronsTransferTranslator(bindings), bindings.backend
        ),
    )


class SimulatorSessionAssembler:
    def build(self, state, config, motion_path=None, legacy_motion_path=None):
        from .motion.repository import MotionStateRepository
        from .motion.interactors import LabwareMotionInteractor, MotionStateAdoptionInteractor
        motion = None
        if config.labware_moves:
            if motion_path is None:
                raise ValueError("MOTION_STATE_PATH_REQUIRED")
            repository = MotionStateRepository(motion_path)
            try:
                saved, adopted = MotionStateAdoptionInteractor(repository).select(legacy_motion_path)
                motion = LabwareMotionInteractor(config, repository, saved)
                if adopted:
                    repository.save(motion.state)  # Validate before persisting the imported state.

            except BaseException:
                repository.close()
                raise
        try:
            backend = OfficialSimulatorBackend(config, motion.positions() if motion else None)
        except BaseException:
            if motion: motion.close()
            raise
        try:
            bindings = OpentronsBindingResolver(state, config, backend)
            if motion: motion.attach(backend)
            return SimulatorSession(backend, bindings, build_driver(bindings), motion=motion)
        except BaseException:
            backend.close()
            if motion: motion.close()
            raise
