# PLAN: `wait_for` — block until the session needs attention

> Thesis: v0.2 gave cleat a protocol-level `state` field so agents don't have
> to guess whether a command finished. But every tool still returns
> immediately, so an agent watching a long-runner has to build its own
> polling loop around `read_output` and guess a sleep interval between calls.
> `wait_for` closes that gap: one blocking call that returns the instant the
> session actually needs attention, with zero polling.
>
> This file is self-contained: an implementer needs this file plus the
> existing source. Read `engine.py` (especially `_probe_state`, `_cond`,
> `_read_until_idle`, and `read_output`) before starting — `wait_for` reuses
> all of that machinery; it does not introduce a new mechanism.

## Why

`run_command`/`read_output` already carry `state`, computed from termios
flags and the foreground process group (see `PLAN.md` for that rationale).
But an agent that wants to wait *longer* than a single call's timeout has no
way to do so except calling `read_output` again and again, picking its own
poll interval — exactly the "guessing" cleat's state oracle was built to
remove, just moved to the client side.

`Engine` already has the primitive needed to fix this: `_cond`, a
`threading.Condition` that `_read_loop` notifies on every chunk of PTY
output. `read_output`/`run_command` already wait on it internally. `wait_for`
is a thin new entry point onto the same condition variable: block until
`_probe_state()` is no longer `"running"`, instead of blocking until output
goes idle for a fixed window.

## API

```
wait_for(timeout: float = 30.0) -> dict
```

Returns `{"output": str, "exit_code": int|None, "completed": bool,
"state": str}` (+ `"spoofed_marks"` when > 0) — the same shape as
`read_output`'s result.

Behavior:
- If the session is already not `"running"` (idle, awaiting-input, password,
  tui) when called, returns immediately — no wait, no polling delay. This
  matters right after `run_command`/`send_keys` already left the session in
  one of those states.
- Otherwise blocks, waking on every `_cond.notify_all()` (i.e. every chunk of
  PTY output), re-checking `_probe_state()` each time, until it differs from
  `"running"` or `timeout` elapses.
- `output` is the raw output accumulated since the caller's last read (same
  cursor bookkeeping as `read_output`/`run_command` — `_drain()`), ANSI-
  stripped via `_clean`.
- `completed`/`exit_code` follow the exact same rule `read_output` uses:
  `done = self._struct.idle and self._rec_total() > 0`.
- Never busy-polls: no new timer, no new heuristic. It's `_probe_state()` +
  the existing `_cond`, wired the way `_read_until_idle` already is.

## Explicit non-goals (do not build these — this is where scope creep lives)

- **No output pattern matching** (e.g. "wait until stdout matches /regex/").
  That reintroduces the exact fragile-heuristic problem the state oracle
  replaced — an agent-authored guess about output shape instead of a
  protocol-level fact. If someone wants this later, it's a separate,
  explicitly-justified proposal, not a parameter bolted on here.
- **No target-state parameter** (e.g. `wait_for(target="idle")`). Waiting for
  "not running" covers every case that matters: a finished command (`idle`),
  a REPL/prompt that needs `send_keys` (`awaiting-input`), a secret prompt
  that needs a human (`password`), or a TUI that needs `read_screen`/
  `send_keys` (`tui`). Adding a target-state filter is one line of code but
  opens the door to "wait for state X or Y", exit-code targets, etc. — keep
  it to the one signal that's actually needed.
- **No cancellation.** MCP tool calls are synchronous request/response over
  stdio; `timeout` is the only bound, exactly like every other tool here.

If real usage later proves one of these is needed, that's a new, small,
separately-justified PLAN — not scope added to this one.

## Tasks

### T1. `engine.py` — the `wait_for` method

Add, alongside `read_output` (same `@_serialized` pattern, same
`if not self._alive: raise RuntimeError(...)` guard):

```python
@_serialized
def wait_for(self, timeout=30.0) -> dict:
    """Block until the session needs attention - state leaves "running" -
    or `timeout` elapses. Returns {output, exit_code, completed, state},
    the same shape as read_output(), but waits on the state oracle
    directly instead of an idle-silence window: no polling, no guessed
    intervals. Returns immediately if the session isn't "running" when
    called (e.g. already sitting at a REPL prompt)."""
    if not self._alive:
        raise RuntimeError("engine not started (or already closed)")
    with self._cond:
        end = time.monotonic() + timeout
        while self._alive and self._probe_state() == "running":
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            self._cond.wait(remaining)
        raw = self._drain()
        done = self._struct.idle and self._rec_total() > 0
        exit_code = self._records[-1].exit_code if done else None
        return self._augment({"output": _clean(raw), "exit_code": exit_code,
                               "completed": done})
```

Notes for the implementer:
- This is intentionally almost identical to `read_output`'s tail (the
  `done`/`exit_code`/return line is copied verbatim) — that consistency is
  the point; don't invent a different completion rule for this method.
- `_probe_state()` already requires the caller to hold `_cond` (see its
  docstring) — the `with self._cond:` block satisfies that.
- Do not touch `_read_until_idle`, `_probe_state`, or any other method.
  This task is additive only.

### T2. `server.py` — expose the MCP tool

Add a `@mcp.tool()` wrapping it, following the existing docstring style
(see `read_output`'s tool docstring for the pattern to match):

```python
@mcp.tool()
def wait_for(timeout: float = 30.0) -> dict:
    """Block until the session needs attention instead of polling for it.

    Use this in place of a read_output() loop when a command might run
    longer than one call's timeout: it returns the instant the state leaves
    "running" - the command finished, a REPL/prompt is waiting for input,
    a password prompt appeared, or a TUI took over - with no guessed poll
    interval. Returns immediately if the session isn't "running" already.
    Returns {output, exit_code, completed, state}; same shape as
    read_output. See that tool's docstring for what each `state` value
    implies you should do next.

    Args:
        timeout: max seconds to block before returning completed=False /
            state="running" (default 30 - longer than read_output's default
            since the point of this tool is to wait out a long-runner).
    """
    return _get_engine().wait_for(timeout=timeout)
```

### T3. Tests

`tests/test_engine.py` (follow the existing fixture conventions —
`eng`/`bash_eng` — already in that file; no new fixtures needed):

- `test_wait_for_returns_immediately_when_already_idle` — run a quick
  command, then `wait_for(timeout=5)` should return in well under the
  timeout (assert `elapsed < 1.0`, same pattern as
  `test_read_output_after_completion_reports_done_promptly`), with
  `completed=True` and the right `exit_code`.
- `test_wait_for_blocks_until_long_command_completes` — start `sleep 1;
  echo woke` with a short `run_command` timeout (so it returns
  `completed=False`), then `wait_for(timeout=5)` should block until it
  actually finishes and return `completed=True, exit_code=0,
  state="idle"`, with `"woke"` in `output`. Assert it took at least ~1s
  (proves it actually waited, didn't just return the initial partial
  output) and well under the 5s timeout (proves it didn't just wait out the
  clock).
- `test_wait_for_returns_on_repl_prompt` (skip on fish, matching
  `test_python_repl_interactive`'s skip) — `run_command("python3",
  timeout=1)` (short enough that the banner may not have fully settled),
  then `wait_for(timeout=10)` should return once `state == "awaiting-input"`
  with the `>>>` prompt in `output`/accumulated stdout; clean up with
  `send_keys("exit()", enter=True)`.
- `test_wait_for_times_out_while_still_running` — `wait_for` on a session
  mid-`sleep 5` with `timeout=0.5` returns `completed=False,
  state="running"` promptly (assert `elapsed < 1.5`, i.e. it honored the
  short timeout and didn't block for the full 5s).
- `test_wait_for_raises_if_not_started` — mirror whatever existing test (if
  any) covers this for `read_output`; if none exists, a simple
  `Engine(...)` without `.start()` should raise `RuntimeError` from
  `wait_for()`.

Also update the `__main__` self-test block at the bottom of `engine.py`:
add a `check(...)` case exercising `wait_for` on a `sleep 1; echo woke`
long-runner (mirroring the existing `slow-but-completes` case's setup) to
prove it blocks and returns `completed=True`.

### T4. Docs

- README: add `wait_for` to the tools table, one line, pointing at
  `read_output` for the polling pattern it replaces.
- No version bump needed for a single additive tool (contrast with v0.2,
  which changed every tool's response shape) — use judgment here; if the
  implementer disagrees, a patch bump (`0.2.1`) is fine.

## Acceptance criteria

- [ ] `wait_for(timeout=N)` returns immediately (no wait) when the session
      is already not `"running"`.
- [ ] `wait_for` blocks past `run_command`'s own timeout and correctly
      reports completion for a command that finishes later.
- [ ] `wait_for` returns as soon as state becomes `awaiting-input` /
      `password` / `tui`, without waiting for `timeout`.
- [ ] `wait_for` honors `timeout` and returns `completed=False,
      state="running"` promptly when the command is still running.
- [ ] No new heuristic, no output pattern matching, no target-state
      parameter — scope stays exactly as specified above.
- [ ] `python -m pytest` green; `python -m cleat.engine` self-test ALL PASS.

## Out of scope (deliberately — do not build these here)

Output pattern matching, target-state parameter, cancellation,
multi-session `wait_for`, secret redaction, audit log, Windows, SSH mark
re-injection (all still open from `PLAN.md`'s original out-of-scope list).
