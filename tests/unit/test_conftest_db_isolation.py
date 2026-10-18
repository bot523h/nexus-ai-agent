"""The autouse DB fixture must not leak "this path is initialised" across tests.

``storage.db`` remembers which SQLite paths it already created tables for
(``_initialized_paths``).  Tests that ``chdir`` into a temp dir use the
CWD-relative default ``data/app.sqlite``; if that *string* survives into the
next test, ``create_all_tables`` returns early for a brand-new empty file and
the test dies with ``no such table`` — but only when the two tests happen to
run in that order.  It surfaced when a new test file shifted the collection
order (``test_ai_memory`` → ``test_approval``), and it reproduces on ``main``
with just those two files.

Pytest runs the tests of one module in definition order, so the pair below is
deterministic: the first leaves the marker behind, the second proves the
autouse fixture in ``tests/conftest.py`` cleared it.
"""

from __future__ import annotations

from nexus_ai_agent.storage import db as db_module

_RELATIVE_DEFAULT = "data/app.sqlite"


def test_1_a_test_may_leave_the_default_path_marked_initialised() -> None:
    db_module._initialized_paths.add(_RELATIVE_DEFAULT)
    assert _RELATIVE_DEFAULT in db_module._initialized_paths


def test_2_the_next_test_starts_with_no_path_marked_initialised() -> None:
    assert _RELATIVE_DEFAULT not in db_module._initialized_paths
    assert not db_module._initialized_paths
