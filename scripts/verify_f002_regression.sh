#!/usr/bin/env bash
# Prove F-002's regression test turns RED when the fix is removed.
# Mutation: drop --single-transaction from the psql apply in restore.py.
set -u
export PATH=/usr/lib/postgresql/17/bin:$PATH
cd /workspace/neon-wt
: "${NEXUS_DATABASE_URL:?set NEXUS_DATABASE_URL to a disposable test cluster}"
: "${NEXUS_PG_TEST_DESTRUCTIVE:?set NEXUS_PG_TEST_DESTRUCTIVE=1 to authorise destructive writes}"
SRC=src/nexus_ai_agent/maintenance/restore.py
cp "$SRC" /tmp/restore.py.bak

python3 - <<'PY'
p = "src/nexus_ai_agent/maintenance/restore.py"
s = open(p).read()
needle = '            "--single-transaction",\n'
assert needle in s, "anchor missing"
open(p, "w").write(s.replace(needle, "", 1))
print("MUTATION APPLIED: removed --single-transaction")
PY

.venv/bin/python -m pytest -q -p no:randomly \
  tests/integration/test_postgres_restore_atomicity.py::test_failed_restore_leaves_target_intact \
  2>&1 | tail -12
RC=${PIPESTATUS[0]}

cp /tmp/restore.py.bak "$SRC"
echo "FIX RESTORED"
if [ "$RC" -ne 0 ]; then
  echo "PROOF OK: test is RED without the fix (exit $RC)"
else
  echo "PROOF FAILED: test stayed GREEN without the fix"
fi
exit 0
