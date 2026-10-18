# mcp-lab

A living lab for the newest agent-protocol stack, built milestone by milestone with a real test pyramid.
Targets **MCP spec 2026-07-28** (stateless MCP) while staying compatible with 2025-11-25 clients.

| Milestone | Status | What it shows |
|---|---|---|
| M1 OpsDesk server | ✅ `m1` (Claude Code hookup unverified) | Official Python SDK v2, MRTR elicitation, `server/discover`, cache hints, `subscriptions/listen` |
| M2a MCP gateway core | ✅ `m2a` | FastMCP 4.1 proxy per backend, YAML registry (tools off by default, hot reload), PII redaction, per-caller rate limits, OpenAPI backend |
| M2b MCP gateway auth | planned | OAuth 2.1 with a local auth server, CIMD clients, `iss` validation, per-server/per-tool access, token-subject callers |
| M2c MCP gateway scale & observability | planned | Omni discovery meta-tools, response projection, OTel traces to Jaeger |
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

## Gateway

`packages/mcp-gateway` puts every registered MCP server behind one endpoint, `/<name>/mcp`, built on
FastMCP 4.1's proxy. `packages/mcp-gateway/registry.yaml` is the control plane: a tool the registry
does not list is hidden, and a listed tool stays disabled unless it says `enabled: true`. The demo
registry fronts OpsDesk (stdio) and Pager, a small OpenAPI HTTP API whose on-call contacts are e-mail
addresses that the gateway redacts.

```bash
uv run python -m gateway.pager --port 8100                                          # terminal 1: Pager API
uv run mcp-gateway serve --registry packages/mcp-gateway/registry.yaml --port 8080   # terminal 2
curl -s http://127.0.0.1:8080/healthz          # {"status":"ok","servers":["opsdesk","pager"]}
uv run mcp-gateway audit --registry packages/mcp-gateway/registry.yaml
```

`serve` prints one endpoint per server (`http://127.0.0.1:8080/opsdesk/mcp`, `.../pager/mcp`).
`audit` connects to each backend and lists the tools the registry does not cover yet:

```
opsdesk (owner: oncall-team): pending review: run_diagnostics
pager (owner: sre): every tool is reviewed
```

Call it like any MCP server:

```bash
uv run python - <<'EOF'
import anyio
from mcp import Client


async def main() -> None:
    async with Client("http://127.0.0.1:8080/opsdesk/mcp") as opsdesk:
        print(sorted(t.name for t in (await opsdesk.list_tools()).tools))
    async with Client("http://127.0.0.1:8080/pager/mcp") as pager:
        print((await pager.call_tool("oncall_for", {"service": "api"})).structured_content)


anyio.run(main)
EOF
```

```
['get_service_status', 'list_incidents', 'restart_service']
{'service': 'api', 'primary': '[REDACTED:email]', 'secondary': '[REDACTED:email]'}
```

Registry edits apply on the next request, with no restart: replace the `# run_diagnostics is
deliberately unlisted` comment with `run_diagnostics: {enabled: true, owner: oncall-team}` and run
the snippet again; `run_diagnostics` is now listed. Adding or removing a server needs a restart.
The registry governs tools only: resources and prompts pass through ungoverned (they are still
redacted). See [`docs/notes/m2a.md`](docs/notes/m2a.md) for the design and its known gaps.

## Test pyramid

| Layer | Command | What it proves |
|---|---|---|
| Contract (in-memory) | `uv run pytest` | every tool/resource/prompt, MRTR rounds, error codes |
| Transport | `uv run pytest -k transport` | stdio + Streamable HTTP, version negotiation, required headers |
| Gateway | `uv run pytest packages/mcp-gateway` | registry, redaction, rate limits, MRTR and progress through the proxy, in-process HTTP, a `serve` subprocess end to end |
| Conformance | `./scripts/conformance.sh` | official conformance suite (6 applicable 2025-11-25 scenarios; none exist yet for 2026-07-28) |
| Smoke | `./scripts/inspector-smoke.sh` | MCP Inspector CLI over stdio |

Notes for each milestone are in [`docs/notes/`](docs/notes/).
