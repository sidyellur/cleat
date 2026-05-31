# cleat

**A headless terminal layer for AI agents.** `cleat` runs a *persistent* shell
session behind a PTY, parses its byte stream for [OSC 133](https://gitlab.freedesktop.org/Per_Bothner/specifications/blob/master/proposals/semantic-prompts.md)
shell-integration marks, and exposes it to an agent over [MCP](https://modelcontextprotocol.io)
as **structured results** — `stdout`, real `exit_code`, files touched — instead
of raw escape-code soup.

It is *not* a terminal emulator and *not* a modification to your terminal. It's
a separate process that runs inside whatever terminal you already use, and it
stays terminal-agnostic.

## Why

When an agent drives a terminal, it normally sees a continuous byte river:
prompt redraws, echoed keystrokes, color codes, and output, with no marker for
where one command's output ends or whether it succeeded. Worse, when you scrape
a PTY, **the exit code isn't in the stream at all** — the shell knows `$?` but
never prints it.

`cleat` injects OSC 133 marks into the shells it spawns and parses them back
out, so the agent gets:

```json
{ "stdout": "...", "exit_code": 0, "completed": true }
```

…and the session is **persistent**: `cd`, `export`, activated venvs, and `ssh`
sessions all carry across calls — something a fresh `subprocess` per command
cannot do.

## Install

Requires Python ≥3.10 on a POSIX system (Linux/macOS). With [uv](https://docs.astral.sh/uv/) installed, register it with Claude Code:

```sh
claude mcp add cleat -- uvx --from git+https://github.com/sidyellur/cleat cleat
```

Or add it to a project's `.mcp.json`:

```json
{
  "mcpServers": {
    "cleat": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/sidyellur/cleat", "cleat"]
    }
  }
}
```

For local development:

```sh
git clone https://github.com/sidyellur/cleat && cd cleat
python -m venv .venv && . .venv/bin/activate
pip install -e .
cleat            # runs the MCP server over stdio
```

## Tools

| Tool | Returns | Use for |
|------|---------|---------|
| `run_command(command, timeout)` | `{stdout, exit_code, completed}` (+ `files_changed` if watching) | normal commands — full, exact stdout |
| `read_output(timeout)` | `{output, exit_code, completed}` | watching a long-running / streaming command |
| `read_screen()` | `{screen, cursor}` | inspecting a full-screen TUI (vim, top, less) |
| `send_keys(keys, enter)` | `{screen, cursor, exit_code, completed}` | driving a REPL / TUI / prompt (control chars pass through: ``=Ctrl-C) |
| `resize(cols, rows)` | `{cols, rows}` | laying out a TUI for a given size |
| `watch_files(path)` | `{watch_root}` | enable per-command `files_changed` under `path` |

`completed: false` means the program is still running or waiting for input (e.g.
a REPL) — drive it with `send_keys` and poll with `read_output`.

## How it works

```
agent ─(MCP)─ server.py ─ engine.py (persistent PTY, ptyprocess)
                            ├─ structure.py  → OSC 133 marks → {stdout, exit_code}
                            └─ pyte screen    → rendered view for REPLs/TUIs
```

The engine injects OSC 133 per shell without touching your real config: zsh via
a temp `$ZDOTDIR`, bash via `--rcfile` + vendored [bash-preexec](https://github.com/rcaloras/bash-preexec),
fish via its native shell integration (fish ≥4) or an injected init (older fish).

## Caveats

- **POSIX only.** Uses `pty`/`termios`; no Windows.
- **It gives the agent a real shell.** Commands run with your user's privileges
  in a persistent session. Run it only where you'd let an agent run shell
  commands.
- **`files_changed` detects writes, not reads** (create/modify/delete under the
  watched root). Read-tracking needs privileged syscall tracing.
- **bash:** a command whose *first token* is a subshell `(...)` or brace group
  `{ ...; }` emits no start mark; its exit code is recovered but stdout for that
  one command is not captured.
- **Full-screen TUIs:** use `read_screen` (the rendered grid), not `run_command`.

## License

MIT (see [LICENSE](LICENSE)). Vendors bash-preexec, also MIT.
