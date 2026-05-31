"""Unit tests for the snapshot/diff file-watch source."""

import os

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
