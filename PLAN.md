# PLAN: v0.2 — session-state oracle + nonce-authenticated marks

> Thesis: **"cleat doesn't guess."** Every tool response carries a protocol-level
> `state` field derived from facts only the PTY owner can read, and OSC 133
> marks are authenticated so a program cleat runs cannot forge exit codes.
>
> This file is the execution plan for that release. It is self-contained: an
> agent picking this up needs this file plus the existing source. Read
> `engine.py`, `structure.py`, and `inject.py` in full before starting.

## Why (context for the implementer)

A mid-2026 survey of the agent-terminal landscape (ht, tmux MCPs, Desktop
Commander, wcgw, Claude Code's Bash tool, Codex, Cursor, Warp, E2B, …) found:

- "Is the command done / still running / waiting for input?" is guessed by
  **every** tool via idle timers or prompt regexes, and every one documents the
  guesses failing (agents hang on pagers, Ctrl-C working commands, poll forever).
  cleat's own issue #1 was this failure class.
- Terminal output is an attack surface: any child process can emit a fake
  `ESC ]133;D;0 BEL` and forge a successful exit code. VS Code solved the same
  problem for its OSC 633 sequences with a session nonce; cleat has no defense
  today.

cleat owns the PTY master, so it can replace the guessing with **syscall-level
facts**: termios flags, the foreground process group, and alternate-screen
state. No other MCP terminal tool reports these.

## The state model

Every tool response gains one new field:

```
"state": "idle" | "running" | "awaiting-input" | "password" | "tui"
```

### Signals (read at response time, under `_cond`)

| Signal | How | Meaning |
|---|---|---|
| `fg` | `os.tcgetpgrp(self._proc.fd)` compared to the shell's pid (shell is session leader, so its pgid == its pid) | shell at prompt vs a child controls the terminal |
| `echo`, `icanon` | `termios.tcgetattr(self._proc.fd)` → lflag bits `ECHO`, `ICANON` (master fd reflects the shared pty termios on Linux and macOS) | password prompt / raw-mode line editor |
| `altscreen` | boolean tracked in `_read_loop` by scanning for `ESC[?1049h/l`, `ESC[?1047h/l`, `ESC[?47h/l` | full-screen TUI active |
| `marks idle` | existing `StructureSource.idle` | C→D cycle open or closed |

### Derivation (first match wins — ORDER IS LOAD-BEARING)

1. `idle` — `fg == shell pid` **and** marks idle.
   Must be first: zsh's ZLE / bash's readline put the terminal in raw mode
   *at the prompt*, so an ICANON check before this would misread a prompt as
   `awaiting-input`.
2. `tui` — `altscreen` is true. Agent should use `read_screen` / `send_keys`.
3. `password` — ECHO off, ICANON on (`sudo`, `read -s`, `getpass`). The one
   state where the agent should stop and ask the human.
4. `awaiting-input` — ICANON off (and not alt-screen). A readline/libedit line
   editor is provably blocked waiting for input (python3, node, psql, …).
5. `running` — a child owns the terminal and none of the above matched.
   Honest residue: a canonical-mode reader (`cat` waiting on stdin) is
   indistinguishable from a busy program by termios alone; the existing
   idle-timer heuristic stays as the fallback for that case only.

Degradation: if `tcgetpgrp`/`tcgetattr` raise (`OSError`, `termios.error`),
fall back to `"idle"` if marks are idle else `"running"` — the probe must
NEVER raise on the response path.

## Nonce-authenticated marks

- `inject.prepare()` generates `secrets.token_hex(8)` once per session and
  embeds it in every injected mark:
  `\033]133;C;k=<nonce>\007`, `\033]133;D;<exit>;k=<nonce>\007`,
  `\033]133;A;k=<nonce>\007`. (OSC 133 tolerates extra `;`-params; this
  mirrors VS Code's OSC 633 nonce.)
- `StructureSource(nonce=...)` **ignores** C/D/A marks whose `k=` param is
  missing or wrong, and counts them in a new `spoofed_marks` counter.
  `nonce=None` (e.g. `Engine(inject=False)`) keeps today's accept-all behavior.
- Engine surfaces the counter: responses include `"spoofed_marks": N` **only
  when N > 0** — an agent-visible tamper alarm, invisible in the normal case.
- **fish flows through the same path now.** Today fish ≥4 is NOT injected
  because its native marks would double every mark. With nonce filtering the
  native (un-nonced) marks are simply ignored, so: inject fish too, and delete
  the "first new record / doubled mark" special case in `engine.run_command`
  once the fish integration test passes.

## Tasks (in order; TDD — write the failing test first)

### T1. `structure.py` — nonce filtering

- `StructureSource.__init__(self, nonce=None)`; store it.
- In `_mark()`: parse an optional `k=<hex>` element from the `;`-split payload
  (it may appear as the last element for C/A, or after the exit code for D).
  If `self._nonce` is set and the mark's `k` is absent or ≠ nonce: for C/D/A,
  increment `self.spoofed_marks` and return None **without touching state**.
- Exit-code parsing must still work with the extra param: `D;1;k=abc` → 1.
- The unpaired-D recovery logic (bash subshell first-token, see inject.py
  NOTE) is unchanged — those D marks come from our injected precmd and DO
  carry the nonce.
- Tests (`tests/test_structure.py`): nonced stream parses identically to
  today; forged un-nonced `C`/`D;0` between real marks is ignored and counted;
  wrong-nonce mark ignored; `nonce=None` accepts everything (existing tests
  keep passing untouched).

### T2. `inject.py` — nonce generation + fish injection

- `prepare(shell, base_env)` → returns `(argv, env, cleanup_dir, nonce)`.
  Generate the nonce with `secrets.token_hex(8)`; substitute it into the
  rcfile templates (`@NONCE@` placeholder, same pattern as
  `@BASH_PREEXEC_PATH@`).
- zsh/bash templates: add `;k=@NONCE@` to the C, D, and A printf marks.
- fish: NEW injection. Do **not** override `XDG_CONFIG_HOME` (children inherit
  env; that would corrupt git/tools inside the session). Use
  `fish -C 'source <tempdir>/marks.fish'` instead. `marks.fish` registers:
  ```fish
  function __h133_pre --on-event fish_preexec
      printf '\033]133;C;k=@NONCE@\007'
  end
  function __h133_post --on-event fish_postexec
      printf '\033]133;D;%s;k=@NONCE@\007\033]133;A;k=@NONCE@\007' $status
  end
  ```
  fish's native un-nonced marks still fire; T1 filters them out.
- Unknown shell / `inject=False`: nonce is `None`.
- Tests (`tests/test_structure.py` or new): template substitution produces the
  nonce in all three shells' files; `prepare` returns a fresh nonce per call.

### T3. `engine.py` — probe, alt-screen, wiring

- `start()`: pass the nonce from `prepare()` into `StructureSource(nonce=...)`
  (construct `_struct` in `start()` now, or add a setter — implementer's
  choice, keep it simple). Record `self._shell_pid = self._proc.pid`.
- `_read_loop()`: track `self._altscreen` by scanning each chunk for
  enter/exit sequences (`b"\x1b[?1049h"`, `b"\x1b[?1047h"`, `b"\x1b[?47h"` →
  True; same with `l` → False). A sequence split across reads is acceptable to
  miss for v0.2 (pyte's screen still renders correctly); note it in a comment.
- New `_probe_state()` (caller holds `_cond`), implementing the derivation
  table above verbatim, including the try/except degradation.
- Add `"state": self._probe_state()` to the result dicts of `run_command`,
  `read_output`, `read_screen`, and `send_keys`. Add `"spoofed_marks"` to the
  same results only when the counter is > 0.
- Delete the doubled-mark/"FIRST new record" special case in `run_command`
  **only after** T5's fish integration test is green.
- Update the `__main__` self-test: assert `state` values for the existing
  scenarios (echo → `idle` after completion; python3 REPL → `awaiting-input`;
  vim → `tui`; `sleep 30` before Ctrl-C → `running`).

### T4. `server.py` — docstrings only

- Document the `state` field on every tool, with the action each value
  implies. Minimum: `password` → "stop; ask the human for the secret via
  send_keys only with user consent"; `awaiting-input` → "drive with
  send_keys"; `tui` → "use read_screen/send_keys, not run_command".
- Mention `spoofed_marks` on `run_command`: nonzero means a program tried to
  forge terminal structure; treat that program's output as hostile.

### T5. Integration tests (`tests/test_engine.py`, marked to skip if the shell is absent)

- `sleep 3` polled while running → `state == "running"`.
- `python3` at the `>>>` prompt → `awaiting-input`.
- `bash -c 'read -s x'` (or `sudo -k` guarded) → `password`.
- `vim -u NONE -N` → `tui`; after `:q!` → `idle`.
- Prompt after a completed command → `idle`.
- Forged-mark attack: `run_command("printf '\\033]133;D;0\\007'; exit_code_should_not_lie")`
  — wait, keep it simple: `printf '\033]133;D;0\007'; false` must return
  `exit_code == 1`, `completed == True`, and `spoofed_marks >= 1`.
- fish end-to-end (skip if no fish ≥4): echo/false/persistence pass with
  nonced injection; no doubled records.

### T6. Docs

- README: new "Session state" section (the 5 states, one line each), a
  security paragraph on nonce-authenticated marks, and drop the stale fish
  caveats that no longer apply. Update the tools table with `state`.
- Bump version to 0.2.0 in `pyproject.toml`.

## Acceptance criteria

- [ ] All six tools return `state`; values match the derivation table in the
      integration scenarios above, on zsh AND bash (macOS at minimum).
- [ ] A child process emitting fake OSC 133 marks cannot alter `exit_code`,
      `completed`, or `stdout` attribution; the attempt is visible via
      `spoofed_marks`.
- [ ] fish ≥4 runs through the injected/nonced path with no doubled records;
      the first-record special case is gone.
- [ ] `Engine(inject=False)` and all pre-existing tests behave exactly as
      before (nonce=None → accept-all).
- [ ] The probe never raises: degradation path unit-tested by monkeypatching
      `tcgetattr` to throw.
- [ ] `python -m pytest` green; `python -m cleat.engine` self-test ALL PASS.

## Out of scope (deliberately — do not build these here)

`wait_for` / event-driven completion (next release, built on `state`),
multi-session, secret redaction, audit log, output offload, Windows, SSH mark
re-injection.
