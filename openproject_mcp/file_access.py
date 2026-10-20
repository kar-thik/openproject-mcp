"""Local file access for attachment uploads and downloads (SPEC §7.1/§7.2).

Two guarantees live here so the attachment tools cannot get them wrong:

* **Downloads never clobber.** A target file is reserved with exclusive
  creation (``O_EXCL``), retrying ``name (2).ext``, ``name (3).ext`` … on
  collision, so two concurrent downloads of the same name get distinct files.
  A failed transfer removes only the file it created itself.
* **Optional root confinement.** With ``OPENPROJECT_MCP_UPLOAD_ROOT`` /
  ``OPENPROJECT_MCP_DOWNLOAD_ROOT`` set, every path is resolved (symlinks
  included) and must land inside the root. The final open walks the resolved
  path one component at a time from a descriptor on the root with
  ``O_NOFOLLOW``, so a symlink swapped in after the check fails the open rather
  than escaping the root.

Platforms without ``dir_fd`` support (Windows) fall back to path-based
operations: exclusive creation still holds, root confinement is checked on the
resolved path but cannot rule out a concurrent symlink swap.
"""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path
from typing import BinaryIO

from openproject_mcp.client.errors import InputValidationError

__all__ = ["DownloadDirectory", "DownloadFile", "open_download_directory", "open_upload_source"]

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)
#: Whether descriptor-relative opens are available (POSIX); see the module docstring.
_DIR_FD = os.open in os.supports_dir_fd and bool(_O_DIRECTORY)

#: Collision suffixes tried before giving up on a file name.
MAX_NAME_ATTEMPTS = 1000


def _outside_root(path: Path, root: Path, setting: str) -> InputValidationError:
    return InputValidationError(
        f"{path} is outside {setting} ({root}).",
        hint=(
            f"This server only touches files under {root}. Pass a path inside it; symlinks that "
            "resolve outside it are refused too."
        ),
    )


def _walk_beneath(root: Path, relative: Path, *, create: bool) -> int:
    """Open the directory ``root/relative`` without following any symlink below ``root``.

    ``root`` is operator configuration and opened normally; every component of
    ``relative`` is opened with ``O_NOFOLLOW`` relative to its parent, and
    created first when ``create`` is set.
    """
    fd = os.open(root, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    try:
        for part in relative.parts:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, dir_fd=fd)
            child = os.open(part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


# --- uploads ----------------------------------------------------------------


def open_upload_source(file_path: str, root: Path | None) -> tuple[Path, BinaryIO]:
    """Open a local file for upload, confined to ``root`` when one is configured.

    Returns the path and an open binary handle; the caller closes it. The
    handle is checked to be a regular file *after* opening as well, so what is
    read is what was checked.
    """
    source = Path(file_path).expanduser()
    if root is not None and not source.is_absolute():
        source = root / source
    if not source.exists():
        raise InputValidationError(
            f"No such file: {source}.",
            hint=(
                "Pass an absolute path to a file on the machine running this MCP server. "
                "Nothing was uploaded."
            ),
        )
    not_regular = InputValidationError(
        f"{source} is not a regular file.",
        hint="Attachments are single files; archive a directory before uploading it.",
    )
    if not source.is_file():
        raise not_regular

    try:
        if root is None:
            handle = source.open("rb")
        else:
            resolved_root = root.resolve()
            resolved = source.resolve()
            if not resolved.is_relative_to(resolved_root):
                raise _outside_root(source, resolved_root, "OPENPROJECT_MCP_UPLOAD_ROOT")
            handle = _open_confined(resolved, resolved_root)
    except OSError as exc:
        raise InputValidationError(
            f"Could not open {source}: {exc.strerror}.",
            hint=(
                "The path changed while it was being opened, or the server process may not "
                "read it. Nothing was uploaded."
            ),
        ) from exc
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        handle.close()
        raise not_regular
    return source, handle


def _open_confined(resolved: Path, root: Path) -> BinaryIO:
    """Open ``resolved`` (already checked to be under ``root``) without escaping it."""
    if not _DIR_FD:
        return resolved.open("rb")
    relative = resolved.relative_to(root)
    parent = _walk_beneath(root, relative.parent, create=False)
    try:
        fd = os.open(relative.name, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=parent)
    finally:
        os.close(parent)
    return os.fdopen(fd, "rb")


# --- downloads --------------------------------------------------------------


class DownloadFile:
    """A file this process created exclusively, plus the means to remove it again."""

    def __init__(self, directory: DownloadDirectory, name: str, fd: int) -> None:
        self.path = directory.path / name
        self._directory = directory
        self._name = name
        self.handle: BinaryIO = os.fdopen(fd, "wb")

    def write(self, chunk: bytes) -> None:
        self.handle.write(chunk)

    def close(self) -> None:
        self.handle.close()

    def discard(self) -> None:
        """Close and delete the file — but only while the name still points at it."""
        identity: os.stat_result | None = None
        with contextlib.suppress(OSError, ValueError):
            identity = os.fstat(self.handle.fileno())
        with contextlib.suppress(OSError):
            self.handle.close()
        if identity is None:
            return
        with contextlib.suppress(OSError):
            current = self._directory.stat(self._name)
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                self._directory.unlink(self._name)


class DownloadDirectory:
    """An open download directory; files are created relative to it."""

    def __init__(self, path: Path, fd: int | None) -> None:
        self.path = path
        self._fd = fd

    def __enter__(self) -> DownloadDirectory:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def create(self, file_name: str) -> DownloadFile:
        """Create ``file_name`` exclusively, or ``stem (n).ext`` if it is taken."""
        stem, suffix = Path(file_name).stem, Path(file_name).suffix
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY
        for attempt in range(1, MAX_NAME_ATTEMPTS + 1):
            name = file_name if attempt == 1 else f"{stem} ({attempt}){suffix}"
            try:
                if self._fd is None:
                    fd = os.open(self.path / name, flags, 0o666)
                else:
                    fd = os.open(name, flags, 0o666, dir_fd=self._fd)
            except FileExistsError:
                continue
            except OSError as exc:
                raise InputValidationError(
                    f"Could not create {self.path / name}: {exc.strerror}.",
                    hint="Pass a save_dir the server process may write to.",
                ) from exc
            return DownloadFile(self, name, fd)
        raise InputValidationError(
            f"{self.path} already holds {MAX_NAME_ATTEMPTS} files named like {file_name!r}.",
            hint="Pass a different save_dir, or clear out old downloads.",
        )

    def stat(self, name: str) -> os.stat_result:
        if self._fd is None:
            return os.stat(self.path / name, follow_symlinks=False)
        return os.stat(name, dir_fd=self._fd, follow_symlinks=False)

    def unlink(self, name: str) -> None:
        if self._fd is None:
            os.unlink(self.path / name)
        else:
            os.unlink(name, dir_fd=self._fd)


def open_download_directory(directory: Path, root: Path | None) -> DownloadDirectory:
    """Create (if needed) and open the download directory, confined to ``root`` if set."""
    try:
        if root is None:
            directory.mkdir(parents=True, exist_ok=True)
            if not directory.is_dir():
                raise NotADirectoryError(20, "Not a directory")
            fd = os.open(directory, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC) if _DIR_FD else None
            return DownloadDirectory(directory, fd)

        resolved_root = root.resolve()
        resolved = directory.resolve()
        if not resolved.is_relative_to(resolved_root):
            raise _outside_root(directory, resolved_root, "OPENPROJECT_MCP_DOWNLOAD_ROOT")
        if not _DIR_FD:
            resolved.mkdir(parents=True, exist_ok=True)
            if not resolved.is_dir():
                raise NotADirectoryError(20, "Not a directory")
            return DownloadDirectory(resolved, None)
        fd = _walk_beneath(resolved_root, resolved.relative_to(resolved_root), create=True)
        return DownloadDirectory(resolved, fd)
    except NotADirectoryError as exc:
        raise InputValidationError(
            f"The download target {directory} is not a directory.",
            hint="Pass a save_dir that is a directory, not a file.",
        ) from exc
    except OSError as exc:
        raise InputValidationError(
            f"Could not create the download directory {directory}: {exc.strerror}.",
            hint="Pass a save_dir the server process may write to.",
        ) from exc
