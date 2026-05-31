#!/usr/bin/env python3
"""
microterm - a ~90-line program that shows you what a terminal actually is.

A "terminal" is three things glued together:
  1. a PTY      - a kernel pipe pretending to be a serial line + keyboard
  2. a shell    - a child process running on the far side of that PTY
  3. a pump     - a loop moving bytes between your real keyboard/screen
                  and that PTY

This program is all three. It is also, structurally, *exactly what an agent
like Claude Code or Codex is*: a program holding the MASTER side of a PTY,
feeding input in and scraping output out. The only difference here is that
the thing reading the output is you, not a model.

Usage:
    python3 microterm.py                 # a working shell, in ~90 lines
    python3 microterm.py --spy bytes.log # also log the RAW byte stream

With --spy, open a second window and run `tail -f bytes.log` while you use
this shell. You'll see what your shell *actually* sends - carriage returns,
color codes, screen clears, the alternate-screen switch when you launch vim.
That raw stream is the thing an agent has to make sense of. Watching it is
how "the terminal is archaic" stops being a vibe and becomes specific.
"""

import os
import pty
import sys
import tty
import termios
import select
import signal
import fcntl
import argparse
import shutil

from .structure import StructureSource
from .inject import prepare


def sync_winsize(master_fd):
    """Copy our real window size onto the PTY so vim/htop/etc. lay out right."""
    try:
        size = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass  # not a real tty (e.g. piped) - nothing to sync


def main():
    ap = argparse.ArgumentParser(description="a terminal in ~90 lines")
    ap.add_argument("--spy", metavar="FILE",
                    help="log every byte the shell sends us, escape codes visible")
    ap.add_argument("--inject", action="store_true",
                    help="inject OSC 133 shell integration into the spawned shell")
    ap.add_argument("--structure", metavar="FILE",
                    help="parse OSC 133 marks and write CommandRecords (JSON lines) here")
    args = ap.parse_args()

    spy = open(args.spy, "w") if args.spy else None
    struct = StructureSource() if args.structure else None
    struct_out = open(args.structure, "w") if args.structure else None
    shell = os.environ.get("SHELL", "/bin/bash")

    # Set up OSC 133 injection (zsh/bash/fish) before we fork.
    inject_dir = None
    if args.inject:
        argv, child_env, inject_dir = prepare(shell, os.environ.copy())
    else:
        argv, child_env = [shell], None

    # pty.fork() splits us in two:
    #   child  -> its stdin/stdout/stderr ARE the PTY slave; we just exec a shell
    #   parent -> we get master_fd, the OTHER end. This is the "agent" handle.
    pid, master_fd = pty.fork()

    if pid == 0:
        # CHILD: become the shell. From here on, it simply *is* the shell.
        if child_env is not None:
            os.execvpe(argv[0], argv, child_env)
        else:
            os.execvp(argv[0], argv)
        os._exit(1)  # only reached if exec fails

    # PARENT: we are now "the terminal" (and the agent).

    # Put our real keyboard in raw mode so keystrokes pass through untouched.
    # (The PTY's line discipline does echo/buffering on the other side.)
    old_attrs = termios.tcgetattr(sys.stdin)
    tty.setraw(sys.stdin.fileno())

    sync_winsize(master_fd)
    signal.signal(signal.SIGWINCH, lambda *_: sync_winsize(master_fd))

    try:
        while True:
            # Block until our keyboard OR the shell has bytes ready.
            readable, _, _ = select.select([sys.stdin, master_fd], [], [])

            if sys.stdin in readable:
                data = os.read(sys.stdin.fileno(), 1024)
                if not data:
                    break
                os.write(master_fd, data)            # keystrokes -> shell

            if master_fd in readable:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break                            # shell exited
                if not data:
                    break
                os.write(sys.stdout.fileno(), data)  # shell output -> screen
                if spy:
                    # repr() makes escape codes visible: \x1b[31m instead of red.
                    spy.write(repr(data)[2:-1] + "\n")
                    spy.flush()
                if struct:
                    # Same raw bytes, but through the glasses: emit structured
                    # CommandRecords as they complete (the engine->structure seam).
                    for rec in struct.feed(data):
                        struct_out.write(rec.as_json() + "\n")
                        struct_out.flush()
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSAFLUSH, old_attrs)
        if spy:
            spy.close()
        if struct_out:
            struct_out.close()
        if inject_dir:
            shutil.rmtree(inject_dir, ignore_errors=True)
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass


if __name__ == "__main__":
    main()
