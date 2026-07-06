"""Integration tests for the Engine. These spawn real shells under a PTY.

Core behaviors are parametrized over whichever supported shells are installed
(bash/zsh/fish); shell- or program-specific cases skip when unavailable, so the
suite runs anywhere (CI included)."""

import os
import re
import shutil
import subprocess
import termios
import threading
import time

import pytest

from cleat.engine import Engine, _MAX_RAW


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
    bash_eng.send_keys("secret", enter=True)  # drain so teardown is clean


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


def test_pyte_feed_does_not_backpressure_pty_draining(bash_eng, monkeypatch):
    # Issue #23: pyte.feed() ran synchronously on the reader thread for every
    # chunk, so a pathologically slow render (pyte is pure-Python) delayed
    # draining the PTY for the WHOLE session, not just screen-reading calls.
    # Simulate that slowness and confirm a plain streaming command still
    # completes promptly - proving the reader thread isn't waiting on it.
    real_feed = bash_eng._pyte.feed

    def slow_feed(data):
        time.sleep(0.05)
        return real_feed(data)

    monkeypatch.setattr(bash_eng._pyte, "feed", slow_feed)

    t0 = time.monotonic()
    r = bash_eng.run_command("head -c 200000 /dev/zero | tr '\\0' x", timeout=10)
    elapsed = time.monotonic() - t0
    assert r["completed"]
    assert elapsed < 1.0, f"run_command was backpressured by slow pyte feed: {elapsed}s"


def test_memory_bounded(bash_eng):
    for _ in range(8):
        bash_eng.run_command("head -c 200000 /dev/zero | tr '\\0' x", timeout=8)
    assert len(bash_eng._raw) <= 2 * _MAX_RAW and bash_eng._base > 0


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
