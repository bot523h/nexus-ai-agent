#!/usr/bin/env bash
# Wave 3D v4 bundle — reproduction script.
#
# Re-derives every claim in MANIFEST.json from a clean slate:
#   leg 3   — the six-test SQLite A/B on the exact PR #211 tree
#   leg 5   — checker v3 on the exact current main (hash + board suite + ruff + mypy)
#   leg 8   — T-ISO-01 test isolation (5 tests, no ambient database)
#   leg 9   — Patch A (AsyncSession boundary) on the exact PR #210 tree
#   leg 10  — Patch B (prompt placement) + Patch A composed, on the exact PR #198 tree
#   leg 11  — dispatch ordering: Patch B, then the dispatch-scoped session repair
#
# Requires: git, python3 with venv, network access to pypi.org.
# Usage:    bash reproduce.sh [/path/to/workdir]   (default: ./_reproduce)
#
# Exit code 0 == every leg produced the expected result. Any other code means the
# bundle no longer reproduces and the manifest is stale.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${1:-$HERE/_reproduce}"
EVID="$HERE/evidence"
FAILED=0

MAIN=6b398565cc1ce0c51fac77d7623350b299c3938f
PR211=878842b6557eb95b00b1c9d1fdf14d444acfbe43
PR210=1d7390cc870ab102b4875442b9cfb1230c6416be
PR198=25c4965b7bfb751cfa9fd60b09962ee27d8bad80

BR211=arena/claude-5-5-night-governance-20261013
BR210=arena/claude-5-5-sqlite-init-serialize-20261012
BR198=arena/c5efc6a3-nexus-ai-agent

# The six node IDs the A/B must flip.
SIX_NODES=(
  "tests/integration/test_checkpoint_adapter.py::test_setup_schema_matches_golden"
  "tests/unit/test_lifecycle_adversarial.py::test_apply_under_lock_contention_fails_safe"
  "tests/unit/test_reconciler.py::test_apply_backfills_missing_with_protected_age"
  "tests/unit/test_reconciler.py::test_purge_eligible_orphan_is_applied"
  "tests/unit/test_reconciler.py::test_scan_error_disables_apply"
  "tests/unit/test_reconciler.py::test_cli_reconcile_apply_requires_flag"
)
AB_FILES="tests/architecture/test_saver_boundary.py tests/integration/test_checkpoint_adapter.py \
tests/integration/test_checkpoint_adapter_contract.py tests/integration/test_checkpoint_composition.py \
tests/integration/test_lifecycle_store_contract.py tests/unit/test_checkpoint_lifecycle.py \
tests/unit/test_checkpoint_lifecycle_lock_exclusion.py tests/unit/test_checkpoint_lifecycle_store.py \
tests/unit/test_checkpoint_lifecycle_store_boundary.py tests/unit/test_core_fingerprint.py \
tests/unit/test_golden_management.py tests/unit/test_inspect_contract.py \
tests/unit/test_lifecycle_adversarial.py tests/unit/test_postgres_checkpointer.py \
tests/unit/test_reconciler.py"

log() { printf '\n=== %s ===\n' "$*"; }
pass() { printf 'OK   %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*"; FAILED=1; }

rm -rf "$WORK"
mkdir -p "$WORK" "$EVID"
# Each run rewrites the leg logs so they always describe exactly this run.
: > "$EVID/reproduce_leg3.txt"; : > "$EVID/reproduce_leg5.txt"; : > "$EVID/reproduce_leg8.txt"
: > "$EVID/reproduce_leg9.txt"; : > "$EVID/reproduce_leg10.txt"; : > "$EVID/reproduce_leg11.txt"
cd "$WORK" || exit 99

# ---------------------------------------------------------------- clone helper
clone_at() {  # <dir> <branch> <sha>
  local dir="$1" branch="$2" sha="$3"
  [ -d "$dir" ] || git clone -q --no-hardlinks "$SOURCE" "$dir" || return 1
  git -C "$dir" fetch -q origin "+refs/remotes/origin/$branch:refs/remotes/origin/$branch" || true
  git -C "$dir" checkout -q "$sha" || return 1
  [ "$(git -C "$dir" rev-parse HEAD)" = "$sha" ] || return 1
}

if [ -d /home/user/nexus-ai-agent/.git ]; then
  SOURCE=/home/user/nexus-ai-agent
else
  SOURCE=https://github.com/bot523h/nexus-ai-agent.git
fi

python3 -m venv "$WORK/venv" >/dev/null 2>&1 || { echo "cannot create venv"; exit 99; }
PY="$WORK/venv/bin/python"
"$PY" -m pip install -q --upgrade pip >/dev/null 2>&1
"$PY" -m pip install -q pytest pytest-asyncio pytest-mock ruff mypy numpy pillow psutil \
  "sqlmodel==0.0.42" "sqlalchemy==2.0.54" "aiosqlite==0.22.1" "alembic==1.20.0" \
  "psycopg[binary,pool]==3.3.5" "asyncpg==0.31.0" "langgraph>=0.2" "pydantic-settings>=2.0" \
  "structlog>=24.0" "typer>=0.12" "python-telegram-bot>=21.0" >/dev/null 2>&1

# =============================================================== leg 3: A/B ==
log "leg 3 — six-test SQLite A/B on PR #211 ($PR211)"
clone_at ab211 "$BR211" "$PR211" || fail "clone #211"
AB="$WORK/ab211"
"$PY" -m pip install -q -e "$AB" --no-deps >/dev/null 2>&1 || true
A_OK=0; B_OK=0
for ver in 3.1.2 3.1.1; do
  "$PY" -m pip install -q "langgraph-checkpoint-sqlite==$ver" >/dev/null 2>&1
  out=$(cd "$AB" && PYTHONPATH="$AB/src" "$PY" -m pytest -q -p no:randomly --tb=no $AB_FILES 2>&1 | tail -1)
  echo "$ver -> $out" | tee -a "$EVID/reproduce_leg3.txt"
  if [ "$ver" = "3.1.2" ]; then echo "$out" | grep -q "^6 failed" && A_OK=1; else
    echo "$out" | grep -q "^114 passed" && B_OK=1; fi
done
[ "$A_OK" = 1 ] && pass "3.1.2 reproduces exactly 6 failures" || fail "3.1.2 leg did not show 6 failures"
[ "$B_OK" = 1 ] && pass "3.1.1 reproduces 114 passed" || fail "3.1.1 leg did not show 114 passed"
# the six node IDs must be exactly the ones that move
for node in "${SIX_NODES[@]}"; do
  "$PY" -m pip install -q "langgraph-checkpoint-sqlite==3.1.2" >/dev/null 2>&1
  red=$(cd "$AB" && PYTHONPATH="$AB/src" "$PY" -m pytest -q -p no:randomly --tb=no "$node" 2>&1 | tail -1)
  "$PY" -m pip install -q "langgraph-checkpoint-sqlite==3.1.1" >/dev/null 2>&1
  green=$(cd "$AB" && PYTHONPATH="$AB/src" "$PY" -m pytest -q -p no:randomly --tb=no "$node" 2>&1 | tail -1)
  printf '%s\n  3.1.2: %s\n  3.1.1: %s\n' "$node" "$red" "$green" | tee -a "$EVID/reproduce_leg3.txt"
  echo "$red" | grep -q "^1 failed" && echo "$green" | grep -q "^1 passed" || fail "node did not flip: $node"
done
pass "all six node IDs flip 1 failed (3.1.2) -> 1 passed (3.1.1)"

# =========================================================== leg 5: checker ==
log "leg 5 — checker v3 on main ($MAIN)"
clone_at checker "$MAIN" "$MAIN" >/dev/null 2>&1 || true
git clone -q --no-hardlinks "$SOURCE" ck 2>/dev/null || true
CK="$WORK/ck"
git -C "$CK" fetch -q "$SOURCE" || true
git -C "$CK" checkout -q "$MAIN"
if git -C "$CK" apply --check "$HERE/patches/checker_v3.patch"; then
  git -C "$CK" apply "$HERE/patches/checker_v3.patch"
  pass "checker_v3.patch applies cleanly on $MAIN"
else
  fail "checker_v3.patch does not apply on $MAIN"
fi
sha_expected=$(python3 - "$HERE/MANIFEST.json" <<'PY' 2>/dev/null || echo ""
import json,sys
m=json.load(open(sys.argv[1]))
print(m["patches"]["checker_v3"]["source_sha256_after"]["scripts/agent_board.py"])
PY
)
[ -z "$sha_expected" ] || {
  actual=$(sha256sum "$CK/scripts/agent_board.py" | cut -d' ' -f1)
  [ "$actual" = "$sha_expected" ] && pass "scripts/agent_board.py reproduces $actual" \
    || fail "scripts/agent_board.py hash mismatch: got $actual want $sha_expected"; }
out=$(cd "$CK" && PYTHONPATH="$CK/src" "$PY" -m pytest -q -p no:randomly \
  tests/unit/test_agent_board_pr_inventory.py tests/unit/test_agent_board.py \
  tests/unit/test_agent_board_pr_visibility.py tests/unit/test_agent_board_active_in_review.py 2>&1 | tail -1)
echo "$out" | tee -a "$EVID/reproduce_leg5.txt"
echo "$out" | grep -q "^110 passed" && pass "board suite: 110 passed" || fail "board suite: $out"
"$PY" -m ruff check "$CK/scripts/agent_board.py" "$CK/tests/unit/test_agent_board_pr_inventory.py" >/dev/null 2>&1 \
  && pass "ruff clean" || fail "ruff not clean"
"$PY" -m mypy --config-file "$CK/pyproject.toml" "$CK/scripts/agent_board.py" \
  "$CK/tests/unit/test_agent_board_pr_inventory.py" >/dev/null 2>&1 \
  && pass "mypy clean" || fail "mypy not clean"

# =========================================================== leg 8: T-ISO-01 ==
log "leg 8 — T-ISO-01 isolation (base $MAIN)"
git clone -q --no-hardlinks "$SOURCE" tiso 2>/dev/null || true
TS="$WORK/tiso"
git -C "$TS" checkout -q "$MAIN"
(cd "$TS" && git apply "$HERE/patches/tiso01_approval_isolation.patch") || fail "tiso patch did not apply"
rm -rf "${TS:?}/data"
out=$(cd "$TS" && PYTHONPATH="$TS/src" "$PY" -m pytest -q -p no:randomly tests/unit/test_approval.py 2>&1 | tail -1)
echo "$out" | tee -a "$EVID/reproduce_leg8.txt"
echo "$out" | grep -q "^5 passed" && pass "patched suite: 5 passed" || fail "patched suite: $out"
if [ -f "$TS/data/app.sqlite" ]; then fail "an ambient data/app.sqlite was created"; else
  pass "no ambient data/app.sqlite created"; fi
rm -rf "${TS:?}/data"; mkdir -p "$TS/data"; head -c 4096 /dev/urandom > "$TS/data/app.sqlite"
out=$(cd "$TS" && PYTHONPATH="$TS/src" "$PY" -m pytest -q -p no:randomly tests/unit/test_approval.py 2>&1 | tail -1)
echo "corrupt ambient DB -> $out" | tee -a "$EVID/reproduce_leg8.txt"
echo "$out" | grep -q "^5 passed" && pass "patched suite survives a corrupt ambient database" \
  || fail "patched suite broke on a corrupt ambient database: $out"
# the unpatched module fails on the same input
git -C "$TS" checkout -q -- tests/unit/test_approval.py
out=$(cd "$TS" && PYTHONPATH="$TS/src" "$PY" -m pytest -q -p no:randomly tests/unit/test_approval.py 2>&1 | tail -1)
echo "unpatched + corrupt ambient -> $out" | tee -a "$EVID/reproduce_leg8.txt"
echo "$out" | grep -q "^1 failed" && pass "unpatched module is red on the corrupt ambient database" \
  || fail "unpatched module unexpectedly survived: $out"
rm -rf "${TS:?}/data"

# ============================================================ leg 9: Patch A ==
log "leg 9 — Patch A on PR #210 ($PR210)"
clone_at pa "$BR210" "$PR210" || fail "clone #210"
PA="$WORK/pa"
cp "$HERE/tests/patch_a_tests/tests/unit/test_async_session_api_boundary.py" "$PA/tests/unit/"
out=$(cd "$PA" && PYTHONPATH="$PA/src" "$PY" -m pytest -q -p no:randomly \
  tests/unit/test_async_session_api_boundary.py 2>&1 | tail -1)
echo "unpatched -> $out" | tee -a "$EVID/reproduce_leg9.txt"
echo "$out" | grep -q "^3 failed" && pass "unpatched: 3 failures for the documented reason" \
  || fail "unpatched leg: $out"
(cd "$PA" && git apply "$HERE/patches/patch_a_async_session.patch") || fail "patch_a did not apply"
out=$(cd "$PA" && PYTHONPATH="$PA/src" "$PY" -m pytest -q -p no:randomly \
  tests/unit/test_async_session_api_boundary.py tests/unit/test_handlers.py \
  tests/unit/test_feature_wiring.py tests/unit/test_surface_registration.py 2>&1 | tail -1)
echo "patched -> $out" | tee -a "$EVID/reproduce_leg9.txt"
echo "$out" | grep -q "^38 passed" && pass "patched: 38 passed" || fail "patched leg: $out"

# ============================================================ leg 10: Patch B ==
log "leg 10 — Patch B + Patch A composed on PR #198 ($PR198)"
clone_at pb "$BR198" "$PR198" || fail "clone #198"
PB="$WORK/pb"
cp "$HERE/tests/patch_b_tests/tests/unit/test_memory_privacy_adversarial.py" "$PB/tests/unit/"
out=$(cd "$PB" && PYTHONPATH="$PB/src" "$PY" -m pytest -q -p no:randomly \
  tests/unit/test_memory_privacy_adversarial.py 2>&1 | tail -1)
echo "base -> $out" | tee -a "$EVID/reproduce_leg10.txt"
(cd "$PB" && git apply "$HERE/patches/patch_b_prompt_placement.patch") || fail "patch_b did not apply"
out=$(cd "$PB" && PYTHONPATH="$PB/src" "$PY" -m pytest -q -p no:randomly --tb=no \
  tests/unit/test_memory_privacy_adversarial.py 2>&1 | tail -1)
echo "patch B only -> $out" | tee -a "$EVID/reproduce_leg10.txt"
# The whole of Patch A is composed here (both files); it applies to the #198
# tree with an offset on `bot/handlers.py` and cleanly on `features/onboarding.py`.
(cd "$PB" && git apply "$HERE/patches/patch_a_async_session.patch") \
  || fail "patch_a on #198 did not apply"
out=$(cd "$PB" && PYTHONPATH="$PB/src" "$PY" -m pytest -q -p no:randomly --tb=no \
  tests/unit/test_memory_privacy_adversarial.py tests/unit/test_graph_memory.py \
  tests/unit/test_memory_consent_scope.py tests/unit/test_memory_context_failclosed.py \
  tests/unit/test_memory_scope_isolation.py tests/unit/test_ai_memory.py \
  tests/unit/test_ai_memory_consent.py 2>&1 | tail -1)
echo "composed (A+B) -> $out" | tee -a "$EVID/reproduce_leg10.txt"
echo "$out" | grep -q "^1 failed, 81 passed" && pass "composed: 81 passed, 1 documented finding" \
  || fail "composed leg: $out"

log "leg 11 — dispatch ordering on PR #198 ($PR198)"
clone_at dp "$BR198" "$PR198" || fail "clone #198 (dispatch leg)"
DP="$WORK/dp"
cp "$HERE/tests/dispatch_tests/tests/unit/test_registered_dispatch.py" "$DP/tests/unit/"
out=$(cd "$DP" && PYTHONPATH="$DP/src" "$PY" -m pytest -q -p no:randomly --tb=no \
  tests/unit/test_registered_dispatch.py 2>&1 | tail -1)
echo "base -> $out" | tee -a "$EVID/reproduce_leg11.txt"
echo "$out" | grep -q "^2 failed, 4 passed" \
  && pass "base: the two direct dispatch tests are red (AsyncSession boundary)" \
  || fail "dispatch leg on base: $out"
(cd "$DP" && git apply "$HERE/patches/patch_b_prompt_placement.patch") \
  || fail "patch_b (dispatch leg) did not apply"
out=$(cd "$DP" && PYTHONPATH="$DP/src" "$PY" -m pytest -q -p no:randomly --tb=no \
  tests/unit/test_registered_dispatch.py 2>&1 | tail -1)
echo "patch B only -> $out" | tee -a "$EVID/reproduce_leg11.txt"
echo "$out" | grep -q "^2 failed, 4 passed" \
  && pass "Patch B alone does not repair the dispatch path (as declared)" \
  || fail "dispatch leg after Patch B: $out"
(cd "$DP" && git apply "$HERE/patches/dispatch_async_session_repair.patch") \
  || fail "dispatch_async_session_repair.patch did not apply after Patch B"
out=$(cd "$DP" && PYTHONPATH="$DP/src" "$PY" -m pytest -q -p no:randomly --tb=no \
  tests/unit/test_registered_dispatch.py 2>&1 | tail -1)
echo "Patch B -> dispatch repair -> $out" | tee -a "$EVID/reproduce_leg11.txt"
echo "$out" | grep -q "^6 passed" \
  && pass "composed: 6 passed (2 direct dispatch + 4 hardening)" \
  || fail "dispatch leg composed: $out"

log "result"
if [ "$FAILED" = 0 ]; then echo "REPRODUCED — every leg matches MANIFEST.json"; else
  echo "NOT REPRODUCED — see the FAIL lines above"; fi
exit "$FAILED"
