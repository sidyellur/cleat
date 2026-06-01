#!/usr/bin/env python3
"""
engine.py - the persistent PTY engine.

microterm.py is the *interactive* pump (human types, human reads). The engine is
the *programmatic* version: it holds the master side of a PTY around a long-lived
shell and exposes a small API an MCP server can drive.

It reuses `ptyprocess` for the PTY plumbing, injects OSC 133 via inject.py, and
feeds the raw byte stream into the StructureSource. A background thread reads the
PTY continuously into two places at once:
  - the StructureSource, which closes a CommandRecord on each C->D pair
  - a raw byte buffer with a cursor, so callers can read partial output

Two completion signals, because the D mark alone isn't enough:
  - D mark        -> the command finished; we have a real exit code.
  - output idle   -> bytes stopped flowing with no D; the program is probably
                     waiting for input (a REPL/prompt) or just paused. We return
                     what we have and say completed=False instead of hanging.

API:
    eng = Engine().start()
    eng.run_command("ls")                  # {stdout, exit_code, completed}
    eng.run_command("python3", timeout=5)  # completed=False, stdout has the banner+'>>>'
    eng.send_keys("print(6*7)", enter=True)# {output, exit_code, completed}
    eng.read_output()                      # poll a long-runner; {output, exit_code, completed}
    eng.close()

Scope: line-oriented interactive programs (REPLs, prompts, streaming output).
Full-screen TUIs (vim/top) emit cursor-addressing that only means anything when
rendered into a screen grid - that needs a terminal emulator (pyte) and is a
separate step, not handled here.
"""

import os
import time
import shutil
import threading

import ptyprocess
import pyte

from . import filewatch
from .structure import StructureSource, _clean
from .inject import prepare

# Keep the raw-byte window bounded; the consumed prefix is dropped past this.
_MAX_RAW = 1 << 20  # 1 MiB
# Keep only the most recent records; older ones are evicted (callers use
# absolute indices via _rec_base, so eviction is transparent).
_MAX_RECORDS = 256


def _serialized(method):
    """Serialize agent-facing calls: one command/read drives the single shell at
    a time, so two concurrent tool calls can't interleave writes on the PTY or
    cross their record correlation. (Held across the call's internal waits.)"""
    def wrapper(self, *args, **kwargs):
        with self._api_lock:
            return method(self, *args, **kwargs)
    wrapper.__name__ = method.__name__
    wrapper.__doc__ = method.__doc__
    return wrapper


class Engine:
    def __init__(self, shell=None, inject=True, cols=120, rows=40, watch_root=None):
        self.shell = shell or os.environ.get("SHELL", "/bin/zsh")
        self.inject = inject
        self.dims = (rows, cols)
        self._watch_root = watch_root   # if set, run_command reports files_changed
        self._proc = None
        self._inject_dir = None
        self._struct = StructureSource()

        # Second consumer of the same byte stream: a virtual screen. pyte
        # interprets cursor moves/clears/colors so we can read what the terminal
        # *looks like* (clean REPL lines, rendered TUIs) - it ignores OSC 133,
        # which the StructureSource handles instead.
        self._screen = pyte.Screen(cols, rows)
        self._pyte = pyte.ByteStream(self._screen)

        # Shared state, guarded by _cond. The reader thread is the only writer;
        # callers read under the lock and wait on the cond. To stay bounded over
        # a long-lived session, both buffers keep only a recent window and track
        # an absolute base index of element [0], so positions/indices are
        # absolute (eviction-invariant) and old data can be dropped. run_command
        # takes the FIRST new record (a command may emit two marks - e.g. fish
        # 4.x native + our injected - and only the first carries the output).
        self._raw = bytearray()      # recent window of bytes the shell emitted
        self._base = 0               # absolute index of _raw[0] (bytes dropped)
        self._cursor = 0             # absolute index of next unconsumed byte
        self._records = []           # recent CommandRecords
        self._rec_base = 0           # absolute index of _records[0] (count evicted)
        self._cond = threading.Condition()
        self._api_lock = threading.Lock()  # serializes agent-facing calls
        self._reader = None
        self._alive = False

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        base_env = os.environ.copy()
        if self.inject:
            argv, env, self._inject_dir = prepare(self.shell, base_env)
        else:
            argv, env = [self.shell], base_env
        # A real terminal always sets TERM; an MCP server spawned without a tty
        # (or CI) may not, which breaks tput/vim/less/fish. We render an xterm via
        # pyte, so advertise that when nothing else is set.
        env.setdefault("TERM", "xterm-256color")
        self._proc = ptyprocess.PtyProcess.spawn(
            argv, env=env, dimensions=self.dims
        )
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        return self

    def _read_loop(self):
        while self._alive:
            try:
                data = self._proc.read(4096)
            except (EOFError, OSError):
                break
            if not data:
                break
            self._answer_terminal_queries(data)
            with self._cond:
                # Feed under the lock so struct state (commands_started), the
                # raw buffer, and the screen advance atomically for readers.
                recs = self._struct.feed(data)
                self._raw += data
                self._records.extend(recs)
                try:
                    self._pyte.feed(data)
                except Exception:
                    pass  # never let a rendering hiccup kill the read loop
                # Bound memory: drop the consumed raw prefix + evict old records.
                if len(self._raw) > _MAX_RAW:
                    drop = self._cursor - self._base
                    if drop > 0:
                        del self._raw[:drop]
                        self._base += drop
                if len(self._records) > _MAX_RECORDS:
                    drop = len(self._records) - _MAX_RECORDS
                    del self._records[:drop]
                    self._rec_base += drop
                self._cond.notify_all()
        with self._cond:
            self._alive = False
            self._cond.notify_all()

    def close(self):
        self._alive = False
        try:
            self._proc.write(b"exit\n")
            self._proc.close(force=True)
        except Exception:
            pass
        if self._inject_dir:
            shutil.rmtree(self._inject_dir, ignore_errors=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def resize(self, cols, rows):
        """Resize both the PTY and the virtual screen so TUIs relay out."""
        with self._cond:
            self.dims = (rows, cols)
            try:
                self._proc.setwinsize(rows, cols)
            except Exception:
                pass
            self._screen.resize(rows, cols)
        return {"cols": cols, "rows": rows}

    def set_watch_root(self, path):
        """Enable/disable the files-touched feature. path='' or None disables."""
        self._watch_root = os.path.abspath(path) if path else None
        return {"watch_root": self._watch_root}

    # -- internals ----------------------------------------------------------
    def _answer_terminal_queries(self, data):
        """Reply to terminal capability queries so probing programs don't block.

        Some shells/TUIs (notably fish 4.x) refuse to draw a prompt until the
        terminal answers DA1 / cursor-position / background-color queries. Under
        a bare PTY nobody answers, so they hang forever. We send minimal canned
        replies. Harmless for shells that never ask (zsh/bash)."""
        try:
            if b"\x1b[c" in data or b"\x1b[0c" in data:
                self._proc.write(b"\x1b[?62;c")                      # DA1
            if b"\x1b[6n" in data:
                self._proc.write(b"\x1b[1;1R")                       # cursor pos
            if b"\x1b]11;?" in data:
                self._proc.write(b"\x1b]11;rgb:0000/0000/0000\x1b\\")  # bg color
        except Exception:
            pass

    def _total(self):
        """Absolute count of bytes ever emitted. Caller holds _cond."""
        return self._base + len(self._raw)

    def _rec_total(self):
        """Absolute count of records ever produced. Caller holds _cond."""
        return self._rec_base + len(self._records)

    def _drain(self):
        """Return raw bytes since the cursor and advance it. Caller holds _cond."""
        chunk = bytes(self._raw[self._cursor - self._base:])
        self._cursor = self._total()
        return chunk

    def _render_screen(self):
        """Snapshot the virtual screen as text + cursor. Caller holds _cond."""
        lines = [line.rstrip() for line in self._screen.display]
        while lines and not lines[-1]:   # trim trailing blank rows
            lines.pop()
        return "\n".join(lines), [self._screen.cursor.x, self._screen.cursor.y]

    def _read_until_idle(self, timeout, idle):
        """Collect output until it goes quiet for `idle`s or `timeout`s elapses.
        Caller holds _cond. Returns the raw bytes collected."""
        end = time.monotonic() + timeout
        start_rc = self._rec_total()
        out = bytearray()
        # Wait for the first byte (or a record, or timeout).
        while (self._cursor >= self._total()
               and self._rec_total() == start_rc and self._alive):
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            self._cond.wait(remaining)
        out += self._drain()
        # Then keep collecting until an idle gap with nothing new.
        while self._alive and time.monotonic() < end:
            self._cond.wait(idle)
            new = self._drain()
            if new:
                out += new
                continue
            break
        return bytes(out)

    # -- the agent-facing API ----------------------------------------------
    @_serialized
    def run_command(self, cmd, timeout=10.0, idle=0.4) -> dict:
        """Run a command. Returns {stdout, exit_code, completed}.

        completed=True  -> a D mark closed the command; exit_code is real.
        completed=False -> output went idle with no D: the program is waiting
                           for input or still running. stdout is what we have so
                           far; follow up with send_keys()/read_output().
        """
        if not self._alive:
            raise RuntimeError("engine not started (or already closed)")
        before = trunc_before = None
        if self._watch_root:
            before, trunc_before = filewatch.snapshot(self._watch_root)
        with self._cond:
            start_rc = self._rec_total()
            start_started = self._struct.commands_started
            self._proc.write((cmd + "\n").encode())
            end = time.monotonic() + timeout
            prev_len = self._total()
            while self._alive:
                if self._rec_total() > start_rc:
                    break
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(min(idle, remaining))
                if self._rec_total() > start_rc:
                    break
                cur_len = self._total()
                c_seen = self._struct.commands_started > start_started
                # Bail to "interactive" only when the command has started AND the
                # parser has captured REAL stdout (not just terminal chrome like a
                # title-set OSC) AND this wait added nothing. Keying on
                # partial_stdout (ANSI/OSC-stripped) ignores the command echo
                # (pre-C), silent commands like `sleep`, and post-C chrome that
                # some shells (fish) emit before any output.
                idle_now = (cur_len == prev_len)
                has_real_output = c_seen and bool(self._struct.partial_stdout())
                if idle_now and has_real_output:
                    break
                prev_len = cur_len

            if self._rec_total() > start_rc:
                # FIRST new record (a doubled-mark command's 2nd record is empty).
                rec = self._records[start_rc - self._rec_base]
                self._cursor = self._total()
                result = {"stdout": rec.stdout, "exit_code": rec.exit_code,
                          "completed": True}
            else:
                # Not completed: hand back the clean post-C output if we have it.
                self._cursor = self._total()
                result = {"stdout": self._struct.partial_stdout(),
                          "exit_code": None, "completed": False}

        # files-touched: diff the watched tree once the command has finished.
        if before is not None and result["completed"]:
            after, trunc_after = filewatch.snapshot(self._watch_root)
            changed = filewatch.diff(before, after)
            if trunc_before or trunc_after:
                changed["truncated"] = True  # tree too big; result unreliable
            result["files_changed"] = changed
        return result

    @_serialized
    def read_output(self, timeout=2.0, idle=0.4) -> dict:
        """Poll for output without sending anything (e.g. watch a long-runner).
        Returns {output, exit_code, completed}; exit_code is set if a command
        finished while we were reading."""
        if not self._alive:
            raise RuntimeError("engine not started (or already closed)")
        with self._cond:
            # Shell idle at a prompt AND nothing buffered => the previous command
            # finished and nothing more is coming. Report that immediately with
            # the real last exit code instead of blocking the full timeout and
            # returning a misleading completed=False -- that false signal is what
            # pushed callers into defensive over-polling (issue #1). The
            # nothing-buffered check matters: a command that finished *between*
            # polls leaves its output unread, and we must NOT discard it here.
            if (self._struct.idle and self._cursor >= self._total()
                    and self._rec_total() > 0):
                return {"output": "", "exit_code": self._records[-1].exit_code,
                        "completed": True}
            raw = self._read_until_idle(timeout, idle)
            # Completion = the shell is now back at a prompt, not merely "a record
            # formed during THIS call". A command that finished between polls
            # already has its record, so keying on record growth alone would drop
            # its completion signal even though we just drained its output.
            done = self._struct.idle and self._rec_total() > 0
            exit_code = self._records[-1].exit_code if done else None
            return {"output": _clean(raw), "exit_code": exit_code,
                    "completed": done}

    @_serialized
    def read_screen(self, settle=0.3, timeout=1.0) -> dict:
        """Return the rendered virtual screen (what the terminal looks like now)
        plus the cursor [x, y]. Briefly waits for output to settle first so a
        mid-redraw frame isn't captured. Use this for TUIs and REPLs; use
        read_output() for streaming text you don't want truncated to the screen."""
        if not self._alive:
            raise RuntimeError("engine not started (or already closed)")
        with self._cond:
            # If the shell is idle with nothing pending the screen is already
            # stable - render now instead of blocking for the settle/timeout
            # (issue #1). A running program (TUI/REPL) still settles first.
            if not (self._struct.idle and self._cursor >= self._total()):
                self._read_until_idle(timeout, settle)  # flush pending bytes
            screen, cursor = self._render_screen()
            return {"screen": screen, "cursor": cursor}

    @_serialized
    def send_keys(self, keys, enter=False, timeout=2.0, idle=0.4) -> dict:
        """Send raw input to the running program, then return the rendered screen.

        Control chars go through as-is: "\\u0003"=Ctrl-C, "\\u0004"=Ctrl-D.
        Set enter=True to append a newline. Returns {screen, exit_code,
        completed}; the screen is pyte-rendered so REPL/TUI output is clean (no
        per-keystroke redraw noise). completed=True (with exit_code) if the
        program exited.
        """
        if not self._alive:
            raise RuntimeError("engine not started (or already closed)")
        payload = keys + ("\n" if enter else "")
        with self._cond:
            start_rc = self._rec_total()
            self._proc.write(payload.encode())
            self._read_until_idle(timeout, idle)
            done = self._rec_total() > start_rc
            exit_code = self._records[-1].exit_code if done else None
            screen, cursor = self._render_screen()
            return {"screen": screen, "cursor": cursor, "exit_code": exit_code,
                    "completed": done}


# ---------------------------------------------------------------------------
# Self-test: drive a real shell, including interactive programs. Headless.
if __name__ == "__main__":
    eng = Engine().start()
    results = []

    def check(label, ok, detail=""):
        results.append(ok)
        print(f"{'ok ' if ok else 'FAIL'} {label}{(' -> ' + detail) if detail else ''}")

    try:
        r = eng.run_command("echo hello")
        check("one-shot echo", r == {"stdout": "hello", "exit_code": 0, "completed": True}, str(r))

        r = eng.run_command("false")
        check("exit code 1", r["exit_code"] == 1 and r["completed"], str(r))

        r = eng.run_command("export FOO=bar")
        r = eng.run_command("echo $FOO")
        check("persistence", r["stdout"] == "bar", str(r))

        # long-running but silent until the end: should COMPLETE (no false idle).
        r = eng.run_command("sleep 1; echo woke", timeout=5)
        check("slow-but-completes", r["completed"] and r["stdout"] == "woke", str(r))

        # interactive REPL: run_command should NOT hang; returns completed=False.
        r = eng.run_command("python3", timeout=8)
        check("repl starts (not completed)",
              (not r["completed"]) and (">>>" in r["stdout"]), repr(r["stdout"][-40:]))

        r = eng.send_keys("print(6*7)", enter=True)
        # screen-rendered: the answer is present AND the per-keystroke redraw
        # noise (">>> p>>> pr") is gone.
        check("repl computes (clean screen)",
              "42" in r["screen"] and ">>> p>>> pr" not in r["screen"], repr(r["screen"][-60:]))

        r = eng.send_keys("exit()", enter=True)
        check("repl exits (exit code)", r["completed"] and r["exit_code"] is not None, str(r))

        # interrupt a hung command with Ctrl-C.
        r = eng.run_command("sleep 30", timeout=1.0)
        check("sleep not completed", not r["completed"], str(r))
        r = eng.send_keys("")  # Ctrl-C
        check("ctrl-c interrupts", r["completed"] and r["exit_code"] is not None, str(r))

        # full-screen TUI: vim should render (empty buffer shows '~' rows), then quit.
        r = eng.run_command("vim -u NONE -N", timeout=4)
        scr = eng.read_screen()
        check("vim renders (TUI)", "~" in scr["screen"], repr(scr["screen"][:60]))
        r = eng.send_keys("\x1b:q!", enter=True)  # ESC then :q!
        check("vim quits", r["completed"], str({k: r[k] for k in ("exit_code", "completed")}))

        # (3) dynamic resize: PTY width should follow.
        eng.resize(80, 24)
        r = eng.run_command("tput cols")
        check("resize -> tput cols=80", r["stdout"] == "80", str(r))

        # (2) files a command touched: watch a temp dir, mutate it, see the diff.
        import tempfile as _tf
        wd = _tf.mkdtemp(prefix="engine-watch-")
        eng.set_watch_root(wd)
        r = eng.run_command(f"touch {wd}/created.txt")
        fc = r.get("files_changed", {})
        check("files_changed reports create",
              fc.get("created") == [os.path.join(wd, "created.txt")], str(fc))
        eng.set_watch_root(None)
        shutil.rmtree(wd, ignore_errors=True)

        # memory bound: push >1 MiB of output through; the consumed raw prefix
        # must be dropped (so _base advances and _raw stays bounded).
        for _ in range(8):
            eng.run_command("head -c 200000 /dev/zero | tr '\\0' x", timeout=8)
        check("raw buffer bounded after >1.5MiB output",
              len(eng._raw) <= 2 * _MAX_RAW and eng._base > 0,
              f"len(_raw)={len(eng._raw)} base={eng._base}")

        print("\nALL PASS" if all(results) else "\nSOME FAILED")
    finally:
        eng.close()

    # (1) bash injection (bash-preexec): a SEPARATE engine on /bin/bash.
    print("\n--- bash injection (bash-preexec) ---")
    if os.path.exists("/bin/bash"):
        beng = Engine(shell="/bin/bash").start()
        try:
            rb = beng.run_command("echo hi")
            print(f"{'ok ' if rb == {'stdout':'hi','exit_code':0,'completed':True} else 'FAIL'} bash echo -> {rb}")
            rb = beng.run_command("false")
            print(f"{'ok ' if rb['exit_code']==1 and rb['completed'] else 'FAIL'} bash false exit=1 -> {rb}")
            # subshell first-token: no C mark; parser must still recover exit code.
            rb = beng.run_command("(exit 7)", timeout=4)
            print(f"{'ok ' if rb['exit_code']==7 and rb['completed'] else 'FAIL'} bash subshell exit=7 (no-C recovery) -> {rb}")
        finally:
            beng.close()
    else:
        print("skip - /bin/bash not found")

    # fish injection: needs the terminal-query responder so fish 4.x doesn't hang.
    print("\n--- fish injection ---")
    fish = shutil.which("fish")
    if fish:
        feng = Engine(shell=fish).start()
        try:
            rf = feng.run_command("echo hi", timeout=8)
            print(f"{'ok ' if rf == {'stdout':'hi','exit_code':0,'completed':True} else 'FAIL'} fish echo -> {rf}")
            rf = feng.run_command("false")
            print(f"{'ok ' if rf['exit_code']==1 and rf['completed'] else 'FAIL'} fish false exit=1 -> {rf}")
            rf = feng.run_command("echo second")
            print(f"{'ok ' if rf['stdout']=='second' else 'FAIL'} fish no-drift -> {rf}")
        finally:
            feng.close()
    else:
        print("skip - fish not found")
