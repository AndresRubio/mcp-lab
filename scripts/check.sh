#!/usr/bin/env bash
# The repository's check, the one CI runs: lint, format, types, then the tests under `python -X dev`
# (asyncio debug mode catches thread-safety and slow-callback problems in async code; warnings are
# errors through pyproject.toml). Extra arguments go to pytest.
#
# Usage: scripts/check.sh [pytest args...]
#   scripts/check.sh -k registry
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run python -X dev -m pytest "$@"
