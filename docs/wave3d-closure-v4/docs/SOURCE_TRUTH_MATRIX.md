# Source truth matrix (step 13)

Point-in-time truth: **2026-10-14**, repository `https://github.com/bot523h/nexus-ai-agent`.
Rule applied throughout: a patch is only ever applied to the tree it was authored against, and a
tree-hash mismatch stops the sequence instead of forcing an apply. No hash here was inferred from a
prefix — every value was produced by hashing the actual bytes.

## Live refs

| Ref | SHA | Tree |
|---|---|---|
| `origin/main` | `6b398565cc1ce0c51fac77d7623350b299c3938f` | `90e7fc2b6480fa1b550d0f4ed57baaf61d937409` |
| PR **#211** head | `878842b6557eb95b00b1c9d1fdf14d444acfbe43` | `a7d6fd4e9a72eda337cd4db11aa122e53a610f67` (merge-base with main `e69ddb0c1ec2`) |
| PR **#210** head | `1d7390cc870ab102b4875442b9cfb1230c6416be` | `225500038fa2fa83a962a8e8480416a3d6e04948` (base `main`, merge-base `6b398565`) |
| PR **#198** head | `25c4965b7bfb751cfa9fd60b09962ee27d8bad80` | `8eeb853e44f2b68a081d91b6a1e0e8d0ea322067` (merge-base `6b94f3d244a4`) |
| PR **#213** head | `29ca3d669126` (short; branch `arena/superagent-asset-hardening-20261013`) | base is **#210's branch**, not main |
| PR **#118** head | `d5a205f40a52` (short; branch `arena/01a0e846-nexus-ai-agent`) | merge-base `e5b326b2eaf6` |

PR states: #211 `MERGEABLE/BEHIND`, approved by the **bot** only · #210 `BLOCKED`,
`CHANGES_REQUESTED` · #198 `CONFLICTING/DIRTY`, `REVIEW_REQUIRED` · #213 `CONFLICTING/DIRTY`,
stacked on #210 · #118 `CONFLICTING/DIRTY`, `REVIEW_REQUIRED`. #212 is merged and **is** `main`.

## Patch truth

| Patch (sha256) | Required base | Applies on (git apply) | Verified result | Status |
|---|---|---|---|---|
| `checker_v3.patch` `a54daffa…` | main `6b398565` | main, #198, #210, #211 | `scripts/agent_board.py` → `b43cda74…`; 110 passed; ruff+mypy clean | VERIFIED |
| `tiso01_approval_isolation.patch` `b0d17ea4…` | main `6b398565` | main, #198, #210, #211 | 5 passed; no ambient `data/app.sqlite`; unpatched red on a corrupt ambient DB | VERIFIED |
| `patch_a_async_session.patch` `aa4feb40…` | **#210** `1d7390cc` (tree `22550003…`) | #210 clean; #198 with offset on `bot/handlers.py` | unpatched 3 failed/2 passed → patched 38 passed | VERIFIED on #210; composed on #198 in leg 10 |
| `patch_a_tests.patch` `b3221743…` | #210 (+Patch A) | #210 | included in the 38 passed | VERIFIED |
| `patch_b_prompt_placement.patch` `0240c551…` | **#198** `25c4965b` (tree `8eeb853e…`) | #198 clean; compatible elsewhere | privacy module 5 failed/22 passed → 3 failed/24 passed | VERIFIED |
| `patch_b_tests.patch` `59e3acc6…` | #198 | #198 | see leg 10 | VERIFIED |
| `dispatch_async_session_repair.patch` `74aa97ff…` | **#198** `25c4965b` | #198, main, #210, #211 | dispatch module 2 failed/4 passed → 6 passed (after Patch B) | VERIFIED |

"Applies elsewhere" is context-compatibility only. Validation status is only ever claimed for the
declared base, and only after the tests were executed there.

## Trees that were **not** validated

* No patch was applied to #211's tree beyond a `git apply --check` — #211's tree carries its own
  `scripts/agent_board.py`, so `checker_v3.patch` overlapping it (#211 touches
  `scripts/agent_board.py` and `tests/unit/test_agent_board.py`) must be re-validated **after** #211
  lands, on the new main.
* No patch was applied to #118's tree, and no claim is made about the #118 ↔ #198 taxonomy overlap.

## Recorded per-artifact reconstruction status

The Wave 3D v1–v3 bundles are **LOST**. The six hashes quoted in the originating report
(`tiso01 7214f6dd…`, `patch_a abeef86e…`, `patch_b 6b5975d7…`, `dispatch e5ec6175…`,
`checker 1d28dde9…`, `v4 597d112f…`) do **not** occur in any file present in this workspace, so
byte-identical restoration is impossible and is not claimed. Every artifact in this bundle is
labelled RECONSTRUCTED with its own measured sha256 and must not be compared against those prefixes.
