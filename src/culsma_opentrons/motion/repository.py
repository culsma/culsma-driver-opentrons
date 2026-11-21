"""Durable virtual placement, independent of OS execution evidence."""
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol


class MotionStateRepositoryPort(Protocol):
    def load(self): ...
    def legacy(self, path):
        candidate = Path(path) if path is not None else None
        if candidate is None or candidate.resolve() == self.path.resolve() or not candidate.exists():
            return None
        raw = candidate.read_bytes()
        from hashlib import sha256
        return str(candidate.resolve()), sha256(raw).hexdigest(), json.loads(raw)

    def save(self, state): ...
    def close(self): ...
    def legacy(self, path): ...


class MotionStateRepository:
    def __init__(self, path):
        import fcntl
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.path.with_suffix(".lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.lock.close()
            raise

    def load(self):
        return json.loads(self.path.read_text()) if self.path.exists() else None

    def legacy(self, path):
        candidate = Path(path) if path is not None else None
        if candidate is None or candidate.resolve() == self.path.resolve() or not candidate.exists():
            return None
        raw = candidate.read_bytes()
        from hashlib import sha256
        return str(candidate.resolve()), sha256(raw).hexdigest(), json.loads(raw)

    def save(self, state):
        with NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(state, stream, sort_keys=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def close(self):
        self.lock.close()
