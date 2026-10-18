import os
import re
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml

from gateway.pager import create_pager_app

Announced = Callable[..., AbstractContextManager[re.Match[str]]]


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


@pytest.fixture
async def pager_http() -> AsyncIterator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=create_pager_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://pager.local") as client:
        yield client


@contextmanager
def _announced(args: list[str], pattern: str, timeout: float = 20) -> Iterator[re.Match[str]]:
    """Run `args`, wait until its stderr matches `pattern`, yield the match, then stop it.

    stderr goes to a file, not a PIPE: an undrained pipe blocks the child once it fills.
    """
    with tempfile.TemporaryFile(mode="w+") as log:
        proc = subprocess.Popen(args, stderr=log, stdout=subprocess.DEVNULL, text=True)
        try:
            deadline = time.monotonic() + timeout
            while True:
                log.seek(0)
                output = log.read()
                if match := re.search(pattern, output):
                    break
                if proc.poll() is not None:
                    raise RuntimeError(f"{args} exited with code {proc.returncode}: {output}")
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{args} did not announce itself: {output}")
                time.sleep(0.05)
            yield match
        finally:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


@pytest.fixture
def announced() -> Announced:
    """`with announced([cmd...], r"regex") as match:` runs a server subprocess for the block."""
    return _announced
