"""Package the canonical Alembic tree without maintaining a second source copy.

Project metadata stays in pyproject.toml. This build-only hook mirrors the
repository-owned migrations into the installed runtime; MANIFEST.in includes
its inputs so an sdist produces the same wheel. No application import here.
"""

from pathlib import Path
from shutil import copy2, rmtree

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent


class BuildPy(build_py):
    def run(self) -> None:
        super().run()
        target = Path(self.build_lib) / "nexus_ai_agent" / "storage" / "_alembic"
        # Repeated builds must not ship a revision deleted from the source tree.
        if target.exists():
            rmtree(target)
        target.mkdir(parents=True)
        source = ROOT / "migrations"
        for path in sorted(source.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".mako"}:
                destination = target / path.relative_to(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                copy2(path, destination)
        copy2(ROOT / "alembic.ini", target / "alembic.ini")


setup(cmdclass={"build_py": BuildPy})
