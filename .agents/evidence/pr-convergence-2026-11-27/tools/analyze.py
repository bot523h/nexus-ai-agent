#!/usr/bin/env python3
"""Turn PR_FORENSICS.json into contract-level evidence for dispositions.

For every open PR it measures, against the live main tree:

* how much of its changed-file set already exists on main (identical blobs),
* **qualified contract coverage** — whether the exact
  ``(module_path, Class.method(arg_names), ...)`` contract a PR adds already
  exists on main (an AST measurement, not a name-grep),
* the legacy **bare symbol coverage** (name-only), kept for continuity,
* whether an equivalent commit subject already landed on main,
* which other open PRs touch the same files (overlap graph input).

The difference matters: a name-grep says "``execute`` exists on main" even when
the only ``execute`` is ``SomeUnrelatedClass.execute(self)`` in another module.
``SUPERSEDED_AND_CLOSED`` is a claim about *contract parity*, so it is measured
with :func:`verify_superseded_contract_parity`.

Nothing here decides a disposition; it only produces the numbers a reviewer can
cite. Usage::

    python analyze.py                      # rewrite PR_ANALYSIS.json
    python analyze.py --verify-superseded  # also write the parity table
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import tarfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FORENSICS = ROOT / "PR_FORENSICS.json"
OUT = ROOT / "PR_ANALYSIS.json"
PARITY_OUT = ROOT / "SUPERSEDED_CONTRACT_PARITY.json"
MAIN = "main"

#: ``(relative module path, qualified name, argument names)``.
SymbolKey = tuple[str, str, tuple[str, ...]]

CONST_RE = re.compile(r"^\+\s*([A-Z][A-Z0-9_]{3,})\s*[:=]")
SKIP = {"main", "run", "setUp", "tearDown", "test", "tests"}
SUPERSEDED_NUMBERS = (135, 140, 205, 295, 301)


# --------------------------------------------------------------------------- #
# Pure AST extraction (no git, no IO) — the unit-tested core.
# --------------------------------------------------------------------------- #
def _argument_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    """Positional, keyword-only and variadic parameter names, in source order."""
    args = node.args
    names = [arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    if args.vararg is not None:
        names.append(f"*{args.vararg.arg}")
    if args.kwarg is not None:
        names.append(f"**{args.kwarg.arg}")
    return tuple(names)


def extract_symbols(source: str, rel_path: str) -> set[SymbolKey]:
    """Every function/method/class defined in *source*, keyed by its contract.

    ``rel_path`` is the repository-relative path used as the module component,
    so ``("src/pkg/mod.py", "Cls.method", ("self", "x"))`` cannot be satisfied
    by a same-named symbol in a different module.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    found: set[SymbolKey] = set()

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                found.add((rel_path, f"{prefix}{child.name}", ()))
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.add((rel_path, f"{prefix}{child.name}", _argument_names(child)))
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return {key for key in found if not key[1].split(".")[-1].startswith("__")}


def symbol_delta(
    base_sources: Mapping[str, str], head_sources: Mapping[str, str]
) -> set[SymbolKey]:
    """Contracts present at the PR head and absent from its merge base."""
    added: set[SymbolKey] = set()
    for path, head_source in head_sources.items():
        if not path.endswith(".py"):
            continue
        head_keys = extract_symbols(head_source, path)
        base_keys = extract_symbols(base_sources[path], path) if path in base_sources else set()
        added |= head_keys - base_keys
    return added


def build_index(sources: Mapping[str, str]) -> set[SymbolKey]:
    """Contract index of a whole tree (main), keyed exactly like :func:`symbol_delta`."""
    index: set[SymbolKey] = set()
    for path, source in sources.items():
        if path.endswith(".py"):
            index |= extract_symbols(source, path)
    return index


def bare_name_index(index: Iterable[SymbolKey]) -> set[str]:
    """Name-only projection, i.e. what the old grep-based metric measured."""
    return {name.split(".")[-1] for _path, name, _args in index}


def coverage_report(added: Iterable[SymbolKey], main_index: Iterable[SymbolKey]) -> dict[str, Any]:
    """Bare (name) and qualified (module + name + signature) coverage in one pass."""
    added_set = set(added)
    index_set = set(main_index)
    names = bare_name_index(index_set)
    exact = {key for key in added_set if key in index_set}
    bare_hits = {key for key in added_set if key[1].split(".")[-1] in names}
    return {
        "added": len(added_set),
        "bare_hits": len(bare_hits),
        "bare_coverage": (len(bare_hits) / len(added_set)) if added_set else None,
        "qualified_hits": len(exact),
        "qualified_coverage": (len(exact) / len(added_set)) if added_set else None,
        "missing_contracts": sorted(added_set - exact),
    }


def _coerce_keys(raw: Iterable[Any]) -> set[SymbolKey]:
    """Accept JSON round-tripped ``[module, name, [args]]`` triples."""
    keys: set[SymbolKey] = set()
    for entry in raw:
        if isinstance(entry, (list, tuple)) and len(entry) == 3:
            module, name, args = entry
            keys.add((str(module), str(name), tuple(str(arg) for arg in (args or ()))))
    return keys


def verify_superseded_contract_parity(
    pr_record: Mapping[str, Any], main_index: Iterable[SymbolKey]
) -> dict[str, Any]:
    """Prove a ``SUPERSEDED_AND_CLOSED`` verdict at the contract level.

    ``parity`` is true only when *every* contract the PR adds already exists on
    main with the same module path, qualified name **and** argument names. A
    same-name symbol in another module is reported as ``relocated`` (it does
    *not* establish parity), and anything else is ``missing``.
    """
    added = _coerce_keys(
        pr_record.get("added_contracts") or pr_record.get("added_symbol_keys") or []
    )
    index = set(main_index)
    by_name_and_args = {(name, args) for _path, name, args in index}
    exact = sorted(key for key in added if key in index)
    missing = sorted(key for key in added if key not in index)
    relocated = [key for key in missing if (key[1], key[2]) in by_name_and_args]
    return {
        "number": pr_record.get("number"),
        "added_contracts": len(added),
        "exact_matches": len(exact),
        "relocated": [[key[0], key[1], list(key[2])] for key in relocated],
        "missing": [[key[0], key[1], list(key[2])] for key in missing if key not in relocated],
        "parity": bool(added) and not missing,
        "coverage": (len(exact) / len(added)) if added else None,
    }


# --------------------------------------------------------------------------- #
# Git-backed helpers (thin, so the pure core stays testable).
# --------------------------------------------------------------------------- #
def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False).stdout


def _show(ref: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True, check=False
    )
    return result.stdout if result.returncode == 0 else None


def _ref_for(pr: Mapping[str, Any]) -> str:
    candidate = f"refs/remotes/pr/{pr['number']}"
    exists = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", candidate],
        capture_output=True,
        text=True,
        check=False,
    )
    return candidate if exists.returncode == 0 else str(pr["head_sha"])


def pr_contracts(pr: Mapping[str, Any]) -> set[SymbolKey]:
    """Contracts the PR adds, computed per changed ``.py`` file (base vs head)."""
    ref = _ref_for(pr)
    added: set[SymbolKey] = set()
    for entry in pr["files"]:
        path = entry["path"]
        if not path.endswith(".py"):
            continue
        head_source = _show(ref, path)
        base_source = _show(pr["merge_base"], path)
        if head_source is None:
            continue
        head_keys = extract_symbols(head_source, path)
        base_keys = extract_symbols(base_source, path) if base_source is not None else set()
        added |= head_keys - base_keys
    return added


def main_symbols() -> set[str]:
    text = git("grep", "-h", "-o", "-E", r"(def|class) [A-Za-z_][A-Za-z0-9_]*", MAIN, "--", "*.py")
    return {parts[1] for parts in (line.split() for line in text.splitlines()) if len(parts) == 2}


def main_subjects() -> list[str]:
    return [
        line.split(" ", 1)[1]
        for line in git("log", "--format=%H %s", MAIN, "-n", "4000").splitlines()
        if " " in line
    ]


def main_contracts() -> set[SymbolKey]:
    """Contract index of main, materialised once through ``git archive``."""
    raw = subprocess.run(
        ["git", "archive", "--format=tar", MAIN, "--", "*.py"],
        capture_output=True,
        check=False,
    ).stdout
    sources: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive.getmembers():
            if member.isfile() and member.name.endswith(".py"):
                handle = archive.extractfile(member)
                if handle is not None:
                    sources[member.name] = handle.read().decode("utf-8", "replace")
    return build_index(sources)


def _const_names(diff: str) -> set[str]:
    names: set[str] = set()
    for line in diff.splitlines():
        if line.startswith("+"):
            match = CONST_RE.match(line)
            if match and match.group(1) not in SKIP:
                names.add(match.group(1))
    return names


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-superseded", action="store_true")
    args = parser.parse_args(argv)

    data = json.loads(FORENSICS.read_text(encoding="utf-8"))
    names = main_symbols()
    subjects = main_subjects()
    index = main_contracts()
    print(
        f"main: {len(names)} bare symbol names, {len(index)} qualified contracts, "
        f"{len(subjects)} commit subjects"
    )

    records = []
    parity: list[dict[str, Any]] = []
    for pr in data["records"]:
        number = pr["number"]
        ref = _ref_for(pr)
        diff = git("diff", "--unified=0", pr["merge_base"], ref)
        added = pr_contracts(pr)
        report = coverage_report(added, index)
        consts = sorted(name for name in _const_names(diff) if name in names)

        pr_subjects = [
            line.split(" ", 1)[1]
            for line in git("log", "--format=%H %s", f"{pr['merge_base']}..{ref}").splitlines()
            if " " in line
        ]
        landed_subjects = [s for s in pr_subjects if s in subjects]

        files = [entry["path"] for entry in pr["files"]]
        identical = sum(1 for entry in pr["files"] if entry["on_main_identical"])
        on_main = sum(1 for entry in pr["files"] if entry["on_main"])

        record: dict[str, Any] = {
            "number": number,
            "title": pr["title"],
            "head_sha": pr["head_sha"],
            "ahead": pr["ahead"],
            "behind": pr["behind"],
            "changed_files": len(files),
            "files_on_main": on_main,
            "files_identical_on_main": identical,
            "conflicts": pr["merge_tree_conflicts"],
            "added_symbols": report["added"],
            "symbols_on_main": report["bare_hits"],
            "symbol_coverage": report["bare_coverage"],
            "qualified_contracts": report["added"],
            "qualified_contracts_on_main": report["qualified_hits"],
            "qualified_contract_coverage": report["qualified_coverage"],
            "missing_symbols": [key[1] for key in report["missing_contracts"][:40]],
            "missing_contracts": [list(key) for key in report["missing_contracts"][:40]],
            "added_contracts": [list(key) for key in sorted(added)],
            "constants_on_main": consts[:20],
            "commits": len(pr_subjects),
            "commit_subjects_landed_on_main": len(landed_subjects),
            "draft": pr["draft"],
            "unresolved_threads": pr["unresolved_threads"],
            "reviews": pr["reviews"],
            "linked_refs": pr["linked_refs"],
            "files": files,
        }
        records.append(record)
        if number in SUPERSEDED_NUMBERS:
            parity.append(verify_superseded_contract_parity(record, index))

        qualified = (
            "n/a" if report["qualified_coverage"] is None else f"{report['qualified_coverage']:.0%}"
        )
        bare = "n/a" if report["bare_coverage"] is None else f"{report['bare_coverage']:.0%}"
        print(
            f"  #{number:<4} contracts={report['qualified_hits']:>3}/{report['added']:<3} "
            f"(q={qualified:>4} bare={bare:>4}) files_on_main={on_main}/{len(files)} "
            f"identical={identical} subjects_on_main={len(landed_subjects)}/{len(pr_subjects)}"
        )

    overlap = []
    for position, left in enumerate(records):
        left_files = set(left["files"])
        for right in records[position + 1 :]:
            right_files = set(right["files"])
            union = left_files | right_files
            if not union:
                continue
            jaccard = len(left_files & right_files) / len(union)
            if jaccard >= 0.2:
                overlap.append(
                    {
                        "a": left["number"],
                        "b": right["number"],
                        "jaccard": round(jaccard, 3),
                        "shared_files": sorted(left_files & right_files)[:20],
                    }
                )

    payload = {
        "main_sha": data["main_sha"],
        "open_total": data["open_total"],
        "main_qualified_contracts": len(index),
        "records": records,
        "overlap": sorted(overlap, key=lambda edge: (-edge["jaccard"], edge["a"], edge["b"])),
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")

    if args.verify_superseded:
        parity_payload = {
            "main_sha": data["main_sha"],
            "main_qualified_contracts": len(index),
            "records": sorted(parity, key=lambda row: row["number"]),
        }
        PARITY_OUT.write_text(
            json.dumps(parity_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        for row in parity_payload["records"]:
            print(
                f"  parity #{row['number']}: exact={row['exact_matches']}/{row['added_contracts']} "
                f"relocated={len(row['relocated'])} missing={len(row['missing'])} "
                f"parity={row['parity']}"
            )
        print(f"wrote {PARITY_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
