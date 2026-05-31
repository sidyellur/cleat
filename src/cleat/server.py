#!/usr/bin/env python3
"""
server.py - the MCP server. The agent-facing edge.

Wraps the persistent Engine in an MCP tool so an agent gets the clean sticky
note - {stdout, exit_code} - instead of the raw byte river. Reuses the official
`mcp` SDK (FastMCP); the only bespoke part here is the tool schema, which is
deliberately minimal: one persistent shell, one tool to run a command in it.

Run it as an MCP stdio server:

    cleat                      # installed entry point
    python -m cleat.server     # or as a module

Or point an MCP client / Claude Code at that command.
"""

import atexit

from mcp.server.fastmcp import FastMCP

from .engine import Engine

mcp = FastMCP("cleat")

_engine = None


def _get_engine() -> Engine:
    """Lazily start the persistent shell on first use, then reuse it."""
    global _engine
    if _engine is None:
        _engine = Engine().start()
    return _engine


@atexit.register
def _shutdown():
    """On server exit, terminate the shell and clean up its temp inject dir."""
    if _engine is not None:
        _engine.close()


@mcp.tool()
def run_command(command: str, timeout: float = 10.0) -> dict:
    """Run a shell command in a PERSISTENT terminal session.

    The session keeps state across calls: cd, exports, activated venvs, and ssh
    sessions all persist. Returns {stdout, exit_code, completed}:
      - completed=True : the command finished; exit_code is real (from OSC 133
        marks - information not otherwise present in a scraped terminal stream).
      - completed=False: the command is still running or waiting for input (e.g.
        a REPL like `python3`, or an interactive prompt). stdout is the output so
        far; drive it with send_keys() and poll with read_output().

    Args:
        command: the shell command to run.
        timeout: seconds to wait for completion before returning partial (default 10).
    """
    return _get_engine().run_command(command, timeout=timeout)


@mcp.tool()
def send_keys(keys: str, enter: bool = False, timeout: float = 2.0) -> dict:
    """Send input to a running interactive program (a REPL, a prompt, a TUI)
    started by run_command, then return the rendered screen.

    Control characters pass through: "\\u0003"=Ctrl-C, "\\u0004"=Ctrl-D,
    "\\u001b"=Esc. Set enter=True to append a newline. Returns {screen, cursor,
    exit_code, completed}; the screen is rendered by a virtual terminal so
    REPL/TUI output is clean (no per-keystroke redraw noise). completed=True
    (with an exit_code) means the program exited.
    """
    return _get_engine().send_keys(keys, enter=enter, timeout=timeout)


@mcp.tool()
def read_screen() -> dict:
    """Return what the terminal currently LOOKS LIKE - the virtual screen
    rendered by a terminal emulator - plus the cursor [x, y]. Use this to
    inspect a full-screen TUI (vim, top, less) or an interactive prompt.

    Returns {screen, cursor}. Note the screen is the visible grid only; for long
    scrolling output you want captured in full, use run_command / read_output.
    """
    return _get_engine().read_screen()


@mcp.tool()
def resize(cols: int, rows: int) -> dict:
    """Resize the terminal (PTY + virtual screen). Use before/while driving a
    full-screen TUI so it lays out for the size you want to read. Returns the
    new {cols, rows}.
    """
    return _get_engine().resize(cols, rows)


@mcp.tool()
def watch_files(path: str) -> dict:
    """Enable the files-touched feature: after this, run_command results include
    files_changed = {created, modified, deleted} for files under `path`. Pass a
    specific PROJECT directory (not your home dir). Pass "" to disable.

    Detects WRITES only (create/modify/delete), not reads - tracking reads needs
    privileged syscall tracing. Returns {watch_root}.
    """
    return _get_engine().set_watch_root(path)


@mcp.tool()
def read_output(timeout: float = 2.0) -> dict:
    """Poll the session for new raw text output without sending anything - e.g.
    to watch a long-running command or streaming build log (not truncated to the
    screen). Returns {output, exit_code, completed}; exit_code is set if a
    command finished while reading. For TUIs, prefer read_screen.
    """
    return _get_engine().read_output(timeout=timeout)


def main():
    """Console-script entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
