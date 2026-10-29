#!/usr/bin/env bash
# Run the official MCP conformance suite against OpsDesk over Streamable HTTP.
# conformance 0.1.16 has no 2026-07-28 scenarios yet, so this exercises OpsDesk's
# backward-compatible 2025-11-25 path. Scenarios that need the suite's own fixture
# tools (images, sampling, ...) are not applicable to a domain server and are skipped.
set -euo pipefail

CONFORMANCE_VERSION="0.1.16"
PORT="${PORT:-18765}"
URL="http://localhost:${PORT}/mcp"
SCENARIOS=(
  server-initialize
  ping
  tools-list
  resources-list
  prompts-list
  dns-rebinding-protection
)

if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
  echo "port ${PORT} is already in use; stop that process or set PORT" >&2
  exit 1
fi

uv run opsdesk-server --transport http --port "$PORT" &
SERVER_PID=$!
# `uv run` spawns the server as a child process, so stop the children before uv itself.
trap 'pkill -P "$SERVER_PID" 2>/dev/null || true; kill "$SERVER_PID" 2>/dev/null || true' EXIT

# Check the server is still alive on every poll: a server that died at startup fails at once, and
# a stray process already listening on the port is not mistaken for OpsDesk.
started=0
for _ in $(seq 1 50); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    wait "$SERVER_PID" || status=$?
    echo "server exited during startup (exit code ${status:-0}) before listening on port ${PORT}" >&2
    exit 1
  fi
  if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
    started=1
    break
  fi
  sleep 0.2
done
if [ "$started" -ne 1 ]; then
  echo "server did not start on port ${PORT}" >&2
  exit 1
fi

mkdir -p tests/conformance/results
for scenario in "${SCENARIOS[@]}"; do
  echo "== ${scenario}"
  npx -y "@modelcontextprotocol/conformance@${CONFORMANCE_VERSION}" server \
    --url "$URL" --scenario "$scenario" -o tests/conformance/results
done
