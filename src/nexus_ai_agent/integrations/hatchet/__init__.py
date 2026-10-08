"""Optional Hatchet workflow-execution backend for the Execution Fabric.

Importing this package never imports ``hatchet_sdk`` (see ``_sdk``); the extra
is resolved lazily. Product entrypoints must not select this backend without an
explicit operator opt-in, and embedded mode is dev/CI-only.
"""

from __future__ import annotations

from .adapter import HatchetExecutionAdapter, HatchetTriggerError

__all__ = ["HatchetExecutionAdapter", "HatchetTriggerError"]
