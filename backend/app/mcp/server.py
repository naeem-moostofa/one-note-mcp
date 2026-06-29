"""
FastMCP server instance and ASGI app.

The HTTP-mounted form (`mcp_app`) is what `app/main.py` mounts at `/mcp`. The
`mcp` instance is what `app/mcp/tools.py` decorates via `@mcp.tool`.
"""

from fastmcp import FastMCP

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
        "Use `onenote_list_notebooks` first to discover notebook IDs, then "
        "use `onenote_search_pages` as the primary way to gather context. "
        "Prefer several targeted or alternate search queries, and increase "
        "`search_size`, `max_pages`, or `max_snippets_per_page` when useful, "
        "before reading full pages. Use `onenote_get_page` sparingly only after "
        "search snippets identify a specific page whose full content is needed."
    ),
)

# Importing the tools module registers the @mcp.tool decorators on the instance
# above. Must come after `mcp` is defined and before `mcp_app` is built.
from app.mcp import tools  # noqa: F401,E402

mcp_app = mcp.http_app(path="/")
