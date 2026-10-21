"""Guard for the warnings-as-errors rule in the root pytest config.

A socket dropped without close raises ResourceWarning when it is collected. The root
config turns warnings into errors, so a test that leaks one must fail. This runs an inner
pytest under the root config with Python dev mode on, as CI does, and checks that it fails.
"""

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

ROOT_CONFIG = Path(__file__).resolve().parents[3] / "pyproject.toml"

LEAKY_TEST = """
import gc
import socket


def test_leaks_a_socket():
    socket.socket()
    gc.collect()
"""


def test_leaked_socket_fails_the_suite(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONDEVMODE", "1")
    pytester.makepyfile(test_leaky=LEAKY_TEST)
    result = pytester.runpytest_subprocess(
        "-c", str(ROOT_CONFIG), "--rootdir", str(pytester.path), "-p", "no:cacheprovider", "test_leaky.py"
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*ResourceWarning*"])
