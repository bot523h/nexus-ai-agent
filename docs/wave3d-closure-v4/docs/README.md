# Wave 3D closure bundle (v4) — how to use it

```
MANIFEST.json   every claim, hash, base SHA and expected result
reproduce.sh    re-derives all of it from a clean slate; exit 0 == reproduced
patches/        the seven patches, each with its declared base (see MANIFEST.patches)
tests/          the three reconstruction test modules (copies of the patched files)
evidence/       raw logs: A/B, checker, T-ISO-01, Patch A, Patch B, dispatch, step 16, handoff
docs/           SOURCE_TRUTH_MATRIX, INTEGRATION_MATRIX, POSTGRES_LIMITATIONS,
                PR211_OWNER_ACTION_PACKET, CLOSURE_REPORT, this file
```

## Run it

```bash
bash reproduce.sh            # uses ./_reproduce as its work directory
bash reproduce.sh /tmp/w3d   # or an explicit one
```

Requires `git`, `python3` with `venv`, and network access to pypi.org (the legs build their own
venvs). The script deletes its work directory and the leg logs before it starts, so a run never
inherits state from a previous one. Roughly 2–3 minutes.

Expected tail: `REPRODUCED — every leg matches MANIFEST.json`, exit code `0`.

## What is NOT in here

* No v1–v3 artifacts: they are **LOST** (see `MANIFEST.lost_artifacts`). Everything here is labelled
  RECONSTRUCTED with its own measured sha256.
* No merged state: every patch is validated on the exact upstream tree named in `MANIFEST.patches`,
  and none of it is integrated into any contributor's branch.
* No PostgreSQL or production verification: see `docs/POSTGRES_LIMITATIONS.md`.
