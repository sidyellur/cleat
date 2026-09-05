"""Unit tests for the snapshot/diff file-watch source."""

import os
import time

from cleat import filewatch


def test_diff_create_modify_delete(tmp_path):
    keep = tmp_path / "keep.txt"
    gone = tmp_path / "gone.txt"
    keep.write_text("v1")
    gone.write_text("bye")

    before, _ = filewatch.snapshot(str(tmp_path))
    new = tmp_path / "new.txt"
    new.write_text("hi")
    keep.write_text("v2-longer")
    gone.unlink()
    after, _ = filewatch.snapshot(str(tmp_path))

    result = filewatch.diff(before, after)
    assert result["created"] == [str(new)]
    assert result["modified"] == [str(keep)]
    assert result["deleted"] == [str(gone)]


def test_ignores_noisy_dirs(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "obj").write_text("x")
    (tmp_path / "real.txt").write_text("y")
    snap, _ = filewatch.snapshot(str(tmp_path))
    assert os.path.join(str(tmp_path), "real.txt") in snap
    assert not any(".git" in p for p in snap)


def test_diff_detects_content_change_with_preserved_mtime_and_size(tmp_path):
    # Issue #28: (mtime_ns, size) alone misses an edit that preserves both -
    # e.g. `cp -p`/`tar -x` restoring original timestamps, or a deliberate
    # `touch -r original modified` after an edit. Same-length new content
    # (AAAA -> BBBB, both 4 bytes) with the mtime explicitly restored must
    # still show up as modified.
    f = tmp_path / "f"
    f.write_text("AAAA")
    before, _ = filewatch.snapshot(str(tmp_path))

    orig_stat = os.stat(f)
    time.sleep(0.01)
    f.write_text("BBBB")
    # ns= for full nanosecond precision - matches what `touch -r`/utimensat
    # actually achieves; the float form loses precision and wouldn't
    # reproduce the exact original mtime_ns.
    os.utime(f, ns=(orig_stat.st_atime_ns, orig_stat.st_mtime_ns))
    after, _ = filewatch.snapshot(str(tmp_path))

    result = filewatch.diff(before, after)
    assert str(f) in result["modified"], \
        f"content change with preserved mtime+size was missed: {result}"
