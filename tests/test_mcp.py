"""End-to-end test over the real MCP stdio transport (what an agent uses)."""

import asyncio
import json
import os
import shutil
import sys

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {"run_command", "send_keys", "read_screen",
                  "resize", "watch_files", "read_output"}


def _text(result):
    return "".join(c.text for c in result.content
                   if getattr(c, "type", "") == "text")


def test_mcp_stdio_roundtrip():
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not installed")
    # Force a supported shell for the server's engine (CI's default may be sh).
    env = dict(os.environ, SHELL=bash)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "cleat.server"], env=env)

    async def run():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = {t.name for t in (await session.list_tools()).tools}
                assert EXPECTED_TOOLS <= tools

                r = json.loads(_text(await session.call_tool(
                    "run_command", {"command": "echo mcp-ok"})))
                assert r["stdout"] == "mcp-ok" and r["exit_code"] == 0

                # persistence across separate tool calls
                await session.call_tool("run_command", {"command": "cd /tmp"})
                r2 = json.loads(_text(await session.call_tool(
                    "run_command", {"command": "pwd"})))
                assert r2["stdout"] in ("/tmp", "/private/tmp")

    asyncio.run(run())
