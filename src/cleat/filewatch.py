#!/usr/bin/env python3
"""
filewatch.py - "what files did this command touch?" via snapshot/diff.

A bounded mtime+size snapshot of a directory tree, diffed before/after a command,
yields the files it created / modified / deleted. Because the engine runs one
shell serially, attribution to the command is safe.

HONEST SCOPE: this detects WRITES (create/modify/delete), not READS. Tracking
reads needs syscall tracing (dtrace/strace), which requires root and is blocked
by SIP on macOS. So this is the writes-half of "files touched" - useful and
cheap, with no extra dependency.

Cost control: ignores noisy/huge dirs (.git, node_modules, venvs, caches) and
caps the file count; if capped, `truncated` is True and the diff is unreliable
(point watch_files() at a specific project dir, not $HOME).

Known cost (issue #28, not addressed here): run_command calls snapshot()
twice per watched command (before/after), each a full synchronous os.walk -
latency scales with file count under the watched root regardless of how
much actually changed. The real fix is OS-level notification (inotify on
Linux, FSEvents on macOS), which this project deliberately doesn't pull in
as a dependency (see the module's "no extra dependency" framing above); a
directory-mtime-based caching layer was also considered, but a directory's
own mtime only changes on entry add/remove, not on an existing file's
content changing in place - it would still need to stat every file under an
unchanged directory to catch in-place edits, so the win is narrower than it
first looks and wasn't worth the added statefulness (this module is
currently a pure snapshot/diff pair with no cross-call cache to invalidate
correctly if watch_files() switches roots between commands). Left as a
follow-up if the latency actually bites in practice.
"""

import os


IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "headless-venv",
    ".mypy_cache", ".pytest_cache", ".idea", ".tox", ".gradle", "target",
}
MAX_FILES = 50_000


def snapshot(root):
    """Map path -> (ino, mtime_ns, ctime_ns, size) for files under root.
    Returns (snap, truncated)."""
    snap = {}
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            # (mtime_ns, size) alone missed an edit that preserves both -
            # e.g. `cp -p`/`tar -x` restoring original timestamps, or a
            # deliberate `touch -r original modified` after an edit (issue
            # #28). st_ctime_ns changes on ANY metadata or content write,
            # even when mtime is deliberately restored afterward - unlike
            # mtime, userspace has no portable way to set ctime to an
            # arbitrary value, so a forged-timestamp edit still shows up
            # here. st_ino is cheap insurance against inode reuse at the
            # same path being missed if mtime/ctime/size ever happened to
            # coincide too - matches the issue's own suggested key exactly.
            snap[p] = (st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)
            if len(snap) >= MAX_FILES:
                return snap, True
    return snap, truncated


def diff(before, after):
    """Compare two snapshots -> {created, modified, deleted} (sorted path lists)."""
    bset, aset = set(before), set(after)
    created = sorted(aset - bset)
    deleted = sorted(bset - aset)
    modified = sorted(p for p in (aset & bset) if before[p] != after[p])
    return {"created": created, "modified": modified, "deleted": deleted}


if __name__ == "__main__":
    # Self-test: create/modify/delete under a temp dir and check the diff.
    import tempfile
    import shutil

    d = tempfile.mkdtemp(prefix="filewatch-test-")
    try:
        keep = os.path.join(d, "keep.txt")
        gone = os.path.join(d, "gone.txt")
        with open(keep, "w") as f:
            f.write("v1")
        with open(gone, "w") as f:
            f.write("bye")

        before, _ = snapshot(d)
        # mutate: create one, modify one, delete one.
        new = os.path.join(d, "new.txt")
        with open(new, "w") as f:
            f.write("hi")
        with open(keep, "w") as f:
            f.write("v2-longer")
        os.remove(gone)
        after, _ = snapshot(d)

        result = diff(before, after)
        ok = (result["created"] == [new]
              and result["modified"] == [keep]
              and result["deleted"] == [gone])
        print("ok  filewatch diff" if ok else f"FAIL {result}")
    finally:
        shutil.rmtree(d, ignore_errors=True)
