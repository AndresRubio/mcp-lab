# M2a notes: the gateway core

## The one-line version
One endpoint per backend (`/<name>/mcp`), each a FastMCP 4.1 proxy. A reviewed YAML registry decides
which tools exist, and middleware redacts e-mail addresses in results, resource reads and prompts,
and rate-limits each caller.

## Control plane and data plane
- The control plane is `registry.yaml`, parsed into pydantic models by `gateway.registry`. It records
  who owns each server and tool, which tools are enabled, and each server's rate limit. Unknown keys
  are rejected (`extra="forbid"`), so a typo such as `tols:` fails review instead of silently hiding
  everything (`test_invalid_entries_are_rejected`).
- The data plane is `create_proxy(target)` per backend, plus a `RegistryVisibility` transform and a
  `GatewayPolicyMiddleware`, all mounted in one Starlette app (`gateway.app`). It only reads the
  control plane, through `RegistryHolder.current`.
- The registry is trusted config, equivalent to code: a `stdio` source's `command` and an OpenAPI
  `module:ATTR` spec both run code the operator names, so a registry edit needs the same review as a
  code change.
- `mcp-gateway audit` connects to every backend and prints the tools the registry does not list
  ("pending review"). That is how a newly shipped tool reaches a reviewer
  (`test_audit_reports_unlisted_backend_tools`).

## Disabled by default
A tool the registry does not list is hidden, and a listed tool is disabled unless it says
`enabled: true`. Enabling a tool is a reviewed edit to the YAML. A hidden tool is absent from
`tools/list`, and calling it returns the same `Unknown tool: '<name>'` tool error as a tool that does
not exist, so a caller cannot probe for it (`test_disabled_tool_is_not_callable`,
`test_unlisted_tool_is_hidden_and_unknown`). A server removed from the YAML exposes nothing until the
restart that unmounts it: its tools, resources, resource templates and prompts all list empty, a
resource read fails with `Resource not found` and a `prompts/get` with `Unknown prompt`, as they do
for names that do not exist. Put back in the YAML, the server serves them again
(`test_server_removed_from_registry_exposes_nothing_until_restart`).

**Known gap: the registry governs tools only.** While a server is listed, its resources and prompts
pass through ungoverned: the registry cannot hide or disable them one by one, and no owner is recorded
for them. They are redacted like everything else (see below), but a reviewer sees nothing about them.

## Why hot reload goes through a transform
- `RegistryHolder.refresh()` reloads when the file's `st_mtime_ns` changes. If the new YAML is
  invalid, or the file is not valid UTF-8, it keeps the last good registry, logs the error and
  records it in `.last_error` (`test_invalid_reload_keeps_last_good_registry`,
  `test_non_utf8_reload_keeps_last_good_registry`).
- It records the new mtime only once a load succeeds. An editor's truncate-then-write can be read
  half-done and then finish under the same mtime, and the finished file is still loaded on the next
  request (`test_a_write_that_completes_in_the_same_tick_as_a_torn_read_is_loaded`). While the file
  stays broken, a refresh reads it again only when its stat (mtime and size) differs from the content
  last read and not installed, and compares a SHA-256 digest before parsing, so the same broken bytes
  are read, parsed and logged once; the refresh runs in the event loop on every request, and a read
  is only cheap on a local disk (`test_a_broken_file_is_read_parsed_and_logged_once`,
  `test_ignored_content_under_the_good_mtime_is_read_once_while_an_error_is_pending`). A content
  change that keeps both the mtime and the size of the rejected content is therefore not seen either.
  A content change that keeps the mtime of the last good load is still not seen
  (`test_refresh_ignores_content_change_with_same_mtime`,
  `test_a_same_mtime_change_while_an_error_is_pending_is_still_ignored`). Restoring the last good
  bytes with their old mtime (`cp -p`, an editor's backup) clears the pending error without installing
  anything (`test_restoring_the_good_file_with_its_mtime_clears_the_error`).
- The middleware calls `refresh()` at the start of every request
  (`test_each_request_refreshes_the_registry`).
- `RegistryVisibility` reads `holder.current` on every list and get of tools, resources, resource
  templates and prompts, so a reload needs nothing rebuilt or mutated
  (`test_hot_reload_flips_visibility_without_rebuild`,
  `test_server_removed_from_registry_exposes_nothing_until_restart`).
- `FastMCP.enable()` and `disable()` only append `Visibility` transforms, and no API removes one.
  Toggling a tool ten times would stack ten transforms.
- A transform also hides the tool from `get_tool`, which the call path and any catalog or search
  transform use. Middleware would only filter the wire methods.
- Live: tool enablement, owners and rate limits. Restart needed: adding or removing a server, or
  changing a server's `source`, because each server is a mounted sub-app with its own lifespan, built
  at startup. A reload that makes such an edit still installs the new registry, and logs a WARNING
  naming the servers and saying a restart is needed (`test_restart_only_edits_log_a_warning`).

## Shutdown
`resolve_targets` returns a `ResolvedTargets` (a dict of proxy targets) with an `aclose()` method that
stops the kept-alive stdio backends and closes the OpenAPI backends' HTTP clients. `create_app(...,
on_shutdown=targets.aclose)` runs it after every mounted app's lifespan has exited, and both `serve`
and `audit` pass it (`test_shutdown_stops_the_stdio_backend_and_closes_the_http_client`,
`test_aclose_closes_the_http_client_and_is_idempotent`, `test_audit_leaves_no_backend_process_behind`).

## MRTR (multi round-trip requests) through a proxy
In an MRTR exchange a tool answers `input_required` and the client retries the call with its answers.

- `create_proxy` makes each backend request mirror the front client's era. A hand-built `ProxyClient`
  defaults to the legacy era, and then a 2026-07-28 front breaks: the backend tries to push
  `elicitation/create` with no back-channel. Pinning `mode="legacy"` in the gateway turns
  `test_mrtr_confirmation_crosses_the_gateway` red, and pinning `"2026-07-28"` breaks the legacy front
  (`test_legacy_client_gets_the_confirmation_as_a_push`).
- The stateless front drives the rounds. On a 2026-07-28 front, the backend's `InputRequiredResult`
  goes back to the client as-is, and the client retries the whole `tools/call` with `inputResponses`
  and `requestState`. The proxy keeps nothing in memory between rounds: what the backend needs to
  resume travels inside the sealed `requestState`. A legacy front gets a pushed `elicitation/create`
  instead.
- Both clients work: the fastmcp `Client` (`test_mrtr_restart_with_the_fastmcp_client`) and the
  official `mcp.Client` (`test_mrtr_restart_with_the_official_sdk_client`), the latter over in-process
  Streamable HTTP, which avoids touching fastmcp's private `_mcp_server`.
- `requestState` is sealed twice. The backend seals its state, and the gateway's front (FastMCP
  installs the SDK's `RequestStateBoundary`) seals that again with a per-process ephemeral key. A
  forged `requestState` is rejected at the gateway before any middleware runs
  (`test_forged_request_state_is_rejected_before_it_reaches_the_backend`). With several gateway
  replicas, a retry that lands on another replica fails with `Invalid or expired requestState` unless
  they share a `request_state_security` key.
- A sealed `requestState` is valid for 600 seconds and is not bound to the caller, so a client can
  replay one. The rate limit has to hold up against that (next section).
- stdio needs `keep_alive=True`: the backend's key is per process, so every round must reach the
  same process (`resolve_targets` sets it explicitly). The price is that all callers share one backend
  process and its state (`test_stdio_becomes_a_kept_alive_stdio_transport`). The end-to-end test sees
  `restarts: 1` on a second call (`test_serve_end_to_end_over_a_stdio_backend`).

## Rate limits and MRTR rounds
- Limits use fixed one-minute windows per caller (`clientInfo.name`) and per server.
  `calls_per_minute` is read live from the registry (`test_rate_limit_changes_apply_live`).
- On a 2026-07-28 front, every MRTR round is a separate `tools/call` through the middleware. The
  middleware reserves a slot on every round before forwarding it, and hands the slot back only when
  the backend answers with an `InputRequiredToolResult`. A confirmation exchange (one round that asks,
  one that executes) therefore counts once (`test_confirmed_restart_counts_as_one_call`), and every
  round that executes counts, including a replay of an old `requestState`
  (`test_replayed_request_state_cannot_exceed_the_rate_limit`).
- The reservation happens in one synchronous step, with no `await` between the check and the
  increment, so concurrent replays cannot all pass the check
  (`test_concurrent_replays_cannot_race_past_the_rate_limit`).
- Replay inside the limit is still possible. The limit caps how often it can happen.
- An earlier design counted only calls without `requestState`, so that the first round of an exchange
  paid for it. Replay broke it: a sealed `requestState` stays valid for 600 seconds and is not bound
  to the caller, so a client could resend the executing round any number of times and never be
  counted.
- Rounds that end in `InputRequiredToolResult` are checked but never counted, so a caller below the
  limit can make any number of them. Results with `is_error=True` and calls that raise still count,
  because the call reached the backend (`test_calls_that_fail_at_the_backend_still_count`).
- Windows are fixed, so a caller can make up to twice the limit across a window boundary. The counts
  are pruned when the window rolls over, so memory is bounded by the callers seen in one window
  (`test_the_call_counts_hold_only_the_current_window`). Each caller is counted under
  `caller_key(name)`: a name of up to 64 characters is its own key, and a longer one becomes its first
  32 characters, `~` and the SHA-256 of the whole name. A client choosing a megabyte-long name pins
  97 characters, and names that differ anywhere keep separate budgets
  (`test_a_huge_caller_name_is_held_as_a_bounded_key`,
  `test_names_that_differ_only_late_get_separate_limits`). `call_counts()` and the over-limit
  message report that key. A clock that runs backwards keeps the current
  window instead of opening a new one (`test_a_clock_running_backwards_keeps_the_current_window`).
- Over the limit, the call fails as a tool error (`isError: true`) naming the caller, the server and
  the limit, so the model sees why. FastMCP's built-in `RateLimitingMiddleware` was not used: it hooks
  every request (discover, lists, each MRTR round) and raises a JSON-RPC error (-32000).
- Calls to hidden or unknown tools count too, because the limit applies before the tool is resolved.
- Counters live in process memory. They reset on restart and are not shared between replicas.
- The caller identity is the self-asserted `clientInfo.name`, so a client can pick any name and get
  its own budget (`test_rate_limits_are_per_caller_over_http` shows two names with separate budgets).
  M2b replaces it with the subject of a verified OAuth (Open Authorization) token.

## Redaction scope
The gateway's PII (personally identifiable information) redaction covers results, resource reads and
prompts. `redact()` replaces e-mail addresses with `[REDACTED:email]` in strings, dict values and list
items.

Redacted:
- tool results: text blocks, `structuredContent` (so the client's typed `.data` is redacted too),
  the text and URI of an `EmbeddedResource` holding text, and the name, title, description and URI of
  a `ResourceLink` (`test_tool_text_and_structured_content_are_redacted`,
  `test_embedded_resources_and_resource_links_in_tool_results_are_redacted`);
- resource reads, text contents only (`test_resource_reads_are_redacted`);
- prompt messages, including embedded resources and links, and the prompt description
  (`test_prompt_messages_are_redacted`,
  `test_embedded_resources_and_resource_links_in_prompts_are_redacted`).

Both backends are covered: OpsDesk (MCP over stdio) and Pager (OpenAPI over HTTP,
`test_openapi_backend_results_are_redacted`).

Not redacted:
- elicitation questions in both eras: the 2026 `input_requests[...].params.message` of an
  `InputRequired*` result, and the legacy `elicitation/create` push;
- `notifications/message` log data and progress messages;
- `ResourceLink` icons and annotations, and `meta` on results and content blocks;
- list results (tools, resources, prompts), completions, blob resources, dict keys, image and audio
  blocks, anything the client sends, and JSON-RPC error messages that come back from a backend.

The elicitation gap is deliberate. `InputRequired*` results pass through untouched, because the
question is what the user confirms: redacting "Grant access to jane@example.com?" would make the
confirmation meaningless. Rebuilding one as a plain result would also swallow the question, since its
content is empty (`test_input_required_round_passes_through_untouched`).

The regex runs in linear time (`test_adversarial_inputs_run_in_linear_time`). It redacts the whole
RFC 5322 (Internet Message Format) local part, entered only at the start of its run, and errs
toward over-redaction: in `?to=jane@example.com` the `?to=` goes too. Version specs like
`lodash@4.17.21` are left alone because the top-level domain (TLD) must be letters. IP-literal
domains, dotless hosts, quoted local parts, percent-encoded `@`, punycode TLD tails and TLDs glued
to digits are out of scope (see `gateway/redaction.py`).

## Host and Origin checks
The gateway validates `Host` and `Origin` on each `/<name>/mcp` endpoint and on `GET /healthz`, as the
Streamable HTTP spec requires against DNS rebinding. Each `http_app` is built with
`host_origin_protection="auto"`, and `/healthz` is wrapped in the same FastMCP
`HostOriginGuardMiddleware` in "auto" mode, because it lists the server names. Both answer a foreign
`Host` with 421 and a foreign `Origin` with 403 (`test_a_forged_host_is_rejected`,
`test_a_forged_origin_is_rejected`, `test_a_loopback_request_with_its_own_host_is_served`,
`test_healthz_rejects_a_forged_host`, `test_healthz_rejects_a_forged_origin`,
`test_healthz_serves_a_loopback_request_with_its_own_host`). Unknown paths get a plain 404 and a
non-GET `/healthz` a 405 before any check runs; neither response carries data. When the request's
`Host` is loopback, the guard also accepts any loopback `Origin` on any scheme and port, so a page
served from `http://localhost:3000` passes. A browser still cannot use that: the endpoint takes only
`application/json` bodies, which need a CORS preflight, and the preflight `OPTIONS` gets a 405 with
no `Access-Control-Allow-Origin` header. FastMCP's
"auto" mode checks only requests whose connection arrived on a loopback address (the ASGI `server`
address that uvicorn reports per connection), so with `--host 0.0.0.0` a request over 127.0.0.1 is
still checked while one that arrives on any other interface skips the Host check, and skips the Origin
check unless its Host header is loopback. Binding beyond loopback should therefore wait for M2b auth.

## Pager and registry details
- Pager's `POST /pages` validates its input: a malformed body gets 422 and an unknown service gets
  404, and neither creates a page (`test_bad_page_requests_get_a_detail_and_create_nothing`,
  `test_unknown_service_is_404`).
- An `openapi` source's `spec:` may be a `.json` path; a relative one is resolved against the
  registry file's directory, not the working directory, and read as UTF-8
  (`test_a_relative_spec_path_is_resolved_against_the_registry_directory`,
  `test_audit_loads_a_spec_relative_to_the_registry`). `resolve_targets` and the tests' `pager_mcp`
  build an OpenAPI server through the same `gateway.openapi.openapi_server`, so the Pager the tests
  drive is the Pager `serve` mounts
  (`test_pager_mcp_is_built_the_way_resolve_targets_builds_an_openapi_source`).
- Each source kind has an end-to-end test: stdio in `test_serve_end_to_end_over_a_stdio_backend`,
  openapi in `test_pager_tools_reach_the_http_api`, and http in
  `test_an_http_source_carries_mrtr_and_redaction_to_a_live_backend`, which runs OpsDesk over
  Streamable HTTP as a subprocess and drives an MRTR restart and a redacted result through the proxy.
- `mcp-gateway serve`, the Pager demo and `opsdesk-server` bind through one `listen(host, port)`
  helper (a copy in each package, since they share no code): the address family follows the host, so
  `--host ::1` works and is announced as `http://[::1]:<port>`, and a port in use is a one-line exit
  instead of a traceback (`test_listens_on_an_ipv6_host`,
  `test_pager_reports_a_port_in_use_without_a_traceback`,
  `test_a_port_in_use_is_reported_without_a_traceback`).
- A test pins the OpenAPI spec to the routes, so the MCP tools generated from it cannot drift from
  the API (`test_spec_matches_the_routes_and_success_statuses`).

## Known gaps (the first two are pinned by a characterization test)
- `subscriptions/listen` is not proxied. FastMCP 4.1's low-level server registers no handler, so the
  client gets `Method not found`, and OpsDesk's runbook-updated events never reach it
  (`test_known_gap_subscriptions_listen_is_not_proxied`). A workaround exists (register the SDK's
  `ListenHandler` on the private `_mcp_server` and bridge an upstream listen). Registering the handler
  alone already turns the test red: that mutation was run, and the test fails with `DID NOT RAISE`.
- Upstream cache hints are dropped. OpsDesk answers `tools/list` with `ttlMs: 60000, public`; the
  gateway answers `ttlMs: 0, private` (`test_known_gap_upstream_cache_hints_are_dropped`). The gateway
  could declare its own hint (`create_proxy(..., cache_ttl=60, cache_scope="public")`; that mutation
  turns the test red), but it cannot forward the backend's.
- The registry governs tools only. Resources and prompts of a listed server are ungoverned.
- stdio backends share one process across every caller (see MRTR above).
- Rate limits trust a self-asserted caller name, and a sealed `requestState` can be replayed within
  its 600 seconds. Both wait for M2b.
- `GET /healthz` is unauthenticated and lists the server names. M2b should revisit it.
- Elicitation questions, log notifications and progress messages reach the client unredacted (see
  Redaction scope).
- If one of the pinned tests fails after an upgrade, the gap closed: update this file.

## FastMCP 4.1 gotchas found while building
- Build each proxy with `create_proxy`. A hand-built `ProxyClient` defaults to the legacy era.
- Starlette does not run mounted apps' lifespans: the parent must enter each `http_app().lifespan`, or
  every request returns 500 (`Task group is not initialized`). Yield `None`, because
  `combine_lifespans` yields `{}`, which in-process ASGI harnesses reject.
- An `httpx_client_factory` needs the three Protocol parameters and `**kwargs` (fastmcp passes
  `follow_redirects`).
- A stdio backend inherits stderr, which needs a real file descriptor. Under pytest, capture it with
  `capfd`; with `capsys` the backend fails with a `fileno` error.

## Interview questions to rehearse
1. Why is "unknown tool" the right answer for a disabled tool, and "forbidden" the wrong one?
2. Walk an MRTR call through the gateway. Who holds state between rounds, and what breaks with two
   gateway replicas?
3. A confirmation exchange takes two `tools/call` rounds. How does the limiter count it once, and why
   did counting only calls without `requestState` fail against a replayed state?
4. A transform, middleware or `disable()` for hot reload: what does each cost?
5. What does the gateway's `cacheScope: private` cost clients, and when would `public` be wrong?
6. The registry hides tools. What does it do for resources and prompts, and how would you close that?
