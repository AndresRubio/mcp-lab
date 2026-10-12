# mcp-lab

A living lab for the newest agent-protocol stack, built milestone by milestone with a real test pyramid.
Targets **MCP spec 2026-07-28** (stateless MCP) while staying compatible with 2025-11-25 clients.

| Milestone | Status | What it shows |
|---|---|---|
| M1 OpsDesk server | ✅ `m1` (Claude Code hookup unverified) | Official Python SDK v2, MRTR elicitation, `server/discover`, cache hints, `subscriptions/listen` |
| M2 MCP gateway | planned | Registry (tools off by default), proxy, OAuth (CIMD), PII redaction, Omni discovery, OTel |
| M3 Evals | planned | Tool-selection accuracy: direct vs discovery vs Code Mode |
| M4 A2A agents | planned | Triage → Remediation delegation over A2A, tools via MCP |
| M5 UI | planned | AG-UI approval flow, MCP App dashboard |

## Quick start

```bash
uv sync
uv run opsdesk-server --transport stdio                                  # stdio
uv run opsdesk-server --transport http --host 127.0.0.1 --port 8000     # http://127.0.0.1:8000/mcp
```

Use it from Claude Code:

```bash
claude mcp add opsdesk -- uv run --directory /path/to/mcp-lab opsdesk-server
claude mcp list
```

Then ask "which services are down?" (it should call `get_service_status` or `list_incidents`) and
"restart search" (Claude Code should ask you to confirm. On 2026-07-28 clients the confirmation arrives via MRTR: `input_required` plus a retry; a client that negotiates 2025-11-25 gets a standalone `elicitation/create` instead). Remove it
afterwards with `claude mcp remove opsdesk`.

> **Claude Code hookup status:** not yet verified end to end. The `claude mcp add` step was deliberately
> not run during M1 (it modifies the local Claude Code configuration), so whether your Claude Code
> version supports form elicitation is unrecorded. If it does not, `restart_service` fails with
> `-32021` (missing required client capability). The server itself is covered by the test pyramid below.

## OpsDesk

| Kind | Name | 2026-07-28 feature on display |
|---|---|---|
| tool | `list_incidents`, `get_service_status` | `outputSchema` + `structuredContent`, `readOnlyHint` |
| tool | `restart_service` | Multi Round-Trip Request: `input_required` → retry with `inputResponses` |
| tool | `run_diagnostics` | request-scoped progress notifications |
| tool | `update_runbook` | `subscriptions/listen` resource-updated events |
| resource | `runbook://{service}` | `ttlMs`/`cacheScope` caching, `-32602` on not-found |
| prompt | `postmortem` | prompt arguments |

## Test pyramid

| Layer | Command | What it proves |
|---|---|---|
| Contract (in-memory) | `uv run pytest` | every tool/resource/prompt, MRTR rounds, error codes |
| Transport | `uv run pytest -k transport` | stdio + Streamable HTTP, version negotiation, required headers |
| Conformance | `./scripts/conformance.sh` | official conformance suite (6 applicable 2025-11-25 scenarios; none exist yet for 2026-07-28) |
| Smoke | `./scripts/inspector-smoke.sh` | MCP Inspector CLI over stdio |

Notes for each milestone are in [`docs/notes/`](docs/notes/).
