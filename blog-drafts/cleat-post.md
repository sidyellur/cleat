# The byte soup fights back: a one-character crash in cleat's OSC 133 parser

cleat is a headless terminal layer for AI agents. It runs a persistent shell
behind a PTY, injects [OSC 133](https://gitlab.freedesktop.org/Per_Bothner/specifications/blob/master/proposals/semantic-prompts.md)
shell-integration marks into the shells it spawns, parses those marks back out
of the raw byte stream, and hands the agent structured results over MCP —
`stdout`, a real `exit_code`, `completed: true` — instead of escape-code soup.

The parsing lives in one deliberately dumb component, `StructureSource` in
`src/cleat/structure.py`. It knows nothing about PTYs or shells; you call
`feed()` with raw terminal bytes, and it emits a `CommandRecord` for every
`C` → `D` mark pair (output-begins → command-finished). That dumbness is the
point: it's testable in isolation, and it has a short list of hard-won,
real-world quirks it already handles — marks split across read boundaries,
BEL vs. ST terminators, the spurious `D;0` some shells emit before the first
prompt.

Today I went hunting for a small robustness gap in it, and found one that's a
nice little case study in why "looks numeric" and "is numeric" are not the
same check.

## The bug

A `D` mark carries the command's exit code as its second field: `133;D;0`,
`133;D;1`, and so on. The parser extracted it like this:

```python
if len(parts) > 1 and parts[1].lstrip("-").isdigit():
    exit_code = int(parts[1])
```

The intent of `lstrip("-")` is to accept negative exit codes. But `lstrip`
strips *every* leading dash, not just one. So a malformed payload like
`D;--5` sails through the check — strip the dashes, `"5".isdigit()` is true —
and then `int("--5")` raises `ValueError`.

That's the whole bug: a validation check that *approximates* what the
consumer (`int()`) accepts, and disagrees with it on exactly one class of
input.

## Why a ValueError here is not just a ValueError

On its own, a crash on garbage input is bad but survivable. What made this
one worth fixing immediately is *where* it raises.

`feed()` is called from the engine's background read-loop thread — the thread
that continuously drains the PTY and advances all of cleat's state. And the
code around that call is instructive:

```python
recs = self._struct.feed(data)
self._raw += data
self._records.extend(recs)
try:
    self._pyte.feed(data)
except Exception:
    pass  # never let a rendering hiccup kill the read loop
```

The neighboring `pyte.feed` — the screen renderer — is wrapped in a guard
precisely because a rendering hiccup must never kill the read loop. The
structure parser's `feed()` sits right next to it, unguarded. So a
`ValueError` escaping it kills the read-loop thread, and the session wedges:
no more output, no more completions, from the agent's side just silence.

And here's the kicker: nothing about triggering it requires a buggy shell.
The parser eats *everything* that crosses the PTY — which means anything any
program prints. `cat` a binary file, curl the wrong endpoint, run a program
that logs escape sequences, and if the bytes merely *resemble* a broken `D`
mark (`ESC ] 133 ; D ; --5 BEL`), the read loop dies. A parser fed untrusted
terminal byte soup has to treat every field as hostile.

## The fix

Make the check exactly as strict as the consumer:

```python
# Full-match, not lstrip+isdigit: a payload like "D;--5" must fall
# through to None, not reach int() and raise out of feed().
if len(parts) > 1 and re.fullmatch(r"-?\d+", parts[1]):
    exit_code = int(parts[1])
```

`-?\d+` accepts at most one leading minus, so legitimate negative exit codes
still parse, and anything else — `--5`, `abc`, `5x`, a bare `-`, an empty
field — falls through to `exit_code=None`. That's the right degradation for
this component: the command record still closes (the agent still sees
`completed`, still gets stdout), it just honestly reports that the exit code
was unreadable, instead of taking the whole session down with it.

The tests encode both sides of that contract:

```python
def test_malformed_exit_code_yields_none_not_crash():
    for bad in (b"--5", b"abc", b"5x", b"-", b""):
        src = StructureSource()
        recs = src.feed(b"\x1b]133;C\x07out\x1b]133;D;" + bad + b"\x07")
        assert _pairs(recs) == [("out", None)], bad


def test_negative_exit_code_still_parsed():
    recs = StructureSource().feed(b"\x1b]133;C\x07\x1b]133;D;-1\x07")
    assert _pairs(recs) == [("", -1)]
```

## What I'm taking away

Two things.

First, the specific Python trap: `s.lstrip("-").isdigit()` is not a validity
check for `int(s)`. `lstrip` takes a *set of characters* to remove, not a
prefix, so it happily eats any number of dashes. If the question you're
asking is "will `int()` accept this?", ask it with a predicate that is a
strict subset of `int()`'s grammar — `re.fullmatch(r"-?\d+", ...)`, which is
all an exit code can legitimately be — not with an approximation that can
approve inputs the consumer rejects.

Second, the general one: `StructureSource`'s docstring calls it the component
that "just eats bytes and emits facts," and its quirk list already reflected
several rounds of contact with real terminal output. This bug is the same
lesson one level deeper. It's not enough to be liberal about *which marks*
arrive and *how they're framed*; every field *inside* a mark is attacker-ish
input too, because in a PTY stream there is no boundary between "the shell
integration talking to you" and "whatever some program decided to print."
Parse totally — every input maps to a value or a `None`, never to an
exception — especially when you're the unguarded code on somebody's read
loop.

---

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
