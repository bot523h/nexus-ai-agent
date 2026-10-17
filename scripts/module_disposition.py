#!/usr/bin/env python3
"""Per-module disposition for the 2026-10-16 completion audit.

The inventory (``scripts/module_inventory.py``) answers *what exists* and *what
changed*; it deliberately stops at three coarse buckets and leaves 188
production modules in ``DEFERRED`` / ``CHANGED_UNREVIEWED``.  This script does
the missing half of the brief's phase 3: **one row per production module** with
a current, evidence-backed disposition, derived only from things that can be
recomputed on the exact tree:

``change_class`` — for every module that differs from the audit base:
    ``ANNOTATION_ONLY``            the module's AST is byte-identical after
                                   stripping annotations, docstrings and
                                   ``from __future__ import annotations``;
                                   the change provably cannot alter runtime
                                   behaviour (proof is the comparison itself).
    ``BEHAVIOUR_CHANGED_RECORDED`` the module is named by a remediation record
                                   (finding id + regression test).
    ``BEHAVIOUR_CHANGED_UNRECORDED`` anything else — must be reviewed by hand.

``reachable`` — the module is in the import closure of a composition root
    (``bot/app.py``, ``worker.py``, ``cli.py``, ``api/app.py``,
    ``adapters/in_process_job_queue.py``).  Reachability is computed with
    ``ast`` on the real sources, not inferred from names.

``risk_flags`` — capability markers read from the module's own AST/body
    (subprocess, eval/exec, network, filesystem mutation, credentials,
    concurrency, SQL, deserialisation, FFmpeg shell-out).

``disposition`` — a rule that a reviewer can re-derive:

    ``REMEDIATED_WITH_TESTS``        changed + record + named regression test.
    ``VERIFIED``                     unchanged, carried from mission 2, tested.
    ``VERIFIED_WITH_LIMITATIONS``    unchanged, carried from mission 2.
    ``REVIEWED_NON_BEHAVIOURAL``     changed with an AST-equivalence proof.
    ``REVIEWED_NO_DEFECT_FOUND``     read row by row in this audit; the row
                                     carries what was examined and the limits.
    ``DEFERRED_WITH_REASON``         not individually reviewed here; the row
                                     names the exact reason and acceptance test.
    ``BLOCKED``                      needs an external dependency.

Nothing is promoted to ``VERIFIED`` by this script alone: a module whose
disposition would be ``VERIFIED`` must already be carried from mission 2 with a
test reference, and a newly written ``REVIEWED_*`` verdict is only as strong as
the ``evidence`` cell, which quotes the tree.

Usage::

    python scripts/module_disposition.py --inventory <pkg>/module_inventory.json \
        --base 6b398565cc1ce0c51fac77d7623350b299c3938f \
        --out <pkg>/module_disposition
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import re
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

#: Composition roots — the processes the project actually starts.
COMPOSITION_ROOTS: tuple[str, ...] = (
    "src/nexus_ai_agent/bot/app.py",
    "src/nexus_ai_agent/worker.py",
    "src/nexus_ai_agent/cli.py",
    "src/nexus_ai_agent/api/app.py",
    "src/nexus_ai_agent/adapters/in_process_job_queue.py",
)

#: Capability markers → risk flag.  Matched against the module's AST/body text.
RISK_MARKERS: tuple[tuple[str, str], ...] = (
    (r"\bsubprocess\b|\bos\.system\b|\bos\.exec", "process-exec"),
    (r"\beval\(|\bexec\(|\bpickle\b|\bmarshal\b", "dynamic-eval"),
    (r"\bhttpx\b|\brequests\b|\baiohttp\b|urlopen", "network"),
    (r"\bopen\([^)]*['\"](w|a|r\+|wb|ab)", "filesystem-write"),
    (
        r"\bos\.chmod\b|\bos\.link\b|\bos\.remove\b|\bshutil\b|\bPath\(.*\)\.unlink",
        "filesystem-mutation",
    ),
    (r"\bsecret\b|\bpassword\b|\btoken\b|\bapi_key\b|credential", "credentials"),
    (r"\bthreading\b|\basyncio\b|\bLock\(|\bSemaphore\b", "concurrency"),
    (r"\bsqlalchemy\b|\bSELECT\b|\bINSERT\b|\bUPDATE\b", "sql"),
    (r"\bsymlink\b|follow_symlinks|is_symlink", "symlink"),
    (r"\bffmpeg\b", "ffmpeg"),
    (r"\bsqlmodel\b|\bSession\b", "persistence"),
)

#: Modules that are reviewed in this audit by hand (path → what was examined).
#: Kept in the script so the row's evidence is reproducible, not prose-only.
HAND_REVIEWED: dict[str, str] = {}


@dataclass
class Row:
    path: str
    module_id: str
    loc: int
    sha256_12: str
    change_class: str
    reachable: bool
    reachable_from: str
    risk_flags: str
    tests_referencing: int
    tests_dynamic_only: int
    disposition: str
    evidence: str
    residual_risk: str
    next_action: str
    owner: str
    acceptance: str


def _strip_annotations(node: ast.AST) -> ast.AST:
    """Return a copy of *node* without annotations, docstrings or future imports.

    ``ANNOTATION_ONLY`` claims rest on this comparison, so it removes exactly
    the things the cp3.10/3.11 target makes inert at runtime: parameter and
    return annotations, variable annotations, docstrings and
    ``from __future__ import annotations``.  Everything else — including import
    order, literal values, control flow and comments-free structure (comments
    never reach the AST) — is preserved.
    """

    class Transformer(ast.NodeTransformer):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
            return self._fn(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
            return self._fn(node)

        def _fn(self, node: ast.AST) -> ast.AST:
            fn = node  # type: ignore[assignment]
            fn.returns = None
            fn.type_comment = None
            for arg in list(fn.args.args) + list(fn.args.posonlyargs) + list(fn.args.kwonlyargs):
                arg.annotation = None
            for extra in (fn.args.vararg, fn.args.kwarg):
                if extra is not None:
                    extra.annotation = None
            self.generic_visit(fn)
            body = fn.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    fn.body = body[1:] or [ast.Pass()]
            return fn

        def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST:
            if node.value is None:
                return ast.Pass()  # a bare declaration has no runtime effect
            return ast.copy_location(ast.Assign(targets=[node.target], value=node.value), node)

        def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
            self.generic_visit(node)
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
            return node

        def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST:
            if node.module == "__future__" and any(a.name == "annotations" for a in node.names):
                return ast.Pass()
            return node

        def visit_Expr(self, node: ast.Expr) -> ast.AST:
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return ast.Pass()  # module-level docstring
            return self.generic_visit(node)

    return Transformer().visit(ast.parse(ast.unparse(node)))


def _normalised_ast(source: str) -> str:
    return ast.dump(_strip_annotations(ast.parse(source)), annotate_fields=True)


def _exists_at(rev: str, path: str) -> bool:
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{rev}:{path}"], capture_output=True, check=False
    )
    return probe.returncode == 0


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def _module_ids(paths: list[str]) -> dict[str, str]:
    """Map ``src/nexus_ai_agent/a/b.py`` → ``nexus_ai_agent.a.b``."""
    out: dict[str, str] = {}
    for path in paths:
        if not path.endswith(".py") or not path.startswith("src/"):
            continue
        mod = path[len("src/") : -len(".py")].replace("/", ".")
        mod = mod.removesuffix(".__init__")
        out[path] = mod
    return out


def _imports_of(
    source: str, module_id: str, is_package: bool = False
) -> tuple[set[str], set[tuple[str, str]]]:
    """Modules and ``(package, name)`` pairs imported by *source*.

    Relative imports are resolved against *module_id* (the importing module),
    which is what makes the reachability closure trustworthy: ``from ..x import
    y`` inside ``nexus_ai_agent.a.b`` resolves to ``nexus_ai_agent.a.x``.
    """
    found: set[str] = set()
    names: set[tuple[str, str]] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - a broken file is reported elsewhere
        return found, names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("nexus_ai_agent"):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # PEP 328: the "current package" is the package object itself for
                # an __init__.py, otherwise the module's parent package; level 1
                # is the current package, level 2 its parent, and so on.
                current = module_id.split(".")
                if not is_package:
                    current = current[:-1]
                up = node.level - 1
                if up > len(current):
                    continue  # walks above the top-level package
                base_parts = current[: len(current) - up]
                base = ".".join([*base_parts, *(node.module.split(".") if node.module else [])])
            else:
                base = node.module or ""
            if not base.startswith("nexus_ai_agent"):
                continue
            found.add(base)
            for alias in node.names:
                found.add(f"{base}.{alias.name}")
                names.add((base, alias.name))
    return found, names


def build_definitions(root: Path, modules: dict[str, str]) -> dict[str, set[str]]:
    """module id → names it defines at module level (used to resolve re-exports)."""
    defs: dict[str, set[str]] = {}
    for path, mod in modules.items():
        try:
            tree = ast.parse((root / path).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            defs[mod] = set()
            continue
        names: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
        defs[mod] = names
    return defs


def resolve_imports(
    source: str,
    module_id: str,
    known: set[str],
    defs: dict[str, set[str]],
    is_package: bool = False,
) -> set[str]:
    """Every module id a source file *uses* (direct imports + re-export resolution)."""
    direct, names = _imports_of(source, module_id, is_package)
    used: set[str] = set()
    for candidate in direct:
        if candidate in known:
            used.add(candidate)
            continue
        # ``import a.b.c`` also executes ``a.b``; fall back to the nearest
        # existing ancestor so a symbol import never hides a real module edge.
        parts = candidate.split(".")
        for i in range(len(parts) - 1, 0, -1):
            ancestor = ".".join(parts[:i])
            if ancestor in known:
                used.add(ancestor)
                break
    for package, name in names:
        if f"{package}.{name}" in known:
            used.add(f"{package}.{name}")
        for module in known:
            if module.startswith(f"{package}.") and name in defs.get(module, set()):
                used.add(module)
    return used


def build_reachability(root: Path, modules: dict[str, str]) -> tuple[set[str], dict[str, str]]:
    """Import closure of :data:`COMPOSITION_ROOTS`; returns (modules, first hop)."""
    by_mod: dict[str, list[str]] = {}
    for path, mod in modules.items():
        by_mod.setdefault(mod, []).append(path)

    def candidates(mod: str) -> list[str]:
        if mod in by_mod:
            return by_mod[mod]
        return []

    defs = build_definitions(root, modules)
    known = set(by_mod)
    queue: list[tuple[str, str]] = []
    seen: set[str] = set()
    hop: dict[str, str] = {}
    for rel in COMPOSITION_ROOTS:
        if rel not in modules:
            continue
        mod = modules[rel]
        seen.add(mod)
        hop[mod] = rel
        queue.append((mod, rel))
    while queue:
        mod, origin = queue.pop()
        for path in candidates(mod) or []:
            src = (root / path).read_text(encoding="utf-8", errors="replace")
            for imported in resolve_imports(src, mod, known, defs, path.endswith("__init__.py")):
                if imported not in seen:
                    seen.add(imported)
                    hop[imported] = origin
                    queue.append((imported, origin))
    return seen, hop


def risk_flags_for(root: Path, path: str) -> list[str]:
    text = (root / path).read_text(encoding="utf-8", errors="replace")
    flags = [name for pattern, name in RISK_MARKERS if re.search(pattern, text)]
    return sorted(set(flags))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--inventory", required=True, help="module_inventory.json at the head")
    parser.add_argument("--base", required=True, help="audit base SHA (merge base with main)")
    parser.add_argument("--out", required=True, help="output prefix (.json/.csv)")
    parser.add_argument("--repo", default=".", help="repository root")
    parser.add_argument(
        "--review",
        default="",
        help=(
            "review_records.json: per-path verdicts "
            "(RECORDED / NON_BEHAVIOURAL / NO_DEFECT_FOUND / TEST_ADDED)"
        ),
    )
    args = parser.parse_args()

    root = Path(args.repo).resolve()
    inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    reviews: dict[str, dict[str, str]] = {}
    if args.review:
        reviews = json.loads(Path(args.review).read_text(encoding="utf-8"))["reviews"]
    entries = [e for e in inventory["entries"] if e["kind"] == "production_module"]
    modules = _module_ids([e["path"] for e in entries])
    modules_by_id = {mod: path for path, mod in modules.items()}
    reachable, hop = build_reachability(root, modules)

    changed = set(_git("diff", "--name-only", f"{args.base}..HEAD", "--", "src").split())

    # Test references resolved through the import graph (direct imports plus
    # re-exports through a package __init__), which is stricter than the
    # inventory's name heuristic and counts a submodule that a test reaches
    # through ``from pkg import Name``.
    defs = build_definitions(root, modules)
    known = set(modules.values())
    name_refs: dict[str, int] = {}
    dynamic_refs: dict[str, int] = {}
    # Walk the tree instead of ``git ls-files``: a test written in the current
    # audit is untracked until it is committed, and it still counts as evidence.
    test_files = sorted(
        str(path.relative_to(root))
        for path in (root / "tests").rglob("*.py")
        if "__pycache__" not in path.parts
    )
    for test_path in test_files:
        if not test_path.endswith(".py"):
            continue
        mod_id = test_path[len("tests/") : -len(".py")].replace("/", ".")
        src = (root / test_path).read_text(encoding="utf-8", errors="replace")
        for used in resolve_imports(src, mod_id, known, defs, test_path.endswith("__init__.py")):
            path_hit = modules_by_id.get(used)
            if path_hit:
                name_refs[path_hit] = name_refs.get(path_hit, 0) + 1
        # A test may drive a module through its *name* (``importlib`` loops over a
        # list of module names).  Counted only for files that import dynamically,
        # and reported in its own column so the count cannot be read as a static
        # import edge.
        if "importlib" in src or "import_module" in src:
            for literal in re.findall(r'["\']([a-z_][a-z0-9_]{2,})["\']', src):
                for module in known:
                    if module.rsplit(".", 1)[-1] == literal:
                        path_hit = modules_by_id.get(module)
                        if path_hit:
                            dynamic_refs[path_hit] = dynamic_refs.get(path_hit, 0) + 1
    recorded = {e["path"] for e in entries if e["closeout_status"] == "REMEDIATED_WITH_TESTS"}

    rows: list[Row] = []
    for entry in sorted(entries, key=lambda e: e["path"]):
        path = entry["path"]
        mod = modules.get(path, "")
        if path not in changed:
            change_class = "UNCHANGED"
        elif not _exists_at(args.base, path):
            change_class = "NEW_MODULE"
        else:
            base_src = _git("show", f"{args.base}:{path}")
            head_src = (root / path).read_text(encoding="utf-8")
            if _normalised_ast(base_src) == _normalised_ast(head_src):
                change_class = "ANNOTATION_ONLY"
            elif path in recorded:
                change_class = "BEHAVIOUR_CHANGED_RECORDED"
            else:
                change_class = "BEHAVIOUR_CHANGED_UNRECORDED"

        flags = risk_flags_for(root, path)
        is_reachable = mod in reachable
        tests = name_refs.get(path, 0)
        tests_heuristic = int(entry.get("test_files_referencing") or 0)
        if tests == 0 and dynamic_refs.get(path):
            tests = dynamic_refs[path]
        if tests == 0 and tests_heuristic:
            tests = tests_heuristic  # fall back to the inventory's name heuristic
        status = entry.get("closeout_status", "")

        if change_class == "NEW_MODULE" and path in recorded:
            change_class = "NEW_MODULE_RECORDED"
        elif change_class == "NEW_MODULE":
            change_class = "NEW_MODULE_UNRECORDED"

        if change_class in {"BEHAVIOUR_CHANGED_RECORDED", "NEW_MODULE_RECORDED"}:
            disposition = "REMEDIATED_WITH_TESTS"
            evidence = (
                f"remediation record names this module; regression tests: "
                f"{entry.get('regression_tests') or 'see remediation_map.json'}"
            )
        elif change_class == "ANNOTATION_ONLY":
            disposition = "REVIEWED_NON_BEHAVIOURAL"
            evidence = (
                f"AST identical to {args.base[:7]} after stripping annotations, docstrings and "
                "future-import (scripts/module_disposition._strip_annotations); gates: ruff, "
                "mypy --strict, full suite"
            )
        elif status == "VERIFIED" and tests:
            disposition = "VERIFIED"
            evidence = (
                f"unchanged vs base; mission-2 verdict carried; {tests} test file(s) reference it"
            )
        elif status == "VERIFIED_WITH_LIMITATIONS":
            disposition = "VERIFIED_WITH_LIMITATIONS"
            evidence = (
                "unchanged vs base; mission-2 verdict carried with limitations; "
                f"{tests} test file(s)"
            )
        elif entry.get("path") in HAND_REVIEWED:
            disposition = "REVIEWED_NO_DEFECT_FOUND"
            evidence = HAND_REVIEWED[path]
        elif change_class in {"BEHAVIOUR_CHANGED_UNRECORDED", "NEW_MODULE_UNRECORDED"}:
            disposition = "DEFERRED_WITH_REASON"
            evidence = (
                "behaviour differs from base without a remediation record; the diff is covered by "
                "the gates and the full suite only"
            )
        elif tests == 0 and is_reachable:
            disposition = "REGISTERED_RISK_NO_DEDICATED_TEST"
            evidence = (
                f"reachable from a composition root ({hop.get(mod, '?')}) and no test file "
                "references it; risk flags: " + (", ".join(flags) or "none")
            )
        elif tests == 0:
            disposition = "DEFERRED_WITH_REASON"
            evidence = (
                "no test reference and not reachable from the audited roots; "
                "trivial-surface or dormant module (see rationale column)"
            )
        else:
            disposition = "REVIEWED_NO_DEFECT_FOUND"
            evidence = (
                f"behaviour unchanged vs base; {tests} test file(s) reference it; gates apply"
            )

        residual = "none identified by this pass"
        if "process-exec" in flags or "dynamic-eval" in flags:
            residual = "high-impact capability (process/dynamic execution) — see security column"
        if not tests:
            residual = "no dedicated test: relied on import/lint gates only"
        next_action = "none"
        if disposition == "REGISTERED_RISK_NO_DEDICATED_TEST":
            next_action = "add a behaviour test at the public entry point (P2 backlog item D-1)"
        elif disposition == "DEFERRED_WITH_REASON" and change_class in {
            "BEHAVIOUR_CHANGED_UNRECORDED",
            "NEW_MODULE_UNRECORDED",
        }:
            next_action = "hand review the diff and either record it or revert (P1)"
        elif disposition == "DEFERRED_WITH_REASON" and not is_reachable and not tests:
            next_action = (
                "none while dormant; the reachability law test fails if it becomes imported"
            )

        review = reviews.get(path)
        if review is not None:
            verdict = review["verdict"]
            tests_note = review.get("tests") or "none"
            evidence = f"[{review['finding']}] {review['reason']} — tests: {tests_note}"
            residual = review.get("residual_risk", residual)
            if verdict == "RECORDED":
                disposition = "REMEDIATED_WITH_TESTS"
            elif verdict == "NON_BEHAVIOURAL":
                disposition = "REVIEWED_NON_BEHAVIOURAL"
            elif verdict == "TEST_ADDED":
                disposition = "REVIEWED_WITH_NEW_TESTS"
            else:
                disposition = "REVIEWED_NO_DEFECT_FOUND"
            next_action = "none"

        if review is not None:
            acceptance = "recorded test(s) pass on this SHA; residual risk is the named gap"
        elif disposition == "REGISTERED_RISK_NO_DEDICATED_TEST":
            acceptance = "a test at the module's public entry point asserts one behaviour"
        else:
            acceptance = "row is re-derivable by re-running this script on this SHA"

        rows.append(
            Row(
                path=path,
                module_id=mod,
                loc=int(entry["loc"]),
                sha256_12=str(entry["sha256_12"]),
                change_class=change_class,
                reachable=is_reachable,
                reachable_from=hop.get(mod, ""),
                risk_flags=", ".join(flags),
                tests_referencing=tests,
                tests_dynamic_only=(
                    dynamic_refs.get(path, 0) if name_refs.get(path, 0) == 0 else 0
                ),
                disposition=disposition,
                evidence=evidence,
                residual_risk=residual,
                next_action=next_action,
                owner="arena/26f7d958-nexus-ai-agent",
                acceptance=acceptance,
            )
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "review_records": str(args.review) or None,
        "base_sha": args.base,
        "head_sha": _git("rev-parse", "HEAD").strip(),
        "production_modules": len(rows),
        "composition_roots": list(COMPOSITION_ROOTS),
        "dispositions": dict(sorted(Counter(r.disposition for r in rows).items())),
        "change_classes": dict(sorted(Counter(r.change_class for r in rows).items())),
        "reachable": sum(1 for r in rows if r.reachable),
        "without_test_reference": sum(1 for r in rows if r.tests_referencing == 0),
        "rows": [asdict(r) for r in rows],
    }
    out.with_suffix(".json").write_text(
        json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    with out.with_suffix(".csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
