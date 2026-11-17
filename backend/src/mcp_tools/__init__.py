"""Tool modules served by the arya-mcp sidecar (src/mcp_server.py).

One module per external system that nobody hosts an MCP server for. Each
exposes `build() -> FastMCP` and takes its per-connection credential from a
request header, so one process serves every saved connection of that type.

Named `mcp_tools`, never `mcp`: that would shadow the installed MCP SDK.
"""
