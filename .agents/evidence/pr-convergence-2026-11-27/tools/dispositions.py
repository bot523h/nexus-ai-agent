#!/usr/bin/env python3
"""Emit the disposition matrix, dependency graph, final registry and final report.

Inputs (all produced by the sibling tools in this directory):
  PR_FORENSICS.json   live GitHub + git evidence per open PR
  PR_ANALYSIS.json    symbol coverage / overlap evidence
  MERGE_RESULT.json   optional: the exact merge evidence for a MERGED PR
  CLOSURE_RESULT.json optional: one record per PR closed by the campaign
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_SHA = "54073d1f4fde8c1d5b8f2b1837c3d1f8a442f1cf"
LIVE_367_HEAD = "31895925a757e24984d9056aec588d8d123fa483"
CAMPAIGN = "NEXUS-PR-BACKLOG-CONVERGENCE-2026-11-27"

SUPERSEDED = {
    135: (
        "Studio PlanTransaction substrate",
        "main defines PlanTransaction in creative/studio/{bus,plan_execution,runtime}.py; "
        "symbol coverage 77/82 (94%) and 11 unresolved threads predate the merged substrate.",
    ),
    140: (
        "Studio CommandBus hardening",
        "same canonical substrate on main; symbol coverage 77/82 (94%); 7 unresolved threads.",
    ),
    205: (
        "Asset Library V1",
        "main ships the canonical library at creative/assets/{service,blob_store,ingestion,admission,mime}.py "
        "plus migration e575469d90b9 and six integration suites; 164/214 added symbols already exist.",
    ),
    295: (
        "content-addressed asset library extraction",
        "would add execution/asset_library.py as a *second* storage authority next to the canonical "
        "creative/assets/ service (forbidden by the constitution); capability already on main.",
    ),
    301: (
        "engineering-debt watchdog reports",
        "the watchdog automation is on main (.agents/automation/debt_watchdog_prompt.md and the "
        "2026-10-30/31 reports); this PR only adds dated 2026-11-01..12 snapshots.",
    ),
}


SUPERSEDED_EVIDENCE = {
    135: [
        "src/nexus_ai_agent/creative/studio/plan_execution.py",
        "src/nexus_ai_agent/creative/studio/bus.py",
        "src/nexus_ai_agent/creative/studio/runtime.py",
    ],
    140: [
        "src/nexus_ai_agent/creative/studio/bus.py",
        "src/nexus_ai_agent/creative/studio/plan_execution.py",
    ],
    205: [
        "src/nexus_ai_agent/creative/assets/service.py",
        "src/nexus_ai_agent/creative/assets/blob_store.py",
        "src/nexus_ai_agent/creative/assets/ingestion.py",
        "migrations/versions/e575469d90b9_nagar_asset_library_v1.py",
    ],
    295: [
        "src/nexus_ai_agent/creative/assets/service.py",
        "src/nexus_ai_agent/creative/assets/blob_store.py",
        "src/nexus_ai_agent/execution/__init__.py",
    ],
    301: [
        ".agents/automation/debt_watchdog_prompt.md",
        ".agents/evidence/AGENT_DEBT_WATCHDOG_2026-10-31.md",
    ],
    285: [".agents/evidence/pr-convergence-2026-11-27/extracted/board-285.diff"],
    297: [
        ".agents/evidence/M16_CONVERGENCE_PLAN_2026-10-31.md",
        ".agents/evidence/M16_TRUE_CLOSEOUT_2026-10-31.md",
        ".agents/evidence/PR_CONVERGENCE_2026-10-31.md",
        ".agents/evidence/PR_REGISTRY_2026-10-31.json",
    ],
    313: [".agents/evidence/pr-convergence-2026-11-27/extracted/board-313.diff"],
    329: [".agents/evidence/pr-convergence-2026-11-27/extracted/board-329.diff"],
}

EXTRACTED = {
    285: (
        "board sync for task-278",
        "board snapshot is 751 commits stale; the added record is preserved as extracted/board-285.diff.",
    ),
    297: (
        "M16 convergence evidence",
        "the four M16 evidence files do not exist on main and were cherry-picked onto the convergence branch.",
    ),
    313: (
        "board registration task-nagar-preservation-media-profiles-01",
        "registration absent from main; preserved verbatim as extracted/board-313.diff.",
    ),
    329: (
        "board registration NAGAR-IBPC-TRIAD-CLOSURE-01",
        "registration absent from main; preserved verbatim as extracted/board-329.diff.",
    ),
}

FAMILY = {
    33: "security",
    58: "security",
    89: "security",
    99: "security",
    114: "security",
    115: "security",
    149: "security",
    257: "security",
    57: "runtime",
    60: "runtime",
    63: "runtime",
    92: "runtime",
    113: "runtime",
    117: "runtime",
    118: "runtime",
    121: "runtime",
    122: "runtime",
    193: "runtime",
    204: "runtime",
    64: "creative",
    66: "creative",
    69: "creative",
    73: "creative",
    97: "creative",
    327: "creative",
    328: "creative",
    129: "studio",
    150: "studio",
    230: "studio",
    273: "causal-memory",
    284: "causal-memory",
    84: "governance",
    137: "governance",
    157: "governance",
}

FAMILY_TASK = {
    "security": "pr-backlog-security-2026-11-27",
    "runtime": "pr-backlog-runtime-2026-11-27",
    "creative": "pr-backlog-creative-2026-11-27",
    "studio": "pr-backlog-studio-2026-11-27",
    "causal-memory": "pr-backlog-causal-memory-2026-11-27",
    "governance": "pr-backlog-governance-2026-11-27",
}

ARCH_DECISION = {
    193: "Python 3.14 / Ubuntu 26.04 baseline is an owner decision; main targets 3.13.",
    328: "NAGAR Studio PWA is a product surface decision on top of the read API.",
    84: "registering ENGINEERING_CONSTITUTION.md as an enforced CI contract changes governance.",
    157: "the NAGAR agent constitution introduces a new governance layer.",
    118: "authority-race/probe-permit semantics change canonical execution ownership.",
    327: "Creative Capability Composition changes the canonical composition contract.",
    273: "causal-memory NEL semantic-time contracts need an architecture decision.",
    284: "read-only Causal Memory introduces a new memory capability.",
}


def load(name: str) -> dict | list | None:
    path = ROOT / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    forensics = load("PR_FORENSICS.json")
    analysis = load("PR_ANALYSIS.json")
    merge_result = load("MERGE_RESULT.json") or {}
    closures = load("CLOSURE_RESULT.json") or []
    assert isinstance(forensics, dict) and isinstance(analysis, dict)
    per = {row["number"]: row for row in analysis["records"]}
    closed = {row["number"]: row for row in closures}

    matrix = []
    for pr in forensics["records"]:
        number = pr["number"]
        stats = per[number]
        if number == 367:
            disposition = "MERGED" if merge_result.get("number") == 367 else "BLOCKED_ACTIVE_OWNER"
            family = None
            why = (
                "The only genuine merge candidate: closes the two authorization gaps left by #365, "
                "0 merge-tree conflicts, 0 unresolved threads, base updated onto main 54073d1f. "
                "Left OPEN because its owner (task-w35-ibpc-security-closure) is actively pushing "
                "(head a8a06eee -> 31895925, 10 commits / 10 files during this campaign); merging an "
                "in-flight branch would violate the ownership contract. Resume: owner finishes CI on "
                "the exact head and merges, or the convergence owner merges after a stable green head."
            )
            evidence = [
                "docs/engineering/NAGAR_IBPC.md",
                "src/nexus_ai_agent/nagar/ibpc/service.py",
            ]
            resume = ""
        elif number in SUPERSEDED:
            capability, why = SUPERSEDED[number]
            disposition = "SUPERSEDED_AND_CLOSED"
            family = None
            evidence = SUPERSEDED_EVIDENCE.get(number, [capability])
            resume = ""
        elif number in EXTRACTED:
            capability, why = EXTRACTED[number]
            disposition = "EXTRACTED_AND_CLOSED"
            family = None
            evidence = SUPERSEDED_EVIDENCE.get(number, [capability])
            resume = ""
        else:
            disposition = "DEFERRED_TO_BOARD_AND_CLOSED"
            family = FAMILY[number]
            why = (
                f"{pr['ahead']} commits / {pr['changed_files']} files ahead of main, "
                f"{pr['behind']} commits behind, {pr['merge_tree_conflicts']} merge-tree conflicts, "
                f"{pr['unresolved_threads']} unresolved threads, "
                f"symbol coverage {stats['symbols_on_main']}/{stats['added_symbols']}."
            )
            evidence = sorted({entry["path"] for entry in pr["files"]})[:8]
            resume = (
                "Rebase onto main (>= 54073d1f), re-derive the delta against the canonical "
                "implementation, run make lint/types/test, reopen the PR or open a successor branch."
            )
            if number in ARCH_DECISION:
                why += " " + ARCH_DECISION[number]

        matrix.append(
            {
                "number": number,
                "title": pr["title"],
                "head_ref": pr["head_ref"],
                "head_sha": pr["head_sha"],
                "base_ref": pr["base_ref"],
                "base_sha": pr["base_sha"],
                "main_sha": MAIN_SHA,
                "draft": pr["draft"],
                "ahead": pr["ahead"],
                "behind": pr["behind"],
                "changed_files": pr["changed_files"],
                "additions": pr["additions"],
                "deletions": pr["deletions"],
                "merge_tree_conflicts": pr["merge_tree_conflicts"],
                "unresolved_threads": pr["unresolved_threads"],
                "symbol_coverage": stats["symbol_coverage"],
                "symbols_on_main": stats["symbols_on_main"],
                "added_symbols": stats["added_symbols"],
                "files_identical_on_main": stats["files_identical_on_main"],
                "disposition": disposition,
                "family": family,
                "board_task": FAMILY_TASK[family] if family else None,
                "rationale": why,
                "evidence": evidence,
                "resume_condition": resume,
                "closure": closed.get(number),
                "live_head_sha": LIVE_367_HEAD if number == 367 else None,
                "live_changed_files": 10 if number == 367 else None,
            }
        )

    matrix.sort(key=lambda row: row["number"])
    (ROOT / "PR_CONVERGENCE_DISPOSITION_MATRIX.json").write_text(
        json.dumps(
            {"campaign": CAMPAIGN, "main_sha": MAIN_SHA, "records": matrix},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    registry = {
        "campaign": CAMPAIGN,
        "generated_at": forensics["generated_at"],
        "main_sha": MAIN_SHA,
        "open_at_inventory": forensics["open_total"],
        "records": [
            {
                "number": row["number"],
                "title": row["title"],
                "head_sha": row["head_sha"],
                "head_ref": row["head_ref"],
                "disposition": row["disposition"],
                "board_task": row["board_task"],
                "closed": bool(row["closure"]),
            }
            for row in matrix
        ],
    }
    (ROOT / "PR_CONVERGENCE_FINAL_REGISTRY.json").write_text(
        json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    lines = ["graph LR"]
    for row in matrix:
        node = f"P{row['number']}"
        label = f"#{row['number']} {row['disposition'].split('_')[0]}"
        lines.append(f'  {node}["{label}"]')
    seen = set()
    for edge in analysis["overlap"]:
        if edge["jaccard"] < 0.4:
            continue
        key = (edge["a"], edge["b"])
        if key in seen:
            continue
        seen.add(key)
        kind = "same_deliverable" if edge["jaccard"] >= 0.6 else "overlaps"
        lines.append(f"  P{edge['a']} -->|{kind} {edge['jaccard']:.2f}| P{edge['b']}")
    lines += [
        "  P367 -->|depends_on| M365[#365 merged]",
        "  P295 -.->|same_deliverable| P205",
        "  P295 -.->|extracts_from| M218[#218 closed]",
        "  P135 -->|same_deliverable| P140",
        "  P84 -->|requires_architecture_decision| ADR1[governance ADR]",
        "  P157 -->|requires_architecture_decision| ADR1",
        "  P193 -->|requires_architecture_decision| ADR2[Python 3.14 baseline]",
        "  P328 -->|requires_architecture_decision| ADR3[Studio PWA]",
    ]
    (ROOT / "PR_CONVERGENCE_DEPENDENCY_GRAPH.mmd").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

    counts: dict[str, int] = {}
    for row in matrix:
        counts[row["disposition"]] = counts.get(row["disposition"], 0) + 1
    report = [
        "# NEXUS PR backlog convergence — final report",
        "",
        f"campaign `{CAMPAIGN}` · main `{MAIN_SHA[:12]}` · inventory `{forensics['open_total']}` open PRs",
        "",
        "## Dispositions",
        "",
        "| disposition | count |",
        "|---|---|",
    ]
    for key in sorted(counts):
        report.append(f"| `{key}` | {counts[key]} |")
    report += [
        "",
        "## Per-PR matrix",
        "",
        "| PR | head | disposition | family | ahead/behind | conflicts | threads |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in matrix:
        report.append(
            f"| #{row['number']} | `{row['head_sha'][:8]}` | `{row['disposition']}` | "
            f"{row['family'] or '—'} | {row['ahead']}/{row['behind']} | {row['merge_tree_conflicts']} | {row['unresolved_threads']} |"
        )
    report += [
        "",
        "## Blocked / active owner",
        "",
        f"#367 is the only PR left open. Its snapshot head was `{next(r['head_sha'] for r in matrix if r['number'] == 367)}`; "
        f"the live head at report time is `{LIVE_367_HEAD}` (10 commits, 10 files, base `54073d1f`), pushed by the "
        "`task-w35-ibpc-security-closure` owner while this campaign ran. Merging an in-flight branch would violate "
        "the ownership contract, so the convergence records it as `BLOCKED_ACTIVE_OWNER` and does not close or merge it.",
        "",
        "## Merge evidence",
        "",
        f"```json\n{json.dumps(merge_result, indent=2)}\n```"
        if merge_result
        else "_(none performed by this campaign: the single merge candidate #367 remains with its active owner)_",
        "",
        "## Closures",
        "",
        f"`{len(closures)}` closure records in `CLOSURE_RESULT.json`."
        if closures
        else "_(pending)_",
    ]
    (ROOT / "PR_CONVERGENCE_FINAL_REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    print("counts:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
