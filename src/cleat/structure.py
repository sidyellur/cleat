#!/usr/bin/env python3
"""
structure.py - the structure source. THE novel part.

Turns a raw terminal byte stream into structured command records by tracking
OSC 133 (FinalTerm) shell-integration marks. This is the "glasses lens": it
knows nothing about PTYs or shells - it just eats bytes and emits facts.

The contract is deliberately dumb so it's testable in isolation:

    src = StructureSource()
    records = src.feed(b'...raw terminal bytes...')   # call repeatedly
    # records is a list of completed CommandRecord, one per C->D cycle.

State machine (OSC 133):

    ...      ]133;C  -> RUNNING : start capturing stdout
    RUNNING  <bytes> -> RUNNING : accumulate stdout
    RUNNING  ]133;D;n -> IDLE   : finalize record with exit code n
    *        ]133;D    (no preceding C) : unpaired D, ignored
    ]133;A / ]133;B  : prompt framing, irrelevant to stdout - ignored

Things learned from real spy logs that this handles:
  - terminator may be BEL (\\x07) OR ST (\\x1b\\) - accept both
  - a mark can be split across two feed() calls - we buffer the tail
  - the first precmd emits a lone ]133;D;0 before any command - ignored
  - stdout regions carry color/cursor escapes - stripped for legibility

Nonce-authenticated marks: pass nonce=<hex string> (see inject.py) and every
C/D/A mark must carry a matching `;k=<hex>` param or it's ignored entirely -
state, stdout, and exit code are always untouched either way. This closes the
ANSI-injection hole where a program cleat runs could emit its own
`ESC ]133;D;0 BEL` to forge a successful exit code. nonce=None (the default)
accepts every mark, matching pre-nonce behavior exactly.

A rejected mark is also counted in spoofed_marks, UNLESS it has no `k=` param
at all *and* expect_unnonced_marks=True - some shells run their own native,
un-nonced OSC 133 emitter alongside ours (fish >= 4), and that's expected
telemetry, not tampering, so it shouldn't trip a tamper counter. A mark with a
*wrong* `k=` value is always counted regardless - only a deliberate attempt to
impersonate our scheme would bother including one.
"""

import re
import json
from dataclasses import dataclass, asdict
from typing import Optional


# An OSC 133 mark: ESC ] 133 ; <payload> <BEL | ST>. Payload runs until the
# terminator; it cannot contain ESC or BEL, which lets the regex stay greedy-safe.
_MARK_RE = re.compile(rb"\x1b\]133;([^\x07\x1b]*)(?:\x07|\x1b\\)")

# Strip ANSI noise from captured stdout: CSI sequences, other OSC sequences,
# charset designations, and lone two-byte escapes. We parse 133 marks out
# separately, before this runs.
_ANSI_RE = re.compile(
    rb"\x1b\[[0-?]*[ -/]*[@-~]"          # CSI  e.g. \x1b[31m, \x1b[K
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC  e.g. \x1b]0;title\x07
    rb"|\x1b[()*+\-./][0-~]"              # SCS (charset designation) e.g.
                                           # \x1b(B - fish's default prompt
                                           # theme emits one after every color
                                           # reset
    rb"|\x1b[@-Z\\-_]"                    # other two-byte escapes
)


# Cap a single in-flight command's stdout accumulator (issue #15): without
# this, a command like `yes | head -c 2000000000` grows self._stdout without
# bound for the ENTIRE duration of the command, since nothing drains it until
# the D mark closes the record. Past the cap, keep a head window (context from
# the start of the output) and a rolling tail window (the most recent bytes),
# dropping the middle - callers are told via `truncated`.
_MAX_STDOUT = 1 << 20  # 1 MiB
_STDOUT_HEAD = _MAX_STDOUT // 2
_STDOUT_TAIL = _MAX_STDOUT // 2


@dataclass
class CommandRecord:
    """One command's worth of facts, dug out of the byte soup."""
    stdout: str
    exit_code: Optional[int]
    truncated: bool = False

    def as_dict(self):
        return asdict(self)

    def as_json(self):
        return json.dumps(self.as_dict())


def _clean(raw: bytes) -> str:
    """ANSI-strip + normalize newlines -> the clean sticky-note text."""
    txt = _ANSI_RE.sub(b"", raw)
    txt = txt.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    # strip("\n") drops a leading newline some shells (e.g. fish's native C mark)
    # leak from the cursor repaint just before output; rstrip() trims the tail.
    return txt.decode("utf-8", "replace").strip("\n").rstrip()


class StructureSource:
    def __init__(self, nonce=None, expect_unnonced_marks=False):
        self._buf = b""          # bytes not yet resolved (may hold a partial mark)
        self._state = "IDLE"     # IDLE | RUNNING
        self._stdout = b""       # raw stdout accumulated while RUNNING (tail
                                  # window only, once truncated - see _content)
        self._stdout_head = None  # bytes: captured once, first _STDOUT_HEAD
                                   # bytes, or None if never truncated
        self._stdout_total = 0     # total bytes seen for the in-flight command
        self._nonce = nonce      # expected k=<hex> on C/D/A marks; None = accept all
        # Some shells have their OWN native OSC 133 emitter running alongside
        # ours (fish >= 4) that will never carry our nonce - that's expected,
        # harmless telemetry, not tampering. A mark with NO k= at all is only
        # suppressed from spoofed_marks when this is set; a mark with a WRONG
        # k= is always counted, on every shell - only something trying to
        # impersonate our scheme would bother including one.
        self._expect_unnonced_marks = expect_unnonced_marks
        self.commands_started = 0  # bumped on each C mark (output-begins)
        self.prompts_seen = 0      # bumped on each A mark (prompt-start shown)
        self.spoofed_marks = 0     # forged/wrong-nonce C/D/A marks seen and ignored

    def partial_stdout(self) -> str:
        """Cleaned stdout captured so far for an in-flight command (post-C)."""
        return _clean(self._current_stdout_bytes())

    @property
    def stdout_truncated(self) -> bool:
        """True once the in-flight command's stdout has exceeded the cap."""
        return self._stdout_head is not None

    def _current_stdout_bytes(self) -> bytes:
        """Compose the captured stdout, head+tail if truncated. Caller holds
        no lock requirement beyond whatever the engine already uses."""
        if self._stdout_head is None:
            return bytes(self._stdout)
        omitted = self._stdout_total - len(self._stdout_head) - len(self._stdout)
        marker = f"\n...[cleat: {omitted} bytes omitted]...\n".encode()
        return self._stdout_head + marker + bytes(self._stdout)

    def _reset_stdout(self):
        self._stdout = b""
        self._stdout_head = None
        self._stdout_total = 0

    @property
    def idle(self) -> bool:
        """True when no command is running (the shell sits at a prompt): we've
        seen a C->D cycle close (or never opened one) and aren't mid-command.
        Lets the engine tell "finished, nothing more coming" from "still
        running, momentarily quiet" instead of blocking + guessing."""
        return self._state == "IDLE"

    def feed(self, data: bytes):
        """Push raw bytes in; get back a list of newly-completed records."""
        self._buf += data
        completed = []

        # Consume every COMPLETE mark in the buffer, in order.
        while True:
            m = _MARK_RE.search(self._buf)
            if not m:
                break
            self._content(self._buf[: m.start()])
            rec = self._mark(m.group(1).decode("ascii", "replace"))
            if rec is not None:
                completed.append(rec)
            self._buf = self._buf[m.end() :]

        # Whatever's left has no complete mark. The tail from the last ESC
        # onward might be a mark cut in half by a read boundary - hold it back.
        # Everything before it is safe to consume as content now.
        last_esc = self._buf.rfind(b"\x1b")
        if last_esc == -1:
            self._content(self._buf)
            self._buf = b""
        else:
            self._content(self._buf[:last_esc])
            self._buf = self._buf[last_esc:]

        return completed

    def _content(self, chunk: bytes):
        """Bytes between marks: stdout only while a command is RUNNING.
        Bounded (issue #15): past _MAX_STDOUT, keep a fixed head window plus
        a rolling tail window instead of growing for the whole command."""
        if self._state != "RUNNING" or not chunk:
            return
        self._stdout += chunk
        self._stdout_total += len(chunk)
        if self._stdout_head is None:
            if len(self._stdout) > _MAX_STDOUT:
                self._stdout_head = bytes(self._stdout[:_STDOUT_HEAD])
                self._stdout = self._stdout[-_STDOUT_TAIL:]
        elif len(self._stdout) > _STDOUT_TAIL:
            self._stdout = self._stdout[-_STDOUT_TAIL:]

    def _mark(self, payload: str):
        """Apply one mark's payload; return a CommandRecord if one just closed."""
        parts = payload.split(";")
        code = parts[0] if parts and parts[0] else ""

        if code in ("C", "D", "A") and self._nonce is not None:
            nonce_val = next((p[2:] for p in parts[1:] if p.startswith("k=")), None)
            if nonce_val != self._nonce:
                # Forged or wrong-nonce mark: a program we run cannot use this
                # to fake completion or an exit code. Ignore it completely -
                # state/stdout below are untouched either way. Only bump the
                # visible tamper counter if this isn't a bare mark from a
                # shell's own expected native emitter (fish >= 4): a WRONG
                # k= is always counted (that's a deliberate impersonation
                # attempt), a MISSING k= is counted unless expected.
                if not (nonce_val is None and self._expect_unnonced_marks):
                    self.spoofed_marks += 1
                return None

        if code == "C":                      # command output begins
            self._state = "RUNNING"
            self._reset_stdout()
            self.commands_started += 1
            return None

        if code == "D":                      # command finished
            exit_code = None
            # Full-match, not lstrip+isdigit: a payload like "D;--5" must fall
            # through to None, not reach int() and raise out of feed().
            if len(parts) > 1 and re.fullmatch(r"-?\d+", parts[1]):
                exit_code = int(parts[1])
            if self._state == "RUNNING":
                rec = CommandRecord(stdout=_clean(self._current_stdout_bytes()),
                                     exit_code=exit_code,
                                     truncated=self.stdout_truncated)
                self._state = "IDLE"
                self._reset_stdout()
                return rec
            # IDLE D. The shell's first precmd emits a spurious D;0 BEFORE the
            # first prompt is shown (no A, no command yet) - ignore that. But a D
            # after a prompt has been displayed, with no preceding C, means a
            # command ran without a C mark (e.g. bash-preexec skips preexec for a
            # subshell or brace-group first token) - recover its exit code as a
            # zero-output record so the caller sees completion instead of hanging.
            if self.commands_started > 0 or self.prompts_seen > 0:
                return CommandRecord(stdout="", exit_code=exit_code)
            return None                      # spurious leading D;0

        if code == "A":                      # prompt start (a prompt was shown)
            self.prompts_seen += 1
            return None

        # 'B' (prompt end): framing only, nothing to emit.
        return None


# ---------------------------------------------------------------------------
# Self-test: feed a synthetic stream that mimics real spy-log bytes, including
# a mark split across a chunk boundary and the spurious leading D;0.
if __name__ == "__main__":
    stream = (
        b"\x1b]133;D;0\x07\x1b]133;A\x07"          # spurious leading D + prompt
        b"siddharth ~ % echo hello\r\n"             # prompt + echoed input (ignored)
        b"\x1b]133;C\x07"                           # output begins
        b"hello\r\n"                                # stdout
        b"\x1b]133;D;0\x07\x1b]133;A\x07"           # done, exit 0 + next prompt
        b"siddharth ~ % false\r\n"
        b"\x1b]133;C\x07"
        b"\x1b]133;D;1\x07"                         # done, exit 1, no output
    )

    # Split mid-mark to prove cross-chunk buffering works.
    cut = stream.index(b"hello") + 2
    src = StructureSource()
    records = src.feed(stream[:cut]) + src.feed(stream[cut:])

    got = [r.as_dict() for r in records]
    expected = [
        {"stdout": "hello", "exit_code": 0},
        {"stdout": "", "exit_code": 1},
    ]
    assert got == expected, f"FAIL\n got={got}\n exp={expected}"
    print("ok - parsed records:")
    for r in records:
        print("   ", r.as_json())
