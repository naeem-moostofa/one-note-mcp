"""
FastMCP server instance and ASGI app.

The HTTP-mounted form (`mcp_app`) is what `app/main.py` mounts at `/mcp`. The
`mcp` instance is what `app/mcp/tools.py` decorates via `@mcp.tool`.
"""

from fastmcp import FastMCP

from app.mcp.tool_call_logging import MCPToolCallLoggingMiddleware
from app.mcp.workos_auth import build_mcp_auth


mcp = FastMCP(
    "OneNote MCP",
    # Auth runs before every tool call. Composite: WorkOS AuthKit (web OAuth 2.1
    # clients) + the onmcp_ bearer path (CLI clients) when WorkOS is configured,
    # otherwise the onmcp_ verifier alone. Both converge on the same
    # AccessToken.claims shape that tools read via current_scope().
    auth=build_mcp_auth(),
    instructions=(
        "Read-only access to a user's OneNote notebooks. "
        "Call `onenote_search` with natural content words — it searches every "
        "notebook the user has enabled. Include the notebook, section or page "
        "name in the query whenever you know it: matching a page's breadcrumb "
        "ranks it higher, which is what keeps an unrelated notebook out of the "
        "results. It "
        "returns many short snippets: read them, pick the relevant ones, and "
        "call `onenote_expand_snippets` with their `snippet_id`s to get more "
        "context. Expand rather than re-searching when you have already found "
        "the right page/general area; do not keep searching if you have enough context."
    ),
)
mcp.add_middleware(MCPToolCallLoggingMiddleware())

# Importing the tools module registers the @mcp.tool decorators on the instance
# above. Must come after `mcp` is defined and before `mcp_app` is built.
from app.mcp import tools  # noqa: F401,E402

mcp_app = mcp.http_app(path="/")
