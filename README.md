# cleat

[![CI](https://github.com/sidyellur/cleat/actions/workflows/ci.yml/badge.svg)](https://github.com/sidyellur/cleat/actions/workflows/ci.yml)

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
{ "stdout": "...", "exit_code": 0, "completed": true, "state": "idle" }
```

…and the session is **persistent**: `cd`, `export`, activated venvs, and `ssh`
sessions all carry across calls — something a fresh `subprocess` per command
cannot do.

## Session state

Every tool response carries a `state` field, derived from termios flags and
the foreground process group — facts only the process holding the PTY can
read — instead of guessed from output timing:

| `state` | Meaning | What to do |
|---------|---------|------------|
| `idle` | nothing is running | call `run_command` for the next thing |
| `running` | a command is executing | poll `read_output` |
| `awaiting-input` | a REPL/prompt is blocked on stdin | drive it with `send_keys` |
| `password` | a secret prompt (`sudo`, `read -s`) is waiting, echo off | **stop** — only send input here with the human's explicit consent |
| `tui` | a full-screen program (vim, top, less) owns the terminal | use `read_screen`/`send_keys`, not `run_command` |

## Install

Requires Python ≥3.10 on a POSIX system (Linux/macOS). `cleat` is on
[PyPI](https://pypi.org/project/cleat/).

Register it with Claude Code — with [uv](https://docs.astral.sh/uv/) (no install step needed):

```sh
claude mcp add cleat -- uvx cleat
```

…or if you'd rather install it into your environment first:

```sh
pip install cleat
claude mcp add cleat -- cleat
```

Either way, you can add it to a project's `.mcp.json`:

```json
{
  "mcpServers": {
    "cleat": {
      "command": "uvx",
      "args": ["cleat"]
    }
  }
}
```

To run the latest unreleased code straight from git instead of PyPI, use
`uvx --from git+https://github.com/sidyellur/cleat cleat`.

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
| `run_command(command, timeout)` | `{stdout, exit_code, completed, state}` (+ `files_changed` if watching, `spoofed_marks` if tampered) | normal commands — full, exact stdout |
| `read_output(timeout)` | `{output, exit_code, completed, state}` | watching a long-running / streaming command |
| `wait_for(timeout)` | `{output, exit_code, completed, state}` | blocking until the session needs attention — replaces a `read_output` polling loop |
| `read_screen()` | `{screen, cursor, state}` | inspecting a full-screen TUI (vim, top, less) |
| `send_keys(keys, enter)` | `{screen, cursor, exit_code, completed, state}` | driving a REPL / TUI / prompt (control chars pass through: ``=Ctrl-C) |
| `resize(cols, rows)` | `{cols, rows}` | laying out a TUI for a given size |
| `watch_files(path)` | `{watch_root}` | enable per-command `files_changed` under `path` |

`completed: false` means the program is still running or waiting for input (e.g.
a REPL) — check `state` (above) for what to do next.

## How it works

```
agent ─(MCP)─ server.py ─ engine.py (persistent PTY, ptyprocess)
                            ├─ structure.py  → OSC 133 marks → {stdout, exit_code, state}
                            └─ pyte screen    → rendered view for REPLs/TUIs
```

The engine injects OSC 133 per shell without touching your real config: zsh via
a temp `$ZDOTDIR`, bash via `--rcfile` + vendored [bash-preexec](https://github.com/rcaloras/bash-preexec),
and fish via `fish -C 'source ...'` (fish ≥4 also emits its own native marks
alongside ours; see below for why that’s harmless).

### Nonce-authenticated marks

A command you run can print arbitrary bytes to its own stdout — including a
fake `ESC ]133;D;0 BEL`, forging a clean exit code for output that actually
failed. `cleat` closes this: each session gets a fresh `secrets.token_hex(8)`
nonce at startup, embedded in every mark cleat injects
(`\033]133;D;<exit>;k=<nonce>\007`). A mark whose `k=` param is missing or
wrong is ignored outright — it cannot alter `exit_code`, `stdout`, or
`completed` either way. A *wrong* `k=` is always surfaced as an attempted
forgery; a *missing* `k=` is too, unless the shell is known to run its own
legitimate native OSC 133 alongside ours (fish ≥4) — that's expected
telemetry, not tampering, so it's ignored quietly instead of triggering a
false alarm on every fish command. `run_command` surfaces the real count as
`spoofed_marks` in its result (only when it's nonzero), so an agent can tell
when a program it ran tried to lie about how it finished.

The nonce itself is embedded in a temp rcfile at session startup (bash's
`--rcfile`, fish's `-C 'source ...'`, zsh's `$ZDOTDIR`) — its path stays
visible in the shell's own argv for the life of the process. `cleat` removes
that file from disk as soon as the shell has read it (before it ever shows a
prompt), so a same-uid child process spawned later in the session can't open
the path and read the nonce out, even if it discovers it via
`/proc/<pid>/cmdline`.

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
- **Shells:** zsh and bash are fully supported, including interactive REPL/TUI
  driving. fish is supported for command execution on **≥4** (its own native
  marks require that version; cleat's injection works alongside them either
  way — see [Nonce-authenticated marks](#nonce-authenticated-marks)).

## License

MIT (see [LICENSE](LICENSE)). Vendors bash-preexec, also MIT.
