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
