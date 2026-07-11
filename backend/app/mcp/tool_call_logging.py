"""One structured log event for each authenticated MCP tool call."""

from __future__ import annotations

import json
import time
from typing import Any

import mcp.types as mt
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult


_OMIT_RESULTS_FOR = {"onenote_get_page"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _emit(event: dict[str, Any], level: str) -> None:
    """Print raw JSON so Railway indexes its top-level fields."""
    try:
        print(_json({"message": "MCP tool call", "level": level, **event}), flush=True)
    except Exception:
        # Observability must never break a tool call.
        try:
            print(
                '{"message":"Failed to emit MCP tool-call log",'
                '"level":"error","event":"mcp_tool_call_logging_error"}',
                flush=True,
            )
        except Exception:
            pass


def _semantic_result(result: ToolResult) -> Any:
    """Remove FastMCP's duplicate text copy from the logged result."""
    structured = result.structured_content
    if structured is not None:
        if set(structured) == {"result"}:
            return structured["result"]
        return structured
    return [block.model_dump(mode="json", by_alias=True) for block in result.content]


class MCPToolCallLoggingMiddleware(Middleware):
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        started_at = time.perf_counter()
        access_token = get_access_token()
        user_id = int(access_token.client_id) if access_token and access_token.client_id else None

        event: dict[str, Any] = {
            "event": "mcp_tool_call",
            "timestamp": context.timestamp.isoformat().replace("+00:00", "Z"),
            "user_id": user_id,
            "tool": context.message.name,
            "parameters": context.message.arguments or {},
        }

        try:
            result = await call_next(context)
        except Exception as error:
            event.update(
                status="error",
                duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                error_type=type(error).__name__,
                error_message=str(error),
            )
            _emit(event, "error")
            raise

        try:
            semantic_result = _semantic_result(result)
            wire_json = _json(result.model_dump(mode="json", by_alias=True))

            event.update(
                status="success",
                duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                response_chars=len(wire_json),
                response_bytes=len(wire_json.encode("utf-8")),
                result_count=(
                    len(semantic_result)
                    if isinstance(semantic_result, list)
                    else int(semantic_result is not None)
                ),
            )

            if context.message.name in _OMIT_RESULTS_FOR:
                event["result_omitted"] = True
            else:
                event["result"] = semantic_result

            _emit(event, "info")
        except Exception:
            _emit(
                {
                    "event": "mcp_tool_call_logging_error",
                    "tool": context.message.name,
                    "user_id": user_id,
                },
                "error",
            )

        return result
