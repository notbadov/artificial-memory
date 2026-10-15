"""Portable, fail-closed tar extraction for supported Python runtimes."""

from __future__ import annotations

import tarfile
from pathlib import Path, PurePosixPath


def extract_data_archive(archive: tarfile.TarFile, destination: Path | str) -> None:
    """Extract regular files/directories without relying on Python 3.12 filters."""
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    resolved_root = root.resolve()
    members = archive.getmembers()

    for member in members:
        if "\\" in member.name:
            raise tarfile.ExtractError(f"unsafe archive member path: {member.name}")
        relative = PurePosixPath(member.name)
        if relative.is_absolute() or ".." in relative.parts:
            raise tarfile.ExtractError(f"unsafe archive member path: {member.name}")
        if not (member.isfile() or member.isdir()):
            raise tarfile.ExtractError(f"unsupported archive member type: {member.name}")
        target = (resolved_root / Path(*relative.parts)).resolve()
        if target != resolved_root and resolved_root not in target.parents:
            raise tarfile.ExtractError(f"archive member escapes destination: {member.name}")

    archive.extractall(resolved_root, members=members)
