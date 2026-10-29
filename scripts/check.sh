#!/usr/bin/env bash
# The repository's check, the one CI's python and ascii-locale jobs run: lint, format, types, then
# the tests. Warnings are errors through pyproject.toml, and two interpreter flags feed that:
#   -X dev enables asyncio debug mode, which turns un-awaited coroutines and unclosed resources into
#     warnings that fail the test. It only logs slow callbacks; a slow callback never fails a test.
#   -X warn_default_encoding raises EncodingWarning for every text open, read or write without an
#     explicit encoding. Child processes that a test starts with `sys.executable` do not inherit it.
# Extra arguments go to pytest.
#
# Usage: scripts/check.sh [pytest args...]
#   scripts/check.sh -k registry
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run python -X dev -X warn_default_encoding -m pytest "$@"
