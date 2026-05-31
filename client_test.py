#!/usr/bin/env python3
"""Smoke test: drive the cleat MCP server over the REAL stdio transport.

    python client_test.py    # requires `pip install -e .`
"""

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _text(result):
    return "".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "cleat.server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])

            r = await session.call_tool(
                "run_command", {"command": "echo live over stdio; uname -s"})
            print("\nrun #1:\n" + _text(r))

            # Persistence across SEPARATE MCP tool calls - the subprocess-can't-do-this bit.
            await session.call_tool("run_command", {"command": "export FOO=persisted"})
            await session.call_tool("run_command", {"command": "cd /tmp"})
            r2 = await session.call_tool("run_command", {"command": "echo $FOO; pwd"})
            print("\nrun #2 (after export+cd in earlier calls):\n" + _text(r2))

            r3 = await session.call_tool("run_command", {"command": "false"})
            print("\nrun #3 (failure):\n" + _text(r3))

            # Interactive: start a REPL (won't 'complete'), compute, exit.
            r4 = await session.call_tool("run_command", {"command": "python3", "timeout": 8})
            print("\nrun #4 (repl start, completed=False expected):\n" + _text(r4)[-80:])
            r5 = await session.call_tool("send_keys", {"keys": "print(6*7)", "enter": True})
            print("\nrun #5 (send_keys -> 42 expected):\n" + _text(r5))
            r6 = await session.call_tool("send_keys", {"keys": "exit()", "enter": True})
            print("\nrun #6 (repl exit, completed=True expected):\n" + _text(r6))

            # Full-screen TUI over MCP: open vim, SEE it via read_screen, quit it.
            await session.call_tool("run_command", {"command": "vim -u NONE -N", "timeout": 4})
            r7 = await session.call_tool("read_screen", {})
            print("\nrun #7 (read_screen of vim, '~' rows expected):\n" + _text(r7)[:200])
            r8 = await session.call_tool("send_keys", {"keys": ":q!", "enter": True})
            print("\nrun #8 (vim quit, completed=True expected):\n" + _text(r8)[-120:])


if __name__ == "__main__":
    asyncio.run(main())
