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
        "`onenote_search_pages` to find relevant pages within those notebooks, "
        "and `onenote_get_page` to read the full content of a specific page."
    ),
)

# Importing the tools module registers the @mcp.tool decorators on the instance
# above. Must come after `mcp` is defined and before `mcp_app` is built.
from app.mcp import tools  # noqa: F401,E402

mcp_app = mcp.http_app(path="/")
