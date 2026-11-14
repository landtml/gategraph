"""Local stdio MCP server for the A4 tests (brief A4, plan section 1.10).

It sets no environment variables itself. The update-check and banner settings
must arrive from the client side (core/tools/mcp.py passes them to the child
process), and the ``report_env`` tool lets a test confirm that they did.

Run by the tests only. It makes no network calls.
"""
import os

from fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool
def echo(text: str) -> str:
    """Echo the text back."""
    return "echo:" + text


@mcp.tool
def fail(text: str) -> str:
    """Always reports an error."""
    raise ValueError("refused " + text)


@mcp.tool
def report_env(name: str) -> str:
    """Return the value of one environment variable in this server process."""
    return os.environ.get(name, "<unset>")


if __name__ == "__main__":
    mcp.run(show_banner=False)
