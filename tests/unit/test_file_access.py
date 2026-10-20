"""Exclusive download files and root-confined local paths (file_access)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from openproject_mcp.client.errors import InputValidationError
from openproject_mcp.file_access import open_download_directory


def test_create_never_reuses_an_existing_name(tmp_path: Path) -> None:
    (tmp_path / "report.pdf").write_bytes(b"older")
    with open_download_directory(tmp_path, None) as directory:
        first = directory.create("report.pdf")
        second = directory.create("report.pdf")
        first.write(b"one")
        second.write(b"two")
        first.close()
        second.close()
    assert (tmp_path / "report.pdf").read_bytes() == b"older"
    assert first.path == tmp_path / "report (2).pdf"
    assert second.path == tmp_path / "report (3).pdf"
    assert first.path.read_bytes() == b"one"
    assert second.path.read_bytes() == b"two"


def test_create_refuses_to_write_through_a_planted_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"precious")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "report.pdf").symlink_to(outside)
    with open_download_directory(downloads, None) as directory:
        created = directory.create("report.pdf")
        created.write(b"new")
        created.close()
    assert created.path == downloads / "report (2).pdf"
    assert outside.read_bytes() == b"precious"


def test_discard_removes_only_the_file_it_created(tmp_path: Path) -> None:
    with open_download_directory(tmp_path, None) as directory:
        mine = directory.create("a.bin")
        mine.discard()
        assert not (tmp_path / "a.bin").exists()

        replaced = directory.create("b.bin")
        # Another writer replaces the name while this transfer is still open.
        (tmp_path / "other.bin").write_bytes(b"theirs")
        os.replace(tmp_path / "other.bin", tmp_path / "b.bin")
        replaced.discard()
    assert (tmp_path / "b.bin").read_bytes() == b"theirs"


def test_download_directory_is_created_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with open_download_directory(root / "a" / "b", root) as directory:
        created = directory.create("x.txt")
        created.close()
    assert (root / "a" / "b" / "x.txt").exists()


def test_download_directory_outside_the_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(InputValidationError, match="OPENPROJECT_MCP_DOWNLOAD_ROOT"):
        open_download_directory(tmp_path / "elsewhere", root)
    with pytest.raises(InputValidationError, match="OPENPROJECT_MCP_DOWNLOAD_ROOT"):
        open_download_directory(root / ".." / "elsewhere", root)
    assert not (tmp_path / "elsewhere").exists()


def test_download_directory_symlinked_out_of_the_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (root / "escape").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(InputValidationError, match="OPENPROJECT_MCP_DOWNLOAD_ROOT"):
        open_download_directory(root / "escape", root)
