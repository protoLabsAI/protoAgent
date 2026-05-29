"""Minimal MCP stdio server — for testing protoAgent's MCP client.

Exposes a single ``echo`` tool over stdio using the FastMCP helper that ships
with the ``mcp`` SDK (a transitive dependency of langchain-mcp-adapters). Point
protoAgent at it to confirm MCP discovery end-to-end:

    # config/langgraph-config.yaml (or via the wizard/drawer)
    mcp:
      enabled: true
      servers:
        - name: echo
          transport: stdio
          command: python
          args: ["examples/mcp/echo_server.py"]

The tool then appears to the agent as ``echo__echo`` (tools are namespaced by
server name).
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def echo(text: str) -> str:
    """Echo back the given text — a trivial round-trip to prove the link works."""
    return text


if __name__ == "__main__":
    mcp.run(transport="stdio")
