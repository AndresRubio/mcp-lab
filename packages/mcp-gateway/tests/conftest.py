import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.yaml"


@pytest.fixture
def write_registry(registry_path: Path) -> Callable[[Any], Path]:
    """Write `data` (a dict as YAML, or raw text) and move the mtime forward by a whole second.

    Filesystem timestamps can be coarse, so two quick writes may share an mtime; bumping it
    explicitly makes every write visible to `RegistryHolder.refresh()` without sleeping.
    """
    generation = [0]

    def write(data: Any) -> Path:
        text = data if isinstance(data, str) else yaml.safe_dump(data)
        registry_path.write_text(text)
        generation[0] += 1
        stamp = 1_700_000_000_000_000_000 + generation[0] * 1_000_000_000
        os.utime(registry_path, ns=(stamp, stamp))
        return registry_path

    return write
