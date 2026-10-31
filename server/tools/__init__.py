"""Turbine Lens tools package — MCP tool server for SQL, Genie, and chart tools."""


def get_tool_server(session_id: str | None = None, mode: str | None = None):
    """Build and return the MCP tool server + allowed tool names.

    Args:
        mode: "agent" or "classic". If None, falls back to global default.
        session_id: What lets the report tool know which conversation to summarise; without
            it that tool is simply not offered.
    """
    from .mcp_tools import get_tool_server as _get

    return _get(session_id=session_id, mode=mode)
