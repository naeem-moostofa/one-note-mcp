# MCP tool-call logging MVP

Status: **implemented**

## Goal

Add enough logging to see how users' agents are using the MCP server and how the tools are
performing.

For every authenticated MCP tool call, log:

- UTC timestamp
- user ID
- tool name
- complete parameters
- complete returned result, except for `onenote_get_page`
- success or error status
- execution duration
- serialized response characters and bytes
- top-level result count

Search queries, snippets, and other tool results are intentionally allowed in the logs for this
MVP. Access to the backend logs is assumed to be secure. `onenote_get_page` is the exception: its
full-page result is omitted because it is predictably large and made the operational logs unusable.

## Event format

Emit one single-line JSON event at info level after each successful tool call:

```json
{
  "message": "MCP tool call",
  "level": "info",
  "event": "mcp_tool_call",
  "timestamp": "2026-07-11T18:24:31.482Z",
  "user_id": 17,
  "tool": "onenote_search_pages",
  "parameters": {
    "query": "NFA to DFA conversion",
    "notebook_ids": [3],
    "search_size": 80,
    "max_pages": 10,
    "max_snippets_per_page": 5
  },
  "status": "success",
  "duration_ms": 134.7,
  "response_chars": 12640,
  "response_bytes": 12704,
  "result_count": 1,
  "result": [
    {
      "page_id": 27,
      "page_title": "Module 5 Part 1",
      "snippets": ["..."]
    }
  ]
}
```

For a failed call, emit the same event at error level with:

```json
{
  "status": "error",
  "error_type": "ToolError",
  "error_message": "Page 27 is outside this connection's scope"
}
```

There should be one event per call, not separate start and finish events.

## Implementation

### 1. Add MCP logging middleware

Create `backend/app/mcp/tool_call_logging.py` with a custom FastMCP middleware implementing
`on_call_tool`.

Use the middleware available in the pinned FastMCP 3.2.4 version:

```python
from fastmcp.server.middleware import Middleware, MiddlewareContext
```

The middleware should:

1. Capture the UTC start timestamp and start a monotonic timer.
2. Read the tool name and full parameters from `context.message`.
3. Read the authenticated `AccessToken` and extract `user_id` from
   `AccessToken.client_id`.
4. Run `await call_next(context)`.
5. On success, log `ToolResult.structured_content` as the complete semantic result, unwrapping
   FastMCP's standard `{ "result": ... }` envelope and without also logging its duplicate text
   representation. Fall back to `ToolResult.content` when no structured result exists. Emit one
   JSON info log. For
   `onenote_get_page`, omit the result and emit `"result_omitted": true` instead.
6. On failure, emit one JSON error log containing the exception type and message, then re-raise the
   original exception so MCP behavior is unchanged.

Use `time.perf_counter()` for `duration_ms`. Use Pydantic's JSON-compatible serialization for the
tool result so response models, lists, and MCP content blocks are handled consistently.

Keep logging best-effort: if event serialization itself fails, emit a small fallback error without
changing the result returned to the MCP client.

### 2. Register the middleware

In `backend/app/mcp/server.py`, register the middleware once after constructing `FastMCP` and before
building `mcp_app`:

```python
mcp.add_middleware(MCPToolCallLoggingMiddleware())
```

This automatically covers all current tools and any tools added later without modifying individual
functions in `backend/app/mcp/tools.py`.

Emit the event directly to stdout as one compact JSON line. Do not pass it through the application's
normal Python/Uvicorn formatter because that adds a text prefix before `{` and causes Railway to
store the JSON as an unqueryable message string. Every event must have top-level `message` and
`level` fields so Railway recognizes and indexes it as a structured log.

For successful calls, also serialize the complete FastMCP `ToolResult` to calculate
`response_chars` and UTF-8 `response_bytes`. These measure the complete MCP response, including the
wire representation, even when `onenote_get_page` content is omitted from the log. Set
`result_count` to the length of a top-level list result, zero for no result, or one for a single
object.

No new database table, logging dependency, or configuration setting is needed for the MVP.

## Why this uses FastMCP middleware

`MCPToolCallLoggingMiddleware` is FastMCP operation middleware, not FastAPI HTTP middleware. Its
`on_call_tool` hook runs only for MCP `tools/call` operations and does not affect REST routes, OAuth
routes, sync workers, or unrelated application logs.

This is preferable to putting a decorator on each tool because:

- Logging is a cross-cutting concern that applies consistently to every current and future MCP tool.
- Middleware sees the authenticated FastMCP context and the final canonical `ToolResult` used to
  calculate the actual MCP response size.
- It records success and exceptions from one place without repeating `try`/`except`/timing code in
  every tool.
- A decorator would wrap the Python function before FastMCP builds the final `ToolResult`, so it
  would measure the raw return value rather than the complete MCP response.
- Tool decorators must preserve function signatures for FastMCP dependency injection and schema
  generation, which adds avoidable failure risk.

Keep per-tool differences as small policy rules inside the middleware. The current example is
`onenote_get_page`, whose result is measured but omitted from the log. A decorator would be more
appropriate only for a domain event unique to one tool, not for the common tool-call audit event.

## Querying production logs with Railway

Run these commands from the linked project directory. Specify service and environment explicitly
when there is any chance the local Railway link points somewhere else. The examples are written out
in full so they are easy to copy independently.

### View MCP tool calls

All MCP calls from the last 24 hours:

```powershell
railway logs --service backend --environment production --since 24h `
  --filter "@event:mcp_tool_call"
```

Only searches:

```powershell
railway logs --service backend --environment production --since 24h `
  --filter "@event:mcp_tool_call AND @tool:onenote_search_pages"
```

One user's calls:

```powershell
railway logs --service backend --environment production --since 24h `
  --filter "@event:mcp_tool_call AND @user_id:1"
```

Errors, slow calls, and large responses:

```powershell
railway logs --service backend --environment production --since 24h `
  --filter "@event:mcp_tool_call AND @status:error"

railway logs --service backend --environment production --since 24h `
  --filter "@event:mcp_tool_call AND @duration_ms:>500"

railway logs --service backend --environment production --since 24h `
  --filter "@event:mcp_tool_call AND @response_bytes:>10000"
```

Railway performs these attribute and numeric filters server-side. The application must continue to
emit a complete JSON object on one line with no Python logging prefix; otherwise Railway stores the
JSON as an unqueryable message string.

### Count and summarize calls without printing full results

The Railway CLI filters and returns log events but does not aggregate or project columns itself.
Use `--json` and PowerShell for that. `--since` is important because it fetches a bounded historical
window instead of opening an endless log stream.

Load all MCP events without printing them:

```powershell
$events = @(
  railway logs --service backend --environment production --since 24h `
    --filter "@event:mcp_tool_call" --json |
    ForEach-Object { $_ | ConvertFrom-Json }
)
```

Total tool calls:

```powershell
$events.Count
```

Calls grouped by tool:

```powershell
$events | Group-Object tool | Sort-Object Count -Descending |
  Select-Object Name, Count
```

Compact call list that does not display `parameters` or `result`:

```powershell
$events | Select-Object timestamp, user_id, tool, status, duration_ms,
  response_bytes, result_count
```

Latency summary:

```powershell
$events | Measure-Object duration_ms -Average -Minimum -Maximum
```

Ten largest responses:

```powershell
$events | Sort-Object response_bytes -Descending |
  Select-Object -First 10 timestamp, user_id, tool, response_bytes,
    result_count, duration_ms
```

Search-call count and average latency only:

```powershell
$searches = @(
  railway logs --service backend --environment production --since 24h `
    --filter "@event:mcp_tool_call AND @tool:onenote_search_pages" --json |
    ForEach-Object { $_ | ConvertFrom-Json }
)

$searches.Count
$searches | Measure-Object duration_ms -Average -Minimum -Maximum
```

Use `--since 1h`, `1w`, or an ISO-8601 timestamp to change the window. Add `--until` when comparing a
fixed incident period. Railway log retention depends on the account plan, so these logs are for
operational investigation rather than permanent analytics.

## Production verification

After deployment:

1. Call each MCP tool once from a real agent.
2. Confirm Railway shows one JSON event per call.
3. Confirm each event contains the expected user, tool, parameters, result, duration, response size,
   and result count, with the result present only once.
4. Trigger one known tool error and confirm it is logged while the MCP client still receives the
   expected error.
5. Confirm `onenote_get_page` records `result_omitted: true` and does not include page content.

## Acceptance criteria

- Every authenticated MCP tool call produces exactly one completion event.
- Successful calls are logged at info level; failed calls are logged at error level.
- Logs contain the user ID, timestamp, tool, complete parameters, status, and duration.
- Railway parses `event`, `tool`, `status`, `duration_ms`, `response_chars`, `response_bytes`, and
  `result_count` as queryable top-level attributes.
- Results are logged in full for every tool except `onenote_get_page`, which records
  `result_omitted: true`.
- Tool errors and unexpected exceptions are logged and retain their existing client behavior.
- Logging failures never fail a tool call.
- Manual verification confirms the existing MCP tools continue to work.

## Later improvements, only if needed

- Session IDs and authentication-method fields.
- Payload truncation or redaction.
- Log retention controls.
- Dashboards, metrics, alerts, or tracing.
- A dedicated audit-log store.
