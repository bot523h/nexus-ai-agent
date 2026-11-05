# NEXUS-OMNI delta — dependency-vulnerability gate (2026-11-05)

Dated record (never a source of truth). Extends
`NEXUS_OMNI_SECURITY_PRIVACY_AUDIT_2026-11-03.md` and
`NEXUS_OMNI_CI_CD_SUPPLY_CHAIN_AUDIT_2026-11-03.md` with one P1 CI/security
remediation, its evidence, and the corrections it forces in the earlier
recommendations.

## 1. Finding (P1) — `pip-audit` was not an enforced gate

The prior audit enumerated the advisories by hand (a snapshot), but nothing in
CI re-ran `pip-audit`. A *new* advisory in the installed closure — or a
transitive dependency quietly relaxing a cap — would ship silently. That is the
difference between a report and a gate.

## 2. Remediation

- `scripts/dependency_audit.py` — runs `pip-audit --format=json` against the
  installed environment and fails unless every advisory id is either fixed or
  present in a reviewed allow-list.
- `.github/dependency-audit.json` — the reviewed allow-list. Each entry names
  its package and an acceptance `reason`:
  - `transitive-cap` (with `fixed` + `capped_by`): the gate re-derives, from the
    installed environment, the concrete specifier the installed `capped_by`
    dependency imposes and re-checks that `fixed` is still unresolvable. If the
    cap is gone, the entry is **STALE** and the gate **fails**.
  - `no-fix-released`: no released version of the package fixes the advisory
    yet; the gate re-checks it the same way.
  The acceptance is therefore *self-expiring*: a relaxed upstream cap or a
  published fix reds the build instead of passing silently.
- `.github/workflows/ci.yml` — new `dependency-audit` job. It runs the gate and
  then **proves it fails closed** with an empty allow-list (an in-job
  anti-vacuity step, not just a green run).
- `Makefile` — `make audit`.
- `pyproject.toml` — `pip-audit>=2.7` added to the `dev` extra (the tool must be
  a declared dev dependency, or `deptry` reds the lint job).
- `tests/unit/test_dependency_audit_gate.py` — 8 unit tests pinning the
  accept/stale decision with pure functions (no network), so the anti-vacuity
  property is itself a contract.

### Evidence (this tree)

```
$ python scripts/dependency_audit.py            # real allow-list
18 advisories: 18 accepted, 0 blocking          EXIT=0
$ python scripts/dependency_audit.py --allowlist /tmp/empty_allow.json
18 advisories: 0 accepted, 18 blocking          EXIT=1
$ python scripts/dependency_audit.py --allowlist /tmp/stale_allow.json   # a wrong capped_by
STALE CVE-2026-112940 (filelock) — no installed dependency caps filelock below 4.2.1 any more
18 advisories: 17 accepted, 1 blocking          EXIT=1
$ pytest tests/unit/test_dependency_audit_gate.py -q
8 passed
```

## 3. Correction to the earlier recommendation

`NEXUS_OMNI_DELTA_2026-11-05.md` §6 said the next step was to "raise the
`filelock` version floor to ≥ 4.2.1". **That is not achievable today, and the
gate now proves why**: `litellm` — a declared runtime dependency
(`litellm>=1.74,<2`) — requires `filelock<4.0,>=3.16.1` on every released
version including the latest (`1.108.0`, verified via the PyPI JSON API on
2026-11-05). So `filelock>=4.2.1` is unresolvable while `litellm` is installed;
the 13 `filelock` CVEs (fixed in 4.0.9 / 4.2.1) cannot be cleared without either
a `litellm` upgrade that relaxes the cap or a documented, deliberate downgrade
of `litellm`. The gate encodes this as a `transitive-cap` acceptance that will
turn STALE — and red the build — the moment `litellm` relaxes the pin.

The earlier `chromadb` / `diskcache` dispositions are unchanged but now
machine-enforced: both are `no-fix-released` (1.5.9 and 5.6.3 respectively are
the latest releases and still affected, verified via the OSV API on 2026-11-05),
and both are optional/transitive, not on a default path.

## 4. Now / Next / Later

- **Now:** merge the `dependency-audit` job; it is additive and self-checking.
- **Next:** when `litellm` publishes a release that drops `filelock<4.0`, the
  gate goes red automatically — bump `filelock` then, and delete the 13 entries.
- **Later:** fold the same gate into the optional-extras install legs if a
  future extra pulls a materially different closure.
