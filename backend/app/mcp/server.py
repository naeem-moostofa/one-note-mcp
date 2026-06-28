"""
FastMCP server instance and ASGI app.

The HTTP-mounted form (`mcp_app`) is what `app/main.py` mounts at `/mcp`. The
`mcp` instance is what `app/mcp/tools.py` decorates via `@mcp.tool`.
"""

from fastmcp import FastMCP

from app.mcp.auth import MCPConnectionTokenVerifier


mcp = FastMCP(
    "OneNote MCP",
    # Verifier runs before every tool call — authenticates the bearer token
    # against the mcp_connections table and stashes the resolved scope in
    # AccessToken.claims for tools to read via current_scope().
    auth=MCPConnectionTokenVerifier(),
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
