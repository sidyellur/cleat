"""End-to-end test over the real MCP stdio transport (what an agent uses)."""

import asyncio
import json
import os
import shutil
import sys
import time

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import cleat.server as server

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


# -- dead engine recovery (issue #19) ---------------------------------------
# Direct against server._get_engine() (not through the stdio transport): the
# behavior under test is server.py's engine-lifecycle bookkeeping itself, and
# this pins down the exact repro (run_command("exit") kills the shell;
# _get_engine() must notice and hand back a fresh, usable engine instead of
# the same dead one forever).
def test_dead_engine_is_respawned_not_reused_forever(monkeypatch):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not installed")
    monkeypatch.setenv("SHELL", bash)
    server._engine = None
    try:
        eng1 = server._get_engine()
        eng1.run_command("exit")
        # The reader thread notices EOF asynchronously; give it a moment.
        for _ in range(50):
            if not eng1._alive:
                break
            time.sleep(0.05)
        assert not eng1._alive, "engine should be dead after the shell exited"

        eng2 = server._get_engine()
        assert eng2 is not eng1, "a dead engine must be replaced, not reused"
        r = eng2.run_command("echo after-respawn")
        assert r["stdout"] == "after-respawn" and r["exit_code"] == 0
    finally:
        if server._engine is not None:
            server._engine.close()
        server._engine = None
