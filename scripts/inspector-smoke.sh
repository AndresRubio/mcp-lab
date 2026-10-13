#!/usr/bin/env bash
# Smoke-test OpsDesk over stdio with the MCP Inspector CLI: list tools, call one.
set -euo pipefail

INSPECTOR_VERSION="2.10.1"
inspect() {
  npx -y "@modelcontextprotocol/inspector@${INSPECTOR_VERSION}" --cli uv run opsdesk-server "$@"
}

tools=$(inspect --method tools/list)
for name in list_incidents get_service_status restart_service run_diagnostics update_runbook; do
  grep -q "\"name\": \"${name}\"" <<<"$tools" || { echo "missing tool: ${name}"; exit 1; }
done

status=$(inspect --method tools/call --tool-name get_service_status --tool-arg service=search)
grep -q '"state": "down"' <<<"$status" || { echo "unexpected status: ${status}"; exit 1; }

echo "inspector smoke: OK"
