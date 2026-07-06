"""Integration tests for the Engine. These spawn real shells under a PTY.

Core behaviors are parametrized over whichever supported shells are installed
(bash/zsh/fish); shell- or program-specific cases skip when unavailable, so the
suite runs anywhere (CI included)."""

import os
import re
import shutil
import signal
import subprocess
import termios
import threading
import time

import pytest

from cleat.engine import Engine, _MAX_RAW, _blocked_on_read


def _fish_supported(path):
    """fish >= 4 emits OSC 133 natively; older fish is unsupported."""
    if not path:
        return False
    try:
        out = subprocess.run([path, "--version"], capture_output=True,
                             text=True, timeout=5).stdout
        m = re.search(r"version (\d+)", out)
        return bool(m) and int(m.group(1)) >= 4
    except Exception:
        return False


_SUPPORTED = [p for p in (shutil.which("bash"), shutil.which("zsh")) if p]
if _fish_supported(shutil.which("fish")):
    _SUPPORTED.append(shutil.which("fish"))


@pytest.fixture(params=_SUPPORTED, ids=lambda p: p.rsplit("/", 1)[-1])
def eng(request):
    e = Engine(shell=request.param).start()
    yield e
    e.close()


@pytest.fixture
def bash_eng():
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not installed")
    e = Engine(shell=bash).start()
    yield e
    e.close()


@pytest.fixture
def fish_eng():
    fish = shutil.which("fish")
    if not _fish_supported(fish):
        pytest.skip("fish >= 4 not installed")
    e = Engine(shell=fish).start()
    yield e
    e.close()


def test_engine_inject_false_unaffected(request):
    # inject=False -> no OSC 133 injection at all -> nonce=None -> the
    # structure source accepts everything, exactly as before nonces existed.
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not installed")
    eng = Engine(shell=bash, inject=False).start()
    request.addfinalizer(eng.close)
    eng.run_command("echo hi", timeout=1.0)
    assert eng._struct._nonce is None
    assert eng._struct.spoofed_marks == 0


# -- core behaviors, every supported shell ---------------------------------
def test_echo(eng):
    assert eng.run_command("echo hi") == {
        "stdout": "hi", "exit_code": 0, "completed": True, "state": "idle"}


def test_nonzero_exit(eng):
    r = eng.run_command("false")
    assert r["exit_code"] == 1 and r["completed"]


def test_persistence_cd(eng):
    eng.run_command("cd /tmp")
    r = eng.run_command("pwd")
    assert r["stdout"] in ("/tmp", "/private/tmp")  # macOS symlinks /tmp


def test_slow_command_completes(eng):
    r = eng.run_command("sleep 1; echo woke", timeout=5)
    assert r["completed"] and r["stdout"] == "woke"


def test_python_repl_interactive(eng):
    if eng.shell.rsplit("/", 1)[-1] == "fish":
        pytest.skip("interactive REPL driving verified on bash/zsh; "
                    "fish is supported for command execution")
    r = eng.run_command("python3", timeout=10)
    # REPL stays open (doesn't "complete"). The ">>>" prompt can lag the banner
    # on slow runners, so prove the REPL actually works by computing in it rather
    # than asserting on prompt-emit timing.
    assert not r["completed"]
    r = eng.send_keys("print(6*7)", enter=True)
    assert "42" in r["screen"]
    r = eng.send_keys("exit()", enter=True)
    assert r["completed"] and r["exit_code"] is not None


# -- polling after completion (issue #1) -----------------------------------
def test_read_output_after_completion_reports_done_promptly(eng):
    # Polling right after a command finished must NOT block the full timeout and
    # must NOT lie with completed=False: the shell is idle at a prompt, so report
    # completion immediately with the real last exit code. (issue #1)
    eng.run_command("echo hi")
    t0 = time.monotonic()
    r = eng.read_output(timeout=2.0)
    elapsed = time.monotonic() - t0
    assert r["completed"] is True
    assert r["exit_code"] == 0
    assert r["output"] == ""          # no NEW command output; prompt chrome != output
    assert elapsed < 1.0              # returned promptly, didn't burn the timeout


def test_read_output_after_completion_reports_failure_exit(eng):
    eng.run_command("false")
    r = eng.read_output(timeout=2.0)
    assert r["completed"] is True and r["exit_code"] == 1


def test_read_output_preserves_tail_of_command_finishing_between_polls(eng):
    # A long-runner whose output lands AFTER run_command's window must not be
    # lost: the idle short-circuit only fires when nothing is buffered, so the
    # tail + completion are still delivered by the following poll. (issue #1)
    r = eng.run_command("sleep 1; echo LATE", timeout=0.3)
    assert not r["completed"]                      # timed out mid-sleep
    r2 = eng.read_output(timeout=3.0)
    assert "LATE" in r2["output"]                  # tail preserved, not dropped
    assert r2["completed"] is True and r2["exit_code"] == 0


def test_read_screen_after_completion_returns_promptly(eng):
    eng.run_command("echo hi")
    t0 = time.monotonic()
    scr = eng.read_screen(timeout=2.0)
    elapsed = time.monotonic() - t0
    assert "hi" in scr["screen"]
    assert elapsed < 1.0              # idle + nothing pending => no settle wait


# -- wait_for: block until the session needs attention (issue #10) --------
def test_wait_for_returns_immediately_when_already_idle(eng):
    eng.run_command("echo hi")
    t0 = time.monotonic()
    r = eng.wait_for(timeout=5.0)
    elapsed = time.monotonic() - t0
    assert r["completed"] is True and r["exit_code"] == 0
    assert elapsed < 1.0              # already idle -> no blocking wait


def test_wait_for_blocks_until_long_command_completes(eng):
    r = eng.run_command("sleep 1; echo woke", timeout=0.2)
    assert not r["completed"]                      # timed out mid-sleep
    t0 = time.monotonic()
    r2 = eng.wait_for(timeout=5.0)
    elapsed = time.monotonic() - t0
    assert "woke" in r2["output"]
    assert r2["completed"] is True and r2["exit_code"] == 0
    assert r2["state"] == "idle"
    assert 0.5 < elapsed < 4.0        # actually waited for it, didn't just poll once


def test_wait_for_returns_on_repl_prompt(eng):
    if eng.shell.rsplit("/", 1)[-1] == "fish":
        pytest.skip("interactive REPL driving verified on bash/zsh; "
                    "fish is supported for command execution")
    eng.run_command("python3", timeout=1)   # banner may not have settled yet
    r = eng.wait_for(timeout=10.0)
    assert not r["completed"]
    assert r["state"] == "awaiting-input"
    eng.send_keys("exit()", enter=True)     # drain so teardown is clean


def test_wait_for_times_out_while_still_running(bash_eng):
    bash_eng.run_command("sleep 5", timeout=0.2)
    t0 = time.monotonic()
    r = bash_eng.wait_for(timeout=0.5)
    elapsed = time.monotonic() - t0
    assert not r["completed"]
    assert r["state"] == "running"
    assert elapsed < 1.5              # honored the short timeout


def test_wait_for_raises_if_not_started():
    e = Engine(shell=shutil.which("bash"))
    with pytest.raises(RuntimeError):
        e.wait_for(timeout=1.0)


def test_wait_for_settle_window_survives_stale_non_running_state(bash_eng, monkeypatch):
    # Regression test for a race found via manual zsh testing while building
    # wait_for (PR #11): on zsh, the shell reclaims the foreground pgid and
    # re-enters its own raw ZLE mode the instant a child exits - a beat
    # BEFORE its precmd hook's D/A completion marks are actually parsed. That
    # makes _probe_state() transiently report a non-"running" state (here,
    # "awaiting-input") for a command that hasn't actually finished yet.
    # Manual reruns only reproduced it ~3/4 of the time on real zsh timing;
    # force the exact misreading deterministically here (fg == shell pid,
    # ICANON off) on bash instead, so this is fast and 100% reproducible
    # regardless of which shells happen to be installed.
    r = bash_eng.run_command("sleep 0.1; echo woke", timeout=0.02)
    assert not r["completed"]                      # still running for real

    shell_pid = bash_eng._shell_pid
    monkeypatch.setattr(os, "tcgetpgrp", lambda fd: shell_pid)
    real_tcgetattr = termios.tcgetattr

    def _fake_tcgetattr(fd):
        attrs = list(real_tcgetattr(fd))
        attrs[3] &= ~termios.ICANON      # forces the "awaiting-input" branch
        return attrs

    monkeypatch.setattr(termios, "tcgetattr", _fake_tcgetattr)
    with bash_eng._cond:
        assert bash_eng._probe_state() == "awaiting-input"  # confirm the fake fools it

    # Despite the state claiming "awaiting-input" throughout, the settle
    # window must still wait for the REAL completion instead of trusting it.
    r2 = bash_eng.wait_for(timeout=2.0)
    assert r2["completed"] is True and r2["exit_code"] == 0
    assert "woke" in r2["output"]


# -- session-state oracle (issue #5) ---------------------------------------
def test_state_idle_after_completed_command(eng):
    eng.run_command("echo hi")
    r = eng.read_output(timeout=1.0)
    assert r["completed"] is True
    assert r["state"] == "idle"


def test_state_running_while_command_executes(eng):
    r = eng.run_command("sleep 3", timeout=0.5)
    assert not r["completed"]
    assert r["state"] == "running"


def test_state_possibly_awaiting_input_on_canonical_read(bash_eng):
    # Issue #27: a plain `read x` (or `cat` waiting on stdin) leaves the
    # terminal in CANONICAL mode with echo on, so termios alone can't tell
    # it apart from a genuinely busy program - it used to fall through to
    # "running" and an agent polling read_output()/wait_for() would just
    # time out, never learning it should send_keys() a line. Best-effort
    # (Linux, via /proc/<pid>/wchan): if the foreground process is blocked
    # in a read-like wait, surface a distinct "possibly-awaiting-input"
    # instead.
    r = bash_eng.run_command("read x", timeout=2)
    assert not r["completed"]
    assert r["state"] == "possibly-awaiting-input"
    bash_eng.send_keys("hi", enter=True)  # drain so teardown is clean


def test_state_still_running_for_sleep_not_possibly_awaiting_input(bash_eng):
    # A real sleep() is ALSO a blocking wait, but a different one
    # (hrtimer_nanosleep, not a read) - must not be misclassified either.
    r = bash_eng.run_command("sleep 3", timeout=0.5)
    assert not r["completed"]
    assert r["state"] == "running"


def test_blocked_on_read_degrades_gracefully_for_unknown_pid():
    # A pid with no /proc entry (or on a non-Linux platform) must never
    # raise - this is a best-effort refinement, not a hard requirement.
    assert _blocked_on_read(-1) is False
    assert _blocked_on_read(2**30) is False


def test_state_awaiting_input_in_repl(eng):
    if eng.shell.rsplit("/", 1)[-1] == "fish":
        pytest.skip("interactive REPL driving verified on bash/zsh; "
                    "fish is supported for command execution")
    r = eng.run_command("python3", timeout=10)
    assert not r["completed"]
    assert r["state"] == "awaiting-input"
    eng.send_keys("exit()", enter=True)


def test_state_password_on_read_dash_s(bash_eng):
    r = bash_eng.run_command("read -s x", timeout=2)
    assert not r["completed"]
    assert r["state"] == "password"
    bash_eng.send_keys("secret", enter=True,
                        confirm_password_prompt=True)  # drain so teardown is clean


# -- password prompt enforcement (issue #24) --------------------------------
def test_send_keys_raises_at_password_prompt_without_confirm(bash_eng):
    # The README/docstrings market "password" state as "stop - only send
    # input here with the human's explicit consent," but nothing enforced
    # it: send_keys had no state check at all. Sending input to a password
    # prompt now requires a deliberate per-call opt-in.
    r = bash_eng.run_command("read -s x", timeout=2)
    assert not r["completed"] and r["state"] == "password"
    with pytest.raises(RuntimeError, match="password"):
        bash_eng.send_keys("secret", enter=True)
    bash_eng.send_keys("secret", enter=True,
                        confirm_password_prompt=True)  # drain so teardown is clean


def test_send_keys_allowed_at_password_prompt_with_confirm(bash_eng):
    r = bash_eng.run_command("read -s x", timeout=2)
    assert not r["completed"] and r["state"] == "password"
    r = bash_eng.send_keys("secret", enter=True, confirm_password_prompt=True)
    assert r["completed"]


def test_send_keys_confirm_flag_irrelevant_outside_password_state(bash_eng):
    # The flag only matters at a password prompt - passing it (or not) at
    # any other state must have no effect.
    r = bash_eng.run_command("python3", timeout=5)
    assert not r["completed"] and r["state"] == "awaiting-input"
    r = bash_eng.send_keys("print(6*7)", enter=True)
    assert "42" in r["screen"]
    bash_eng.send_keys("exit()", enter=True)


def test_state_tui_then_idle_after_quit(bash_eng):
    if not shutil.which("vim"):
        pytest.skip("vim not installed")
    bash_eng.run_command("vim -u NONE -N", timeout=5)
    scr = bash_eng.read_screen()
    assert scr["state"] == "tui"
    r = bash_eng.send_keys("\x1b:q!", enter=True)
    assert r["completed"]
    assert r["state"] == "idle"


def test_forged_mark_cannot_fake_exit_code(eng):
    # A program run in the session (here, the command line itself) emits an
    # un-nonced OSC 133 D;0 mark to try to fake a clean exit. It must be
    # ignored - the REAL, nonced D mark from `false`'s failure is what
    # closes the command - and the forgery attempt must be visible.
    if eng.shell.rsplit("/", 1)[-1] == "fish":
        pytest.skip("fish printf escape handling differs; nonce filtering is "
                    "covered at the unit level (test_structure.py) and by the "
                    "fish e2e tests below")
    r = eng.run_command(r"printf '\033]133;D;0\007'; false")
    assert r["completed"] is True
    assert r["exit_code"] == 1
    assert r.get("spoofed_marks", 0) >= 1


def test_probe_state_degrades_on_tcgetattr_failure(bash_eng, monkeypatch):
    # Mid-command (marks not idle) so the probe reaches the tcgetattr call.
    bash_eng.run_command("sleep 5", timeout=0.3)

    def _raise(fd):
        raise termios.error("simulated failure")

    monkeypatch.setattr(termios, "tcgetattr", _raise)
    with bash_eng._cond:
        state = bash_eng._probe_state()  # must not raise
    assert state == "running"            # degrades using marks alone
    bash_eng.send_keys("\x03")           # interrupt the sleep; clean teardown


def test_probe_state_degrades_on_tcgetpgrp_failure(bash_eng, monkeypatch):
    # Idle at the prompt: the degraded path should still say "idle".
    bash_eng.run_command("echo hi")

    def _raise(fd):
        raise OSError("simulated failure")

    monkeypatch.setattr(os, "tcgetpgrp", _raise)
    with bash_eng._cond:
        state = bash_eng._probe_state()  # must not raise
    assert state == "idle"


# -- specific cases (bash) -------------------------------------------------
def test_run_command_raises_when_not_idle(bash_eng):
    # Issue #18: run_command must not silently forward a new command into a
    # REPL/TUI that isn't idle - a command sent there lands INSIDE it (e.g.
    # `ls` typed into python3's `>>>` prompt), producing a confusing timeout
    # instead of a clear error the agent can act on.
    r = bash_eng.run_command("python3", timeout=5)
    assert not r["completed"] and r["state"] == "awaiting-input"
    with pytest.raises(RuntimeError, match="awaiting-input"):
        bash_eng.run_command("ls")
    bash_eng.send_keys("exit()", enter=True)  # clean teardown


def test_run_command_allowed_again_once_idle(bash_eng):
    bash_eng.run_command("python3", timeout=5)
    bash_eng.send_keys("exit()", enter=True)
    r = bash_eng.run_command("echo back")
    assert r == {"stdout": "back", "exit_code": 0, "completed": True, "state": "idle"}


def test_bash_subshell_exit_recovered(bash_eng):
    # First token '(' emits no C mark under bash-preexec; exit code still recovered.
    r = bash_eng.run_command("(exit 7)", timeout=4)
    assert r["exit_code"] == 7 and r["completed"]


def test_resize(bash_eng):
    bash_eng.resize(80, 24)
    assert bash_eng.run_command("tput cols")["stdout"] == "80"


def test_files_changed(bash_eng, tmp_path):
    bash_eng.set_watch_root(str(tmp_path))
    r = bash_eng.run_command(f"touch {tmp_path}/created.txt")
    assert r["files_changed"]["created"] == [str(tmp_path / "created.txt")]


def test_ctrl_c_interrupts(bash_eng):
    r = bash_eng.run_command("sleep 30", timeout=1.0)
    assert not r["completed"]
    r = bash_eng.send_keys("\x03")
    assert r["completed"] and r["exit_code"] is not None


def test_terminal_query_bytes_in_command_output_not_answered(bash_eng):
    # Issue #16: a command's OWN stdout containing what looks like a terminal
    # query byte sequence must NOT be treated as a genuine query and trigger
    # cleat to write a reply into the shell's stdin - that's spurious input
    # forged by the command's own output.
    writes = []
    real_write = bash_eng._proc.write

    def spy_write(data):
        writes.append(data)
        return real_write(data)

    bash_eng._proc.write = spy_write
    r = bash_eng.run_command(r"printf '\033[6n'")
    assert r["completed"]
    replies = [w for w in writes if w == b"\x1b[1;1R"]
    assert replies == [], f"query reply was injected into stdin: {writes}"


def test_terminal_query_gate_blocks_after_first_prompt_outside_altscreen(bash_eng):
    # Direct unit check of the gate itself, independent of real shell timing.
    bash_eng._struct.prompts_seen = 1
    bash_eng._altscreen = False
    writes = []
    bash_eng._proc.write = lambda data: writes.append(data)
    bash_eng._answer_terminal_queries(b"\x1b[6n")
    assert writes == []


def test_terminal_query_gate_allows_before_first_prompt(bash_eng):
    # The one legitimate case this responder exists for: a shell/TUI probing
    # terminal capabilities before it has ever shown a prompt (fish's own
    # startup DA1 query).
    bash_eng._struct.prompts_seen = 0
    bash_eng._altscreen = False
    writes = []
    bash_eng._proc.write = lambda data: writes.append(data)
    bash_eng._answer_terminal_queries(b"\x1b[6n")
    assert writes == [b"\x1b[1;1R"]


def test_terminal_query_gate_allows_during_altscreen(bash_eng):
    # A full-screen program legitimately owns the terminal and may query it.
    bash_eng._struct.prompts_seen = 5
    bash_eng._altscreen = True
    writes = []
    bash_eng._proc.write = lambda data: writes.append(data)
    bash_eng._answer_terminal_queries(b"\x1b[6n")
    assert writes == [b"\x1b[1;1R"]


def test_run_command_default_stdout_trimmed_not_exact(bash_eng):
    # Default `stdout` stays exactly as documented/tested today - trimmed for
    # readability - and carries no stdout_exact key unless opted in.
    r = bash_eng.run_command(r"printf 'a\n\n\n'")
    assert r["stdout"] == "a"
    assert "stdout_exact" not in r


def test_run_command_exact_preserves_trailing_newlines(bash_eng):
    # Issue #22 repro: printf 'a\n\n\n' must be recoverable byte-exact via the
    # opt-in `exact` flag, without changing the default `stdout` field.
    r = bash_eng.run_command(r"printf 'a\n\n\n'", exact=True)
    assert r["stdout"] == "a"
    assert r["stdout_exact"] == "a\n\n\n"


def test_run_command_exact_preserves_trailing_spaces(bash_eng):
    # Issue #22 repro: printf '  x  ' loses trailing spaces in the cleaned
    # field (rstrip trims the tail; leading spaces - not newlines - already
    # survive _clean's strip("\n")); `exact=True` recovers them exactly.
    r = bash_eng.run_command("printf '  x  '", exact=True)
    assert r["stdout"] == "  x"
    assert r["stdout_exact"] == "  x  "


def test_memory_bounded(bash_eng):
    for _ in range(8):
        bash_eng.run_command("head -c 200000 /dev/zero | tr '\\0' x", timeout=8)
    assert len(bash_eng._raw) <= 2 * _MAX_RAW and bash_eng._base > 0


def test_memory_bounded_during_single_large_output_command(bash_eng):
    # Issue #15: unlike test_memory_bounded (separate commands, cursor
    # advances between them), this streams ~5 MiB in ONE command while
    # run_command is still parked in its wait loop. Both the raw byte
    # buffer and the in-flight stdout accumulator must stay bounded WHILE
    # the command is still running, not just after it completes.
    done = threading.Event()

    def runner():
        bash_eng.run_command("yes | head -c 5000000", timeout=20)
        done.set()

    t = threading.Thread(target=runner)
    t.start()
    max_raw = max_stdout = 0
    while not done.is_set():
        max_raw = max(max_raw, len(bash_eng._raw))
        max_stdout = max(max_stdout, len(bash_eng._struct._stdout))
        time.sleep(0.02)
    t.join()
    assert max_raw <= 2 * _MAX_RAW, f"raw buffer grew unbounded mid-command: {max_raw}"
    assert max_stdout <= 2 * _MAX_RAW, f"stdout accumulator grew unbounded mid-command: {max_stdout}"


def test_concurrent_calls_serialized(bash_eng):
    errors = []

    def worker(tag):
        for i in range(10):
            r = bash_eng.run_command(f"echo {tag}-{i}")
            if r["stdout"] != f"{tag}-{i}" or r["exit_code"] != 0:
                errors.append((tag, i, r))

    ts = [threading.Thread(target=worker, args=(t,)) for t in ("A", "B", "C")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []


def test_stale_altscreen_cleared_when_tui_dies_uncleanly(bash_eng):
    # Issue #17: vim (or any TUI) killed without emitting its rmcup exit
    # sequence (SIGKILL, crash) must not leave state=="tui" stuck forever -
    # once a DIFFERENT, plain command takes over the foreground, the stale
    # flag must be recognized as stale and cleared.
    if not shutil.which("vim"):
        pytest.skip("vim not installed")
    bash_eng.run_command("vim -u NONE -N", timeout=5)
    scr = bash_eng.read_screen()
    assert scr["state"] == "tui"

    with bash_eng._cond:
        fg_pgid = os.tcgetpgrp(bash_eng._proc.fd)
    os.killpg(fg_pgid, signal.SIGKILL)  # no rmcup - simulates an unclean death
    time.sleep(0.5)                    # let the shell reclaim the terminal

    r = bash_eng.run_command("sleep 0.5", timeout=0.2)
    assert r["state"] == "running", f"stale altscreen misclassified state: {r}"


def test_tui_renders_and_quits(bash_eng):
    if not shutil.which("vim"):
        pytest.skip("vim not installed")
    bash_eng.run_command("vim -u NONE -N", timeout=5)
    scr = bash_eng.read_screen()
    assert "~" in scr["screen"]
    r = bash_eng.send_keys("\x1b:q!", enter=True)
    assert r["completed"]


# -- fish e2e: nonce-authenticated injection (issue #5) --------------------
# fish >= 4 emits its own native, un-nonced OSC 133 marks in addition to our
# injected, nonced ones. Before nonce filtering, fish was excluded from
# injection entirely to avoid doubled records; now the native marks are
# ignored outright (see structure.py), so these prove there's no doubling.
def test_fish_echo_false_persistence(fish_eng):
    r = fish_eng.run_command("echo hi", timeout=8)
    assert r == {"stdout": "hi", "exit_code": 0, "completed": True, "state": "idle"}

    r = fish_eng.run_command("false")
    assert r["exit_code"] == 1 and r["completed"]

    fish_eng.run_command("set -x FOO bar")
    r = fish_eng.run_command("echo $FOO")
    assert r["stdout"] == "bar"


def test_fish_no_doubled_records(fish_eng):
    # Exactly one CommandRecord per real command: if fish's native marks were
    # merely deduplicated rather than filtered out, this would be 2.
    fish_eng.run_command("echo one")
    fish_eng.run_command("echo two")
    fish_eng.run_command("echo three")
    assert fish_eng._struct.commands_started == 3
