"""Guard for the warnings-as-errors rule in the root pytest config.

A socket dropped without close raises ResourceWarning when it is collected. The failure
comes from `filterwarnings = ["error"]` in the root config: pytest applies that filter
around each test, and it overrides Python's default ignore for ResourceWarning. Python dev
mode (`-X dev`) is not needed for this. The inner pytest runs in a subprocess: in-process, it
inherits the outer run's warning filters and fails with or without the config line, so it
cannot detect the line being dropped.
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


def test_leaked_socket_fails_the_suite(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_leaky=LEAKY_TEST)
    result = pytester.runpytest_subprocess(
        "-c", str(ROOT_CONFIG), "--rootdir", str(pytester.path), "-p", "no:cacheprovider", "test_leaky.py"
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*ResourceWarning*"])
