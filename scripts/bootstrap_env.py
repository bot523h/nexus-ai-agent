"""Deterministic dev-environment bootstrap — single source of truth.

``scripts/bootstrap_dev.sh`` is a thin wrapper that delegates to this module,
so the environment contract (venv location, pinned packages, verification)
lives in exactly one place, in code that is unit-testable.

Contract enforced here:

* **Pins** are read from ``pyproject.toml`` at runtime — never duplicated,
  never drifted.  Every ``pkg==version`` dependency is verified on ``--verify``.
* **Venv location** is ``$NEXUS_DEV_VENV`` or ``<repo>/.venv`` (inside the repo
  so it survives between Arena turns; ``.venv/`` is git-ignored).
* **Build order** installs the pinned numpy before the project extras so
  heavy extras (``llama-cpp-python``, ``sentence-transformers``) resolve
  against a known ABI rather than dragging a mismatched numpy/torch pair.

Usage (from the repo root)::

    python scripts/bootstrap_env.py            # create/reuse the venv, verify
    python scripts/bootstrap_env.py --verify   # only assert pins, exit 0/1
"""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]

# Installed ahead of the project extras for ABI stability (see module docstring).
NUMPY_PIN = "1.26.4"


@dataclass(frozen=True)
class Pin:
    """A ``pkg==version`` pin extracted from pyproject dependencies."""

    name: str
    version: str

    def check(self) -> str | None:
        """Return a problem description, or ``None`` when the pin holds."""
        try:
            installed = importlib.metadata.version(self.name)
        except importlib.metadata.PackageNotFoundError:
            return f"{self.name}: not installed (expected {self.version})"
        if installed != self.version:
            return f"{self.name}: expected {self.version}, got {installed}"
        return None


def read_pins() -> list[Pin]:
    """Parse exact ``==`` pins out of ``[project].dependencies``.

    Extras markers (``psycopg[binary,pool]==3.3.5``) are stripped so the
    distribution name matches what ``importlib.metadata`` reports.
    """
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pins: list[Pin] = []
    for dep in data["project"]["dependencies"]:
        if "==" not in dep:
            continue
        raw_name, _, version = dep.partition("==")
        name = raw_name.split("[", 1)[0].strip()
        pins.append(Pin(name=name, version=version.strip()))
    return pins


def resolve_venv_dir() -> Path:
    """Return the venv path: ``$NEXUS_DEV_VENV`` or ``<repo>/.venv``."""
    override = os.environ.get("NEXUS_DEV_VENV")
    if override:
        return Path(override)
    return REPO_ROOT / ".venv"


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "python"


def _verify_in_process() -> list[str]:
    """Check every pin against the interpreter running this module."""
    return [problem for pin in read_pins() if (problem := pin.check()) is not None]


def verify(venv_dir: Path | None = None) -> int:
    """Run the pin checks with the venv interpreter and print the result.

    Returns 0 when every pin holds, 1 otherwise (including a missing venv).
    """
    venv_dir = venv_dir or resolve_venv_dir()
    py = _venv_python(venv_dir)
    if not py.exists():
        sys.stderr.write(f"[bootstrap] venv missing at {venv_dir}; run without --verify\n")
        return 1
    result = subprocess.run(
        [str(py), str(__file__), "--verify-in-venv"],
        capture_output=True,
        text=True,
        check=False,
    )
    sys.stdout.write(result.stdout)
    if result.stderr.strip():
        sys.stderr.write(result.stderr)
    return result.returncode


def ensure() -> int:
    """Create (or reuse) the venv and guarantee the pins hold."""
    venv_dir = resolve_venv_dir()
    py = _venv_python(venv_dir)

    if py.exists():
        if verify(venv_dir) == 0:
            return 0
        sys.stderr.write(f"[bootstrap] venv at {venv_dir} failed verification; rebuilding\n")

    sys.stderr.write(f"[bootstrap] creating venv at {venv_dir}\n")
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)

    sys.stderr.write("[bootstrap] upgrading pip\n")
    subprocess.run([str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"], check=True)

    sys.stderr.write(f"[bootstrap] installing numpy=={NUMPY_PIN} first (ABI stability)\n")
    subprocess.run([str(py), "-m", "pip", "install", "--quiet", f"numpy=={NUMPY_PIN}"], check=True)

    sys.stderr.write("[bootstrap] installing project + dev extras (slow step)\n")
    subprocess.run(
        [str(py), "-m", "pip", "install", "--quiet", "-e", f"{REPO_ROOT}[dev]"],
        cwd=REPO_ROOT,
        check=True,
    )

    return verify(venv_dir)


def main(argv: list[str]) -> int:
    if "--verify" in argv:
        return verify()
    if "--verify-in-venv" in argv:
        problems = _verify_in_process()
        if problems:
            sys.stderr.write("toolchain verification failed:\n")
            for problem in problems:
                sys.stderr.write(f"  - {problem}\n")
            return 1
        versions = ", ".join(f"{pin.name}={pin.version}" for pin in read_pins())
        print(f"toolchain OK: {versions}")
        return 0
    return ensure()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
