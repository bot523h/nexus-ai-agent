#!/usr/bin/env python3
"""Repository inventory for the closeout evidence package (brief #3, 2026-10-15).

Walks the repository and writes a machine-readable inventory of everything that
executes or governs execution — production modules, tests, scripts, migrations,
workflows, container files — with size facts, a test-reference heuristic and a
*status* that is derived only from recorded evidence:

* ``prior_status``  — the per-module verdict of the Mission-2 assurance pass on
  ``main`` 6b398565 (``--prior`` CSV; column ``status``), when the module existed;
* ``changed_in_pr`` — the file differs from the merge base (``--base``);
* ``closeout_status`` —
    ``REMEDIATED_WITH_TESTS``   changed in the PR *and* a remediation record names
                               it (``--remediation`` JSON: path → finding ids +
                               regression tests);
    ``CHANGED_UNREVIEWED``      changed in the PR without a dedicated record (the
                               change is covered by the gates and the full suite
                               only — typing/lint-only edits land here);
    ``VERIFIED`` / ``VERIFIED_WITH_LIMITATIONS``
                               unchanged and carried over from Mission 2;
    ``DEFERRED``                everything else (unchanged, never individually
                               reviewed, or a new file without a record).

Nothing here is inferred from prose: statuses come from the two input files and
``git diff``; counts come from the AST.  Outputs are sorted and byte-stable so a
re-run on the same tree is a no-op.

Usage::

    python scripts/module_inventory.py --base origin/main \
        --prior docs/audits/2026-10-15-closeout/mission2_module_status_6b398565.csv \
        --remediation docs/audits/2026-10-15-closeout/remediation_map.json \
        --out docs/audits/2026-10-15-closeout/module_inventory
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "src" / "nexus_ai_agent"
STATUS_ORDER = (
    "REMEDIATED_WITH_TESTS",
    "VERIFIED",
    "VERIFIED_WITH_LIMITATIONS",
    "CHANGED_UNREVIEWED",
    "DEFERRED",
)


@dataclass(frozen=True)
class Entry:
    path: str
    kind: str
    module_id: str
    loc: int
    sha256_12: str
    functions: int
    async_functions: int
    classes: int
    test_files_referencing: int
    changed_in_pr: bool
    prior_status: str
    closeout_status: str
    findings: str
    regression_tests: str


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout


def _python_loc(text: str) -> int:
    """Non-blank, non-comment lines (docstrings count: they are shipped bytes)."""
    return sum(
        1 for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )


def _ast_facts(text: str) -> tuple[int, int, int]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return (0, 0, 0)
    functions = async_functions = classes = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            functions += 1
        elif isinstance(node, ast.AsyncFunctionDef):
            async_functions += 1
        elif isinstance(node, ast.ClassDef):
            classes += 1
    return functions, async_functions, classes


def _module_id(path: Path) -> str:
    if PACKAGE not in path.parents:
        return ""
    rel = path.relative_to(PACKAGE).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return "nexus_ai_agent" + ("." + ".".join(parts) if parts else "")


def _kind(path: Path) -> str | None:
    rel = path.relative_to(REPO)
    parts = rel.parts
    if parts[0] == "src" and path.suffix == ".py":
        return "production_module"
    if parts[0] == "tests" and path.suffix == ".py":
        return "test"
    if parts[0] == "scripts" and path.suffix == ".py":
        return "script"
    if parts[0] == "migrations" and path.suffix == ".py":
        return "migration"
    if parts[:2] == (".github", "workflows") and path.suffix in {".yml", ".yaml"}:
        return "workflow"
    if rel.name == "Dockerfile" or parts[0] == "docker":
        return "container"
    return None


def _walk() -> list[Path]:
    roots = ["src", "tests", "scripts", "migrations", ".github/workflows", "docker", "Dockerfile"]
    found: list[Path] = []
    for root in roots:
        base = REPO / root
        if base.is_file():
            found.append(base)
            continue
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and _kind(path):
                found.append(path)
    return found


def _changed_files(base: str) -> set[str]:
    out = _git("diff", "--name-only", f"{base}...HEAD") if base else ""
    return {line.strip() for line in out.splitlines() if line.strip()}


def _load_prior(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    prior: dict[str, str] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            module = row["module_id"].replace("~.", "nexus_ai_agent.", 1)
            prior[module] = row["status"].strip()
    return prior


def _load_remediation(path: Path | None) -> dict[str, dict[str, list[str]]]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data["records"] if isinstance(data, dict) and "records" in data else data
    return {
        str(k): {"findings": list(v["findings"]), "tests": list(v["tests"])}
        for k, v in records.items()
    }


def _test_reference_index(test_files: list[Path]) -> list[str]:
    return [p.read_text(encoding="utf-8", errors="replace") for p in test_files]


def _references(test_text: str, module_id: str) -> bool:
    head, _, tail = module_id.rpartition(".")
    return module_id in test_text or (bool(head) and f"from {head} import {tail}" in test_text)


def derive_remediation(base: str) -> dict[str, object]:
    """Pair every production module changed in ``base..HEAD`` with the regression tests of the
    same commits.

    Rule (mechanical, no prose involved): a test file counts as regression evidence for a module
    only when it was added/changed in the *same commit* and its current text imports or names that
    module.  Finding ids are read from the commit subject (``A-NN`` / ``GOV-N`` / ``R0`` tags); a
    commit without such a tag contributes its short SHA instead.  Modules whose commits changed no
    test naming them get no record — they surface as ``CHANGED_UNREVIEWED`` in the inventory.
    """
    import re

    tag = re.compile(r"\b(A-\d{2}|GOV-\d|R[0-9])\b")
    records: dict[str, dict[str, set[str]]] = {}
    commits = [c for c in _git("rev-list", "--reverse", f"{base}..HEAD").split() if c]
    for sha in commits:
        subject = _git("log", "-1", "--format=%s", sha).strip()
        findings = sorted(set(tag.findall(subject))) or [sha[:7]]
        files = [f for f in _git("show", "--name-only", "--format=", sha).splitlines() if f]
        tests = [
            f for f in files if f.startswith("tests/") and f.endswith(".py") and (REPO / f).exists()
        ]
        modules = [f for f in files if f.startswith("src/nexus_ai_agent/") and f.endswith(".py")]
        test_texts = {t: (REPO / t).read_text(encoding="utf-8", errors="replace") for t in tests}
        for module in modules:
            path = REPO / module
            if not path.exists():
                continue
            module_id = _module_id(path)
            paired = [t for t, text in test_texts.items() if _references(text, module_id)]
            if not paired:
                continue
            record = records.setdefault(
                module, {"findings": set(), "tests": set(), "commits": set()}
            )
            record["findings"].update(findings)
            record["tests"].update(paired)
            record["commits"].add(sha[:7])
    return {
        "rule": (
            "module <-> test pairing per commit: the test file changed in the same commit and "
            "its text imports or names the module; findings parsed from the commit subject"
        ),
        "base": base,
        "head": _git("rev-parse", "HEAD").strip(),
        "records": {
            k: {key: sorted(v[key]) for key in ("findings", "tests", "commits")}
            for k, v in sorted(records.items())
        },
    }


def _closeout_status(
    *, kind: str, changed: bool, prior: str, remediation: dict[str, list[str]] | None
) -> str:
    if kind != "production_module":
        return "N/A"
    if remediation is not None and changed:
        return "REMEDIATED_WITH_TESTS"
    if changed:
        return "CHANGED_UNREVIEWED"
    if prior in {"VERIFIED", "VERIFIED_WITH_LIMITATIONS"}:
        return prior
    return "DEFERRED"


def build(base: str, prior_csv: Path | None, remediation_json: Path | None) -> list[Entry]:
    changed = _changed_files(base)
    prior = _load_prior(prior_csv)
    remediation = _load_remediation(remediation_json)
    files = _walk()
    test_texts = _test_reference_index([p for p in files if _kind(p) == "test"])
    entries: list[Entry] = []
    for path in files:
        kind = _kind(path)
        assert kind is not None
        rel = str(path.relative_to(REPO))
        text = path.read_text(encoding="utf-8", errors="replace")
        module_id = _module_id(path) if kind == "production_module" else ""
        if path.suffix == ".py":
            loc = _python_loc(text)
            functions, async_functions, classes = _ast_facts(text)
        else:
            loc = sum(1 for line in text.splitlines() if line.strip())
            functions = async_functions = classes = 0
        referencing = sum(1 for t in test_texts if _references(t, module_id)) if module_id else 0
        record = remediation.get(rel)
        entries.append(
            Entry(
                path=rel,
                kind=kind,
                module_id=module_id,
                loc=loc,
                sha256_12=hashlib.sha256(path.read_bytes()).hexdigest()[:12],
                functions=functions,
                async_functions=async_functions,
                classes=classes,
                test_files_referencing=referencing,
                changed_in_pr=rel in changed,
                prior_status=prior.get(module_id, "") if module_id else "",
                closeout_status=_closeout_status(
                    kind=kind,
                    changed=rel in changed,
                    prior=prior.get(module_id, ""),
                    remediation=record,
                ),
                findings=";".join(record["findings"]) if record else "",
                regression_tests=";".join(record["tests"]) if record else "",
            )
        )
    entries.sort(key=lambda e: (e.kind, e.path))
    return entries


def summary(entries: list[Entry], base: str) -> dict[str, object]:
    by_kind = Counter(e.kind for e in entries)
    prod = [e for e in entries if e.kind == "production_module"]
    status = Counter(e.closeout_status for e in prod)
    return {
        "head": _git("rev-parse", "HEAD").strip(),
        "base": base,
        "base_sha": _git("rev-parse", base).strip() if base else "",
        "files": len(entries),
        "by_kind": dict(sorted(by_kind.items())),
        "production_modules": len(prod),
        "production_non_trivial_modules": sum(1 for e in prod if e.loc > 20),
        "production_loc": sum(e.loc for e in prod),
        "production_changed_in_pr": sum(1 for e in prod if e.changed_in_pr),
        "production_status": {k: status.get(k, 0) for k in STATUS_ORDER},
        "tests": by_kind.get("test", 0),
        "test_loc": sum(e.loc for e in entries if e.kind == "test"),
    }


def write(entries: list[Entry], base: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = list(Entry.__dataclass_fields__)
    with out.with_suffix(".csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for entry in entries:
            writer.writerow(asdict(entry))
    payload = {"summary": summary(entries, base), "entries": [asdict(e) for e in entries]}
    out.with_suffix(".json").write_text(
        json.dumps(payload, indent=1, sort_keys=True) + "\n", "utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--base", default="origin/main", help="merge base for changed_in_pr ('' to disable)"
    )
    parser.add_argument("--prior", type=Path, default=None, help="Mission-2 per-module status CSV")
    parser.add_argument("--remediation", type=Path, default=None, help="path → findings/tests JSON")
    parser.add_argument("--out", type=Path, required=True, help="output path without extension")
    parser.add_argument(
        "--derive-remediation",
        type=Path,
        default=None,
        help="write the mechanically derived module→tests map here and use it as --remediation",
    )
    args = parser.parse_args(argv)
    remediation = args.remediation
    if args.derive_remediation is not None:
        derived = derive_remediation(args.base)
        args.derive_remediation.parent.mkdir(parents=True, exist_ok=True)
        args.derive_remediation.write_text(
            json.dumps(derived, indent=1, sort_keys=True) + "\n", "utf-8"
        )
        remediation = args.derive_remediation
    entries = build(args.base, args.prior, remediation)
    write(entries, args.base, args.out)
    facts = summary(entries, args.base)
    print(json.dumps(facts, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
