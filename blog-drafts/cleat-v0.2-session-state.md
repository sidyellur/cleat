# cleat v0.2: knowing what state the terminal is in, and refusing to trust it blindly

`cleat` is a headless terminal layer for AI agents — it runs a persistent shell
behind a PTY, parses OSC 133 shell-integration marks out of the byte stream,
and hands an agent back structured results (`stdout`, real `exit_code`, files
touched) over MCP instead of raw escape-code soup. v0.1 got the basic loop
working: inject marks, capture output, recover exit codes. v0.2, which just
shipped and closes [issue #5](https://github.com/sidyellur/cleat/issues/5),
answers two questions v0.1 couldn't: *what is the terminal doing right now*,
and *can I trust what it just told me*.

## The problem: guessing vs. knowing

Before this release, an agent driving `cleat` had no principled way to tell
the difference between "command still running," "a REPL is waiting for
input," and "a TUI has taken over the screen." You'd infer it from timing and
output shape, which is exactly the kind of guesswork `cleat` exists to
eliminate everywhere else.

There was also a quieter problem: a command can print arbitrary bytes to its
own stdout. Nothing stopped a misbehaving (or malicious) program from writing
its own fake `ESC]133;D;0BEL` — forging a clean exit code for output that
actually failed, and cleat would have believed it.

v0.2's two features are a matched pair aimed at both of these: a **session-state
oracle** (`idle` / `running` / `awaiting-input` / `password` / `tui`, derived
from termios flags and the foreground process group — facts only the process
holding the PTY can actually read) and **nonce-authenticated marks** (a fresh
per-session nonce embedded in every mark cleat injects, so a forged mark can't
alter `exit_code`, `stdout`, or `completed` — it can only get itself counted).

## Building it: a plan, then six tasks

The work was scoped up front in `PLAN.md` before any code was written, broken
into six subtasks (T1–T6), each landing as its own commit. That discipline
paid off in a couple of places worth calling out.

### Ordering is a security property, not a style choice

The nonce check in `structure.py`'s `_mark()` runs *before* any state
mutation for C/D/A marks, and bails out with `None` immediately on a miss.
That ordering is what makes the guarantee clean: a forged mark can't
*partially* apply — it can't, say, bump `commands_started` and then get
rejected. It's as if the byte never arrived, just silently tallied in
`spoofed_marks`. `run_command` surfaces that counter in its result whenever
it's nonzero, so the agent can tell a program in the session tried to lie
about its own completion, without the lie ever touching `exit_code` or
`stdout`.

The plan's derivation table for `_probe_state()` had a comment in all caps —
`ORDER IS LOAD-BEARING` — and it turned out to be earned, not decorative. The
"are we sitting on marks-idle" check has to run *before* the termios check,
because bash's readline puts the terminal into raw mode (ECHO off, ICANON
off) even at a plain, empty prompt — not just mid-REPL. Verified this
empirically by spawning a raw PTY and reading termios directly rather than
taking it on faith. Get the order backwards and every idle prompt would
misreport as `awaiting-input`.

### Two real bugs, both surfaced by the feature itself rather than by hunting for them

Building the alt-screen tracking for `state == "tui"` (watching for
`ESC[?1049h`/`?1047`/`?47`) ran into an environment landmine: the sandbox
inherits `TERM=linux` from its host, which has no `smcup`/`rmcup`
capability — so vim correctly never emitted the alt-screen sequence, and
`state` stayed stuck on `awaiting-input`. The instinct is to suspect your own
regex; the actual root cause was that `env.setdefault("TERM", ...)` only
ever applied when `TERM` was *unset*, not when it was set to something
alt-screen-incapable. Since cleat always renders through pyte regardless of
whatever `TERM` the host process happens to have, the fix was to stop
deferring to the inherited value and force `TERM=xterm-256color`
unconditionally. Any real deployment where the parent process had
`TERM=linux` or `TERM=dumb` would have had `tui` detection silently broken
the whole time.

The second bug showed up once fish was actually being exercised through
`structure.py`'s stdout capture for the first time — fish was entirely
unsupported before this issue, so this code path had simply never run
against it before. Fish's default prompt theme emits an `ESC ( B` charset-
designation escape after every color reset, which `_ANSI_RE` never covered,
so it leaked straight into captured stdout as a literal `\x1b(B`. Fixed by
extending the regex, with a unit test added alongside it.

Both bugs share a pattern worth naming: a security/state feature finally
drove real code paths — alt-screen detection, fish's stdout capture — that
had never been tested before, and the gaps it surfaced had nothing to do
with the feature itself.

### Fish, for real this time

Fish previously wasn't injected into at all — it was a documented gap.
T2 gives it its own injection path (`fish -C 'source <tempdir>/marks.fish'`,
rather than overriding `$XDG_CONFIG_HOME`, which would have corrupted config
lookup for any children spawned in the session), with the same
`@NONCE@`-substituted marks as bash and zsh. The nice side effect of the
nonce work: fish ≥4 emits its own *native* OSC 133 marks alongside cleat's
injected ones, and since fish's native marks carry no nonce, the filter
ignores them the same way it would ignore a forgery — no double-counting,
no special-cased "first record" indexing hack to maintain. That workaround
is gone.

One honest caveat from the acceptance-criteria pass: CI here only had fish
3.7 available to test against, below the >=4 version gate. The injection
mechanism itself is version-independent, though, and it was manually
verified against the 3.7 binary directly — including the `ESC ( B` bug
above, which was also caught that way.

## What shipped

The README now documents both pieces as first-class, user-facing behavior.
Every tool response carries a `state` field:

| `state` | Meaning | What to do |
|---|---|---|
| `idle` | nothing is running | call `run_command` for the next thing |
| `running` | a command is executing | poll `read_output` |
| `awaiting-input` | a REPL/prompt is blocked on stdin | drive it with `send_keys` |
| `password` | a secret prompt (`sudo`, `read -s`) is waiting, echo off | stop — only send input with the human's explicit consent |
| `tui` | a full-screen program (vim, top, less) owns the terminal | use `read_screen`/`send_keys` |

And `run_command` results include `spoofed_marks` whenever it's nonzero —
the signal that some program in the session tried to forge a completion mark
and failed.

## What didn't ship, on purpose

Not every finding turned into a fix. Running the full test suite surfaced
`test_read_output_preserves_tail_of_command_finishing_between_polls[zsh]`
failing deterministically. Before assuming it was a regression, the fix was
confirmed against the pristine pre-#5 base commit via `git stash`/checkout:
it fails there too. Turns out zsh was simply never installed/tested in this
sandbox before now, so this was a latent, unrelated bug getting its first
exposure — not something #5 broke. It was left alone, flagged as worth its
own issue, rather than scope-creeping a security/state feature into an
unrelated fix.

## Closing thought

The two headline bugs here weren't found by hunting for them — they fell out
of finally exercising code paths (fish stdout, alt-screen detection under a
weird inherited `TERM`) that a security-and-state feature happened to be the
first thing to actually drive end-to-end. That's a reasonable argument for
why "add an oracle for what's really happening" and "stop trusting a program
to tell you how it finished" belonged in the same release: both forced
`cleat` to actually look at things it had previously been able to get away
with guessing about.

v0.2 is out now as `0.2.0`; the six-task plan (T1–T6) that shaped it lives in
`PLAN.md`, and the full run of decisions above is journaled in
`NOTES-FOR-BLOG.md` in the repo.
