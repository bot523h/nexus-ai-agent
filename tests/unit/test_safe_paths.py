"""Containment tests for user-controlled file names (P0-6).

``/cloud`` and ``/download`` used to build ``directory / doc.file_name`` and
``directory / " ".join(context.args)`` straight from client input.  These tests
are the regression net for that class of bug.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus_ai_agent.core.paths import (
    UnsafeFileName,
    safe_local_path,
    safe_remote_key,
    sanitize_file_name,
)


def test_parent_traversal_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(UnsafeFileName):
        safe_local_path(tmp_path, "../../etc/passwd")
    with pytest.raises(UnsafeFileName):
        safe_local_path(tmp_path, "..")
    with pytest.raises(UnsafeFileName):
        safe_local_path(tmp_path, "   ")


def test_windows_style_traversal_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(UnsafeFileName):
        safe_local_path(tmp_path, "..\\..\\windows\\system32\\evil.dll")


def test_absolute_path_is_reduced_to_a_base_name(tmp_path: Path) -> None:
    assert sanitize_file_name("/etc/passwd") == "passwd"
    assert sanitize_file_name("a/b/c/report.pdf") == "report.pdf"


def test_control_characters_and_nul_are_rejected() -> None:
    with pytest.raises(UnsafeFileName):
        sanitize_file_name("evil\x00.pdf")
    with pytest.raises(UnsafeFileName):
        sanitize_file_name("line\nbreak.pdf")


def test_on_disk_name_is_a_random_token_not_the_user_name(tmp_path: Path) -> None:
    staged = safe_local_path(tmp_path, "invoice.pdf")
    assert staged.display_name == "invoice.pdf"
    assert staged.suffix == ".pdf"
    assert staged.path.parent == tmp_path.resolve()
    assert "invoice" not in staged.path.name
    assert staged.path.suffix == ".pdf"
    # Two uploads of the same name never collide.
    again = safe_local_path(tmp_path, "invoice.pdf")
    assert again.path != staged.path


def test_keep_name_preserves_the_sanitized_stem(tmp_path: Path) -> None:
    staged = safe_local_path(tmp_path, "uploads/2026/invoice.pdf", keep_name=True)
    assert staged.path.name == "invoice.pdf"
    assert staged.path.parent == tmp_path.resolve()


def test_suffix_allowlist_is_enforced(tmp_path: Path) -> None:
    allowed = frozenset({".pdf", ".png"})
    assert safe_local_path(tmp_path, "a.pdf", allowed_suffixes=allowed).suffix == ".pdf"
    with pytest.raises(UnsafeFileName):
        safe_local_path(tmp_path, "a.sh", allowed_suffixes=allowed)


def test_a_symlinked_directory_cannot_redirect_the_write(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform without symlinks
        pytest.skip("symlinks unavailable")
    staged = safe_local_path(link, "file.txt")
    # Writing must land inside the real directory the link points at, never
    # somewhere the caller did not ask for.
    assert staged.path.parent == outside.resolve()


def test_remote_key_drops_path_components() -> None:
    assert safe_remote_key("bucket/sub/evil.txt") == "evil.txt"
    assert safe_remote_key("a\\b\\c.txt") == "c.txt"
    with pytest.raises(UnsafeFileName):
        safe_remote_key("../../bucket/evil.txt")
    with pytest.raises(UnsafeFileName):
        safe_remote_key("..")
