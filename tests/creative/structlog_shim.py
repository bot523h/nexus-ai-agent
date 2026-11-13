"""Guarded stdlib ``structlog`` stand-in, installed only when the real package is absent.

Load with ``pytest -p tests.creative.structlog_shim`` in sandboxes that cannot install
``structlog``.  When the real package is importable this module changes nothing.
"""

from __future__ import annotations

import logging
import sys
import types
from typing import Any

try:  # pragma: no cover - environment dependent
    import structlog as _real  # noqa: F401
except ModuleNotFoundError:
    class _Logger:
        def __init__(self, name: str) -> None:
            self._log = logging.getLogger(name)

        def bind(self, **_: Any) -> "_Logger":
            return self

        def __getattr__(self, item: str) -> Any:
            return getattr(self._log, item)

    def _noop(*_: Any, **__: Any) -> Any:
        return None

    def _passthrough(*_: Any, **__: Any) -> Any:
        return lambda event_dict: event_dict

    shim = types.ModuleType("structlog")
    shim.get_logger = lambda name=None, **_: _Logger(name or "structlog")  # type: ignore[attr-defined]
    shim.configure = _noop  # type: ignore[attr-defined]
    shim.make_filtering_bound_logger = _passthrough  # type: ignore[attr-defined]
    contextvars = types.ModuleType("structlog.contextvars")
    contextvars.bind_contextvars = _noop  # type: ignore[attr-defined]
    contextvars.clear_contextvars = _noop  # type: ignore[attr-defined]
    stdlib = types.ModuleType("structlog.stdlib")
    stdlib.LoggerFactory = _passthrough  # type: ignore[attr-defined]
    processors = types.ModuleType("structlog.processors")
    for name in ("format_exc_info", "add_log_level", "TimeStamper", "StackInfoRenderer", "JSONRenderer"):
        setattr(processors, name, _passthrough)
    shim.contextvars = contextvars  # type: ignore[attr-defined]
    shim.stdlib = stdlib  # type: ignore[attr-defined]
    shim.processors = processors  # type: ignore[attr-defined]
    sys.modules["structlog"] = shim
    sys.modules["structlog.contextvars"] = contextvars
    sys.modules["structlog.stdlib"] = stdlib
    sys.modules["structlog.processors"] = processors


def _stub_storage_package_if_unimportable() -> None:
    """Expose ``storage`` and ``storage.artifact_vault`` without running their ``__init__``.

    Those ``__init__`` files import provider SDKs (for example ``httpx``) that some sandboxes
    lack.  The artifact-vault modules themselves need only NumPy and the standard library, so
    when the real package cannot be imported, its submodules are loaded by path.
    """
    import importlib
    import pathlib

    try:
        importlib.import_module("nexus_ai_agent.storage")
        return
    except ModuleNotFoundError:
        pass
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "nexus_ai_agent" / "storage"
    import importlib.util

    storage = types.ModuleType("nexus_ai_agent.storage")
    storage.__path__ = [str(root)]  # type: ignore[attr-defined]
    sys.modules["nexus_ai_agent.storage"] = storage
    vault_dir = root / "artifact_vault"
    spec = importlib.util.spec_from_file_location(
        "nexus_ai_agent.storage.artifact_vault",
        vault_dir / "__init__.py",
        submodule_search_locations=[str(vault_dir)],
    )
    assert spec is not None and spec.loader is not None
    vault = importlib.util.module_from_spec(spec)
    sys.modules["nexus_ai_agent.storage.artifact_vault"] = vault
    spec.loader.exec_module(vault)


_stub_storage_package_if_unimportable()
