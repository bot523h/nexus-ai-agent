#!/usr/bin/env python3
"""Mutation gate for REMOTE-KEY ingress sanitization and cache containment.

Run with ``python scripts/security_mutations_remote_key.py``. A green exit
proves that raw external filenames, encoded traversal, and unsafe key-to-path
joins are caught by the dedicated tests.
"""

from __future__ import annotations

from security_mutation_support import Mutation, SourceEdit, run_mutation_suite

TESTS = (
    "tests/architecture/test_storage_key_boundary.py::test_cloud_upload_uses_the_sanitized_external_filename_as_its_remote_key",
    "tests/unit/test_local_cache_provider.py::test_path_for_key_rejects_traversal_absolute_nul_and_encoded_keys",
    "tests/unit/test_local_cache_provider.py::test_symlinked_key_components_cannot_escape_cache",
)
CACHE = "src/nexus_ai_agent/storage/providers/local_cache.py"
HANDLERS = "src/nexus_ai_agent/bot/handlers.py"

MUTATIONS = (
    Mutation(
        "raw_telegram_filename_used_as_remote_key",
        (
            SourceEdit(
                HANDLERS,
                "                remote_key=safe_name,",
                '                remote_key=doc.file_name or "",',
            ),
        ),
        (TESTS[0],),
        (
            "tests/architecture/test_storage_key_boundary.py::test_cloud_upload_uses_the_sanitized_external_filename_as_its_remote_key",
        ),
        "external names must be sanitized before becoming provider object keys",
    ),
    Mutation(
        "untrusted_key_join_bypasses_validation_and_containment",
        (
            SourceEdit(
                CACHE,
                "    def path_for_key(self, remote_key: str) -> Path:\n"
                '        """Return a contained path for display; upload/download '
                'do I/O by fd."""\n'
                "        components = self._validated_components(remote_key)\n"
                "        return self._path_for_components(components)",
                "    def path_for_key(self, remote_key: str) -> Path:\n"
                '        """Return a contained path for display; upload/download '
                'do I/O by fd."""\n'
                "        return self.cache_dir / remote_key",
            ),
        ),
        (TESTS[1], TESTS[2]),
        (
            "tests/unit/test_local_cache_provider.py::test_path_for_key_rejects_traversal_absolute_nul_and_encoded_keys[]",
            "tests/unit/test_local_cache_provider.py::test_symlinked_key_components_cannot_escape_cache",
        ),
        "remote keys must be validated and contained before filesystem use",
    ),
    Mutation(
        "encoded_traversal_no_longer_checked_after_decoding",
        (
            SourceEdit(
                CACHE,
                "            cls._components(\n"
                "                decoded,\n"
                "                allow_empty=allow_empty,\n"
                "                allow_trailing_slash=allow_trailing_slash,\n"
                "            )",
                "            pass  # mutation: skip decoded-key validation",
            ),
        ),
        (
            "tests/unit/test_local_cache_provider.py::test_path_for_key_rejects_traversal_absolute_nul_and_encoded_keys[%2e%2e%2foutside.txt]",
        ),
        (
            "tests/unit/test_local_cache_provider.py::test_path_for_key_rejects_traversal_absolute_nul_and_encoded_keys[%2e%2e%2foutside.txt]",
        ),
        "percent-decoded traversal spellings must be rejected before reaching cache paths",
    ),
)


if __name__ == "__main__":
    raise SystemExit(run_mutation_suite("REMOTE-KEY", TESTS, MUTATIONS))
