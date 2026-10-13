# mcp-lab — Design

Date: 2026-10-12
Status: Approved (design), pending spec review

## Purpose

A public GitHub demo repo that keeps its author current on the latest agent-protocol stack and serves as interview preparation. It is built on the **MCP 2026-07-28** specification, then layers production patterns (inspired by Uber's "Designing MCP Gateway" post), evals, agent-to-agent communication (A2A) and an agent-to-user layer (AG-UI / MCP Apps).

Non-goals: production deployment, real credentials, a polished product UI.

## Background (state of the art, Oct 2026)

- **MCP spec 2026-07-28 (current):**
  - The protocol is stateless. There is no `initialize` handshake and no `Mcp-Session-Id`. Each request carries its protocol version, client capabilities and client info in `_meta`.
  - `server/discover` is required. Version mismatches return `UnsupportedProtocolVersionError` (`-32022`).
  - Multi Round-Trip Requests (MRTR) replace server-initiated requests: the server returns `resultType: "input_required"` and the client retries with `inputResponses`.
  - `subscriptions/listen` replaces the GET stream and `resources/subscribe`.
  - SSE resumability is removed.
  - Results that can be cached carry `CacheableResult` fields (`ttlMs`, `cacheScope`). `tools/list` should be returned in a deterministic order.
  - Tasks became an official extension (`io.modelcontextprotocol/tasks`) that works by polling with `tasks/get` and `tasks/update`.
  - OpenTelemetry trace context can be passed in `_meta`.
  - Streamable HTTP POST requests must carry `Mcp-Method` and `Mcp-Name` headers.
  - Tool schemas may use any JSON Schema 2020-12 keyword.
  - Auth: Client ID Metadata Documents replace Dynamic Client Registration (DCR is deprecated). Clients must validate `iss` (RFC 9207).
  - Deprecated: Roots, Sampling, Logging and the HTTP+SSE transport.
- **Python libraries:**
  - Official `mcp` SDK v2.x. `FastMCP` is renamed `MCPServer`, and there is a unified `Client` that works over URL, stdio or in-memory.
  - FastMCP 4.x (PrefectHQ), built on SDK v2. It adds providers (including OpenAPI), proxying, auth, Code Mode, MCP Apps and OpenTelemetry.
- **Other protocols:**
  - A2A (Linux Foundation, v1.x): agent-to-agent communication using Agent Cards and a task lifecycle.
  - AG-UI: streams agent events to a frontend.
  - MCP Apps / A2UI: UI that an agent sends to be displayed.

## Repository layout

```
mcp-lab/
  packages/
    opsdesk-server/      # M1 reference MCP server (official SDK v2)
    mcp-gateway/         # M2 registry + proxy (FastMCP 4)
    evals/               # M3 tool-use evals
    agents-a2a/          # M4 A2A agents
    ui/                  # M5 AG-UI frontend + MCP App
  tests/conformance/     # official MCP conformance suite, pinned
  docs/notes/            # one short note per milestone, for rehearsing
  docs/superpowers/specs # design docs
  .github/workflows/     # CI
```

Tooling:
- a `uv` workspace on Python 3.13, with `ruff`, `pyright` (strict for new packages) and `pytest`;
- all dependencies pinned to exact versions;
- each milestone ends with a git tag (`m1`…`m5`).

## Example domain: OpsDesk

A fictional incident and on-call system with in-memory seed data: services, incidents, runbooks, and fake customer emails embedded in incident text (for redaction in M2). The data is deterministic so tests are repeatable.

## M1 — Reference MCP server (`opsdesk-server`)

Built on the **official `mcp` SDK v2 (`MCPServer`)**, to learn the protocol at its lowest level.

Features:
- **Tools:**
  - `list_incidents`, read-only: `readOnlyHint`, `outputSchema` + `structuredContent`.
  - `get_service_status`, read-only.
  - `restart_service`: `destructiveHint`. It requires confirmation through an **MRTR elicitation** (`input_required`, then a retry carrying the answer). Any state that must survive between the first call and the retry is encoded in `requestState`.
  - `run_diagnostics`: long-running, uses the **tasks extension** if the SDK supports it. Otherwise it is documented as deferred (the SDK v2.3.0 notes list tasks as not yet implemented).
- **Resources:** `runbook://{service}` (template), with `ttlMs` and `cacheScope` set. Change notifications go through `subscriptions/listen`.
- **Prompts:** `postmortem(incident_id)`.
- **Errors:** a missing resource returns `-32602`. Tool failures come back as `isError` results.
- **Transports:** stdio and Streamable HTTP.

Tests:
1. Unit/contract tests using the SDK's in-memory `Client`, covering every tool, resource and prompt, the MRTR retry flow and the error codes.
2. Transport integration tests: a stdio subprocess and an HTTP server. They cover `server/discover`, version negotiation (including a `2025-11-25` client), and the required `Mcp-Method` / `Mcp-Name` headers.
3. The official conformance suite, run in CI.
4. An MCP Inspector CLI smoke script, run in CI.

Done when:
- CI (lint, types, tests, conformance, Inspector) is green;
- the README shows OpsDesk connected to Claude Code;
- `docs/notes/m1.md` explains the 2026-07-28 changes and the reasons behind them.

## M2 — Production MCP (`mcp-gateway`)

> **Split (2026-10-14):** M2 is delivered as three plans, each shippable on its own:
> - **M2a, gateway core:** registry (tools off by default, hot reload), proxy per server, OpenAPI backend, PII redaction, per-caller rate limits. Plan: `docs/superpowers/plans/2026-10-14-m2a-gateway-core.md`.
> - **M2b, auth:** OAuth 2.1, CIMD, `iss` validation, per-server/per-tool access.
> - **M2c, scale and observability:** Omni mode, response projection, OTel to Jaeger.
>
> Found while researching FastMCP 4.1.0: the proxy does not forward `subscriptions/listen` or upstream cache hints, and it has no built-in "Omni" or projection feature. M2a pins the first two as known gaps with characterization tests; M2c builds Omni and projection itself.

Built on FastMCP 4. It fronts OpsDesk plus one server generated from an OpenAPI spec.

- **Registry (control plane):** a YAML or SQLite file listing servers and tools with owner and on/off state. New tools start **switched off**, and turning one on is a reviewed config change.
- **Proxy (data plane):** one `/<server>/mcp` endpoint per server. The config is reloaded without restarting.
- **Auth:**
  - OAuth 2.1 with a local auth server;
  - clients identify themselves with a Client ID Metadata Document;
  - clients validate `iss`;
  - access is checked per server, with optional per-tool overrides.
- **Policies:** PII redaction on responses, per-caller rate limits.
- **Scale features:**
  - Omni mode, with four meta-tools: `discover_servers`, `discover_tools`, `get_tool_schema`, `invoke_tool`;
  - response projection: the caller asks for only the fields it needs.
- **Observability:** OpenTelemetry traces from client to gateway to server via `traceparent` in `_meta`, viewed in Jaeger via docker-compose.
- **Tests:**
  - switched-off tools are invisible and can't be called;
  - redaction removes the data;
  - failed auth returns 401/403;
  - traces stay connected end to end.

## M3 — Evals (`evals`)

- **Dataset:** 30–50 OpsDesk tasks, each with the expected tool calls and arguments.
- **Metrics:**
  - tool-selection accuracy;
  - argument correctness;
  - whether the model confirms before destructive actions;
  - tokens and latency.
- **Experiment:** 50+ tools exposed directly vs. through Omni discovery vs. Code Mode.
- **Harness:** a pytest-based harness by default. Promptfoo or DeepEval will be evaluated at implementation time.
- **Models:** Claude Haiku 5.5 for routine runs, Claude Sonnet 5.5 for the headline results.
- **Schedule and output:** runs nightly in CI using an API key stored as a CI secret. Results are written as a Markdown table in the README.

## M4 — Agent-to-agent (`agents-a2a`)

- **Agents:**
  - a Triage agent;
  - a Remediation agent.
- **How they work together:** they find each other through A2A Agent Cards, and Triage delegates to Remediation via the A2A task lifecycle, with streaming updates. Both agents call tools through the M2 gateway over MCP.
- **Library:** the official Python `a2a-sdk`. The exact A2A spec version will be confirmed against the official spec at implementation time.
- **Tests:** Agent Card validation, plus a full scenario run with a mocked LLM (deterministic, no API cost).

## M5 — User-facing layer (`ui`)

- An AG-UI frontend that streams agent events, with a human approval step before `restart_service`.
- One OpsDesk tool returns an MCP App: an interactive incident dashboard.
- Kept deliberately thin; it exists for demos.

## Rules for every milestone

- CI gets stricter with each milestone, and merging requires a green build.
- A monthly freshness check flags new releases of the spec, `mcp`, `fastmcp`, `a2a-sdk` and the conformance tooling.
- No real secrets. Demo credentials are local only. The eval API key comes from a CI secret.
- Git identity: `AndresRubio <2109109+AndresRubio@users.noreply.github.com>`.

## Open items (decided per milestone, not now)

- Whether the SDK supports the tasks extension (M1). If not, it is documented and deferred.
- Which eval harness to use (M3).
- Which A2A spec version to pin (M4).
