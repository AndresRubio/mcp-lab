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

uv run opsdesk-server --transport http --port "$PORT" &
SERVER_PID=$!
# `uv run` spawns the server as a child process, so stop the children before uv itself.
trap 'pkill -P "$SERVER_PID" 2>/dev/null || true; kill "$SERVER_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  nc -z 127.0.0.1 "$PORT" 2>/dev/null && break
  sleep 0.2
done

mkdir -p tests/conformance/results
for scenario in "${SCENARIOS[@]}"; do
  echo "== ${scenario}"
  npx -y "@modelcontextprotocol/conformance@${CONFORMANCE_VERSION}" server \
    --url "$URL" --scenario "$scenario" -o tests/conformance/results
done
