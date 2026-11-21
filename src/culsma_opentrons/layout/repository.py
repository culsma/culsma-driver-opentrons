"""Read declared deck configuration without creating an SDK protocol context."""
import json
from pathlib import Path


class FileLayoutConfigurationRepository:
    def read(self, path):
        with Path(path).open('rb') as stream:
            content = stream.read(1000001)
        if len(content) > 1000000:
            raise ValueError('LAYOUT_CONFIGURATION_TOO_LARGE')
        return json.loads(content)
