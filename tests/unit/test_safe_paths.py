"""Tests for the file-name sanitisation helpers (P0-6 path traversal)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.bot.safe_paths import (
    UnsafeFileNameError,
    safe_join,
    sanitize_file_name,
)


class TestSanitizeFileName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("../../etc/passwd", "passwd"),
            ("..\\..\\windows\\system32\\evil.exe", "evil.exe"),
            ("/absolute/path/notes.txt", "notes.txt"),
            ("a/b/c.png", "c.png"),
            ("photo.jpg", "photo.jpg"),
            ("..", "passwd"),  # placeholder, real check below
        ],
    )
    def test_base_name_only(self, raw: str, expected: str) -> None:
        if raw == "..":
            with pytest.raises(UnsafeFileNameError):
                sanitize_file_name("..")
            return
        assert sanitize_file_name(raw) == expected

    def test_dot_only_rejected(self) -> None:
        with pytest.raises(UnsafeFileNameError):
            sanitize_file_name(".")

    def test_empty_rejected(self) -> None:
        with pytest.raises(UnsafeFileNameError):
            sanitize_file_name("")

    def test_slash_only_rejected(self) -> None:
        with pytest.raises(UnsafeFileNameError):
            sanitize_file_name("///")

    def test_control_chars_rejected(self) -> None:
        with pytest.raises(UnsafeFileNameError):
            sanitize_file_name("evil\x00name.txt")

    def test_too_long_rejected(self) -> None:
        with pytest.raises(UnsafeFileNameError):
            sanitize_file_name("a" * 300 + ".txt")

    def test_normal_names_pass_through(self) -> None:
        assert sanitize_file_name("مستند.pdf") == "مستند.pdf"


class TestSafeJoin:
    def test_contains_inside_base(self, tmp_path: Path) -> None:
        result = safe_join(tmp_path, "report.pdf")
        assert result == (tmp_path / "report.pdf").resolve()
        assert result.is_relative_to(tmp_path.resolve())

    def test_traversal_cannot_escape(self, tmp_path: Path) -> None:
        # Base name extraction already neutralises this, and the
        # is_relative_to check is the second line of defence.
        result = safe_join(tmp_path, "../../etc/passwd")
        assert result.is_relative_to(tmp_path.resolve())
        assert result.name == "passwd"

    def test_suffix_preserves_extension(self, tmp_path: Path) -> None:
        result = safe_join(tmp_path, "report.pdf", suffix="_abc123")
        assert result.name == "report_abc123.pdf"

    def test_suffix_without_extension(self, tmp_path: Path) -> None:
        result = safe_join(tmp_path, "Makefile", suffix="_abc123")
        assert result.name == "Makefile_abc123"

    def test_rejects_unsafe_raw(self, tmp_path: Path) -> None:
        with pytest.raises(UnsafeFileNameError):
            safe_join(tmp_path, "..")
