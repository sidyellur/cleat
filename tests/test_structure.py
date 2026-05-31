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


def test_clean_normalizes_newlines_and_trims():
    assert _clean(b"\x1b[Khi\r\nthere\r\n") == "hi\nthere"
    assert _clean(b"\nleading-nl-stripped") == "leading-nl-stripped"
