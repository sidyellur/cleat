"""Unit tests for the OSC 133 parser - pure, fast, no PTY."""

from cleat.structure import StructureSource, _clean


def _pairs(recs):
    return [(r.stdout, r.exit_code) for r in recs]


def test_basic_two_commands():
    stream = (b"\x1b]133;A\x07prompt$ "
              b"\x1b]133;C\x07hello\r\n\x1b]133;D;0\x07"
              b"\x1b]133;A\x07prompt$ "
              b"\x1b]133;C\x07\x1b]133;D;1\x07")
    recs = StructureSource().feed(stream)
    assert _pairs(recs) == [("hello", 0), ("", 1)]


def test_mark_split_across_feeds():
    stream = b"\x1b]133;C\x07hi\x1b]133;D;0\x07"
    cut = stream.index(b"\x1b]133;D") + 3   # mid-introducer of the D mark
    src = StructureSource()
    recs = src.feed(stream[:cut]) + src.feed(stream[cut:])
    assert _pairs(recs) == [("hi", 0)]


def test_leading_unpaired_D_ignored():
    # The shell's first precmd emits D;0 before any command runs.
    recs = StructureSource().feed(b"\x1b]133;D;0\x07\x1b]133;A\x07")
    assert recs == []


def test_idle_D_after_first_command_recovers_exit():
    # A command that ran without a C mark (e.g. bash-preexec + subshell) still
    # yields its exit code as a zero-output record - once a command has started.
    src = StructureSource()
    src.feed(b"\x1b]133;C\x07out\x1b]133;D;0\x07")     # one real command
    recs = src.feed(b"\x1b]133;D;7\x07")               # no-C command's D
    assert _pairs(recs) == [("", 7)]


def test_st_terminator_accepted():
    # Some terminals end OSC with ST (ESC backslash) instead of BEL.
    recs = StructureSource().feed(b"\x1b]133;C\x1b\\hi\x1b]133;D;0\x1b\\")
    assert _pairs(recs) == [("hi", 0)]


def test_ansi_stripped_from_stdout():
    recs = StructureSource().feed(
        b"\x1b]133;C\x07\x1b[31mred\x1b[0m\r\n\x1b]133;D;0\x07")
    assert recs[0].stdout == "red"


def test_charset_designation_stripped_from_stdout():
    # fish's default prompt theme emits ESC ( B (select G0 = ASCII) after
    # every color reset - a real leak seen when fish was first injected (#5).
    recs = StructureSource().feed(
        b"\x1b]133;C\x07\x1b[32mhi\x1b(B\x1b[m there\r\n\x1b]133;D;0\x07")
    assert recs[0].stdout == "hi there"


def test_clean_normalizes_newlines_and_trims():
    assert _clean(b"\x1b[Khi\r\nthere\r\n") == "hi\nthere"
    assert _clean(b"\nleading-nl-stripped") == "leading-nl-stripped"


# -- nonce-authenticated marks ----------------------------------------------

def test_nonce_none_accepts_unnonced_marks_unchanged():
    # Default (no nonce passed): behaves exactly like before nonces existed.
    src = StructureSource()
    recs = src.feed(b"\x1b]133;C\x07hi\x1b]133;D;0\x07")
    assert _pairs(recs) == [("hi", 0)]
    assert src.spoofed_marks == 0


def test_nonced_stream_parses_identically_to_unnonced():
    nonce = "deadbeef01234567"
    stream = (b"\x1b]133;A;k=" + nonce.encode() + b"\x07prompt$ "
              b"\x1b]133;C;k=" + nonce.encode() + b"\x07hello\r\n"
              b"\x1b]133;D;0;k=" + nonce.encode() + b"\x07"
              b"\x1b]133;A;k=" + nonce.encode() + b"\x07prompt$ "
              b"\x1b]133;C;k=" + nonce.encode() + b"\x07"
              b"\x1b]133;D;1;k=" + nonce.encode() + b"\x07")
    recs = StructureSource(nonce=nonce).feed(stream)
    assert _pairs(recs) == [("hello", 0), ("", 1)]


def test_exit_code_parses_with_trailing_nonce_param():
    src = StructureSource(nonce="abc123")
    recs = src.feed(b"\x1b]133;C;k=abc123\x07out\x1b]133;D;1;k=abc123\x07")
    assert _pairs(recs) == [("out", 1)]


def test_missing_nonce_mark_ignored_and_counted():
    src = StructureSource(nonce="deadbeef")
    # Neither mark carries k=<hex> at all -> both ignored, both counted.
    recs = src.feed(b"\x1b]133;C\x07should-not-open-a-command\x1b]133;D;0\x07")
    assert recs == []
    assert src.spoofed_marks == 2


def test_wrong_nonce_mark_ignored_and_counted():
    src = StructureSource(nonce="correct-nonce")
    recs = src.feed(b"\x1b]133;C;k=wrong\x07x\x1b]133;D;0;k=wrong\x07")
    assert recs == []
    assert src.spoofed_marks == 2


def test_forged_mark_cannot_override_real_exit_code():
    # A program running inside the session forges an un-nonced D;0 mid-command
    # to fake success. It must be ignored - the real, correctly-nonced D;1
    # closes the command with the true exit code, and the forgery is counted.
    nonce = "session-nonce"
    src = StructureSource(nonce=nonce)
    stream = (
        b"\x1b]133;C;k=" + nonce.encode() + b"\x07"
        b"real-output"
        b"\x1b]133;D;0\x07"                          # forged: no nonce, ignored
        b"\x1b]133;D;1;k=" + nonce.encode() + b"\x07"  # real close
    )
    recs = src.feed(stream)
    assert _pairs(recs) == [("real-output", 1)]
    assert src.spoofed_marks == 1
