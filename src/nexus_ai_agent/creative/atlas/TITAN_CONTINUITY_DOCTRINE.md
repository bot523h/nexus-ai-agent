# NAGAR TITAN — Continuity Doctrine (Waves 13-20)

Status: **living** (updated with the code in the same PR).
Owner zone: `src/nexus_ai_agent/creative/atlas/**`, `tests/creative/atlas/**`.
Authority: **advisory**. TITAN owns no project state, mints no passport, dispatches
no command, and claims no registered production capability.

> Placement note: this doctrine lives inside the TITAN source zone rather than
> `docs/architecture/` because the shared documentation index (`docs/README.md`)
> is off-limits in this mission. The integration handoff to move it under
> `docs/architecture/` and index it there is recorded in §9.

---

## 1. Scope and honesty contract

TITAN is an offline, deterministic, bounded set of computational-cinematography
operators plus a proposal/analysis spine. It is **not** an inference engine for
biometric identity, **not** an autonomous film generator, and **not** a media
quality certificate.

| Status token | Meaning in TITAN |
|---|---|
| `COMPUTED` | The operator ran on the bounded fixture and its bytes were re-opened and verified. |
| `SKIPPED` | No bounded operator fixture exists for this pass; nothing was faked. |
| `PROPOSED` | A render pass is scheduled but no pixels were written. |
| `FEASIBLE` / `INFEASIBLE` | A declared energy balance is satisfied / violated. |
| `PROVEN_OPTIMAL` | The complete applicable finite search ran (LQ Riccati). |
| `UNKNOWN` / `NEEDS_REVIEW` | The input does not determine the answer. |

Absence of data never becomes `PASS`. A caller-supplied `verified=True` without a
non-empty trusted `evidence_ref` is treated as unverified.

---

## 2. Wave 13 — non-Euclidean portal metrics (`non_euclidean_portal_metrics.py`)

* Oriented polygon aperture with inward unit normal `n`; signed distance
  `d(p) = (p − c)·n`; containment by the winding (sum-of-angles) test in the
  aperture's plane basis `(u, v)`.
* Sub-frame crossing: the ray segment `p(s) = p0 + s·(p1 − p0)` meets the plane
  at `s* = d(p0) / (d(p0) − d(p1))`; a crossing is reported only when
  `0 < s* < 1` **and** the point is inside the aperture.
* Gluing: `x' = R x + t` with `R ∈ SO(3)` validated by `Rᵀ R = I`, `det R = +1`.
  Velocities transport as `v' = R v`, `ω' = R ω`, so the C1 continuity gap
  `‖v'_after − R v_before‖₂ ≡ 0` (a proof, not an estimate).
* Geodesic rays integrate the metric-consistent Christoffel symbols.

**GUM note.** All portal quantities are exact algebra; the only floating-point
error is the `SO(3)` check tolerance and the geodesic integrator step. The
continuity gap is reported as `0.0` because it is computed from the same `R`.

---

## 3. Wave 14 — volumetric atmospheric scattering (`volumetric_atmospheric_scattering.py`)

* Rayleigh coefficient `β_R(λ) ∝ λ⁻⁴` (normalised to the first wavelength). The
  code's measured ratio `β(460 nm)/β(650 nm) ≈ 3.987 ≈ (650/460)⁴`.
* Mie coefficient `β_M(λ) ∝ λ⁻ᵖ` with `p ≈ 1` (near-achromatic aerosol).
* Henyey-Greenstein phase `p(μ) = (1−g²) / (4π (1 + g² − 2gμ)^{3/2})` and Rayleigh
  phase `p(μ) = (3/16π)(1 + μ²)`; both integrate to `1.0` over the sphere (tested
  numerically).
* Single scatter accumulates in-scattered radiance with Beer-Lambert
  transmittance `T = exp(−σ_t · d)`; advection of a density field is
  mass-conserving.

**GUM note.** The phase-function integrals are verified by a spherical quadrature;
the reported deviation from `1.0` is the quadrature error (order `1e-3` for the
tests' resolutions), not a modelling claim.

---

## 4. Wave 15 — Eikonal navigation and social-force crowd (`eikonal_crowd_social_force.py`)

* Eikonal `|∇U| = C(x, y)` with `U|_G = 0`, solved by fast sweeping with the
  first-order Godunov upwind update taken **min per axis**:
  `U_ij = min over the one-sided stencils`. For a unit-cost grid the corner
  distance is within first-order error of the exact `√2`.
* Helbing-Molnár social force `F = m(v₀ − v)/τ + Σ f_ij + f_wall`, with a robust
  contact term that never divides by `_EPS` (clamped), plus explicit-Euler
  acceleration and speed clamps (`_MAX_ACCEL`, `_MAX_SPEED`) for stability.
* Position-based overlap healing drives `max_overlap → 0` every frame; walls are
  never penetrated.
* Topology audit reports finite-speed, left-right order and winding-orientation
  continuity with `camera_forward` defaulting to `[0, 1]`.

**GUM note.** The solver is first-order; the reported geodesic distances carry a
discretisation error `O(h)` that shrinks with grid spacing `h`. The crowd
trajectories are a declared numerical integration, not a measured physical path.

---

## 5. Wave 16 — Voronoi fracture and destruction ledger (`voronoi_fracture_destruction_ledger.py`)

* Voxel Voronoi: every solid voxel is assigned to the nearest seed; ties broken by
  the **lowest seed index**, so the assignment is deterministic. Per-cell counts
  are integers, so fragment masses sum to the body mass with `mass_drift == 0`.
* Impact energy budget `E_k = ½ m v²` versus fracture cost `E_s = G_c · A`; the
  verdict is `FEASIBLE` iff `E_k ≥ E_s`. No probability, no artistic score.
* `DestructionLedger`: append-only Merkle chain, frame hash
  `sha256(prev ‖ canonical_payload)`, payload quantised to integer microns so the
  hash is stable. `verify()` recomputes every frame and link; a tampered frame is
  detected at the exact index.

**GUM note.** `mass_drift` is exact (integer arithmetic); the only float is the
density scaling, reported to `1e-12`.

---

## 6. Wave 17 — Seidel aberrations and anamorphic optics (`anamorphic_seidel_optics.py`)

* Third-order wavefront
  `W(ρ, θ) = S₁ρ⁴ + S₂ρ³cos θ + S₃ρ²cos²θ + S₄ρ² + S₅ρ cos θ`
  (spherical, coma, astigmatism, Petzval field curvature, distortion), `ρ ∈ [0,1]`.
* `AnamorphicLens` squeeze `s = f_x / f_y`; circle-of-confusion
  `r_coc = defocus / (2·t_stop)`, `σ_y = r_coc / pitch`, `σ_x = σ_y / s`.
* Separable Gaussian PSF (optionally with a horizontal streak and per-channel
  chromatic shift), renormalised so each channel integrates to `1.0`.
* Convolution of a scene-linear-radiance image preserves total radiance to
  `< 1e-9` (verified on a real disk read-back in the spine).

**GUM note.** The energy residual is the measured `|E_out − E_in| / E_in`; it is
reported, not assumed. On small images with a bright point the edge-clamped
boundary inflates the residual (`~1e-5`), which is a boundary effect, not a
violation of PSF normalisation.

---

## 7. Wave 18 — Pontryagin/Riccati editing pacing (`pontryagin_editing_pacing_solver.py`)

Discrete finite-horizon LQ problem `x_{k+1} = A x_k + B u_k`,
`J = x_Nᵀ Q_f x_N + Σ (x_kᵀ Q x_k + u_kᵀ R u_k)` with `Q, Q_f ⪰ 0`, `R ≻ 0`.

Backward Riccati recursion with `P_N = Q_f`:

```
K_k = (R + Bᵀ P_{k+1} B)^{-1} Bᵀ P_{k+1} A
P_k = Q + Aᵀ P_{k+1} A − Aᵀ P_{k+1} B K_k
u_k* = −K_k x_k,   V_k = x_kᵀ P_k x_k,   λ_k = 2 P_k x_k
```

This is **exact** for the stated finite LQ problem, so `optimal=True`. The
independent grid-search oracle returns a cost that is `≥` the Riccati cost; the
gap is the grid discretisation, confirming (not assuming) optimality.

The pacing plan maps a control to a bounded duration
`clip(base · exp(−u), min, max)` — smooth, monotone, always physically valid.

**Scope.** `PROVEN_OPTIMAL` is claimed only for this precisely defined finite LQ
problem. It is not a claim about artistic quality or audience response.

---

## 8. Waves 19-20 — compiler and staged spine

* `titan_feature_film_compiler.compile_feature_film` consumes a versioned
  `StoryDocument`, runs the existing ATLAS `analyze_story` (causal + temporal +
  plan) **verbatim** — no re-implementation — and attaches a deterministic,
  sorted render-pass graph with a per-pass content hash and `pass_status =
  "PROPOSED"`. `estimated_seconds = total_frames / frame_rate` is a linear
  estimate, not a measured render time.
* `export_compiled_film` writes `compiled_film.json` through the authorized
  filesystem/staging boundary, **re-opens the bytes** and independently re-hashes
  them (`verified = (reopened == raw)`).
* `titan_staged_execution_spine.run_staged_spine` executes every pass on a bounded
  synthetic fixture with the **real** Wave 13-18 operators, persists each artifact
  through the boundary, re-opens and verifies it, and records a `COMPUTED`/
  `SKIPPED` stage plus a ledger SHA-256 over the canonical ledger. It never
  dispatches a command and never claims to have rendered real film pixels.

---

## 9. Integration handoff (for a later authorized PR)

The spine emits an inert handoff dict (`kind = "titan.compiled_film_handoff"`,
`authority = "advisory"`, `requires_authorization = true`). A later authorized PR
would:

1. map each `PROPOSED` pass onto the real `CommandBus`/rendering compiler, under
   the existing authorization and policy flow;
2. move this doctrine under `docs/architecture/` and index it in
   `docs/README.md` (off-limits here);
3. register any new capability in `OPERATION_TRUTH.json` only after a byte-exact
   re-render and static scan (the Operation Truth gate).

TITAN itself performs none of these edits.

---

## 10. References (inspected)

* Repository: `creative/rendering/compiler.py`, `creative/rendering/executor.py`,
  `execution/staging.py` (`AttemptStaging`), `tools/filesystem_policy.py`
  (`WorkspaceFilesystem`), `creative/episode_causality_lab/**` (ATLAS).
* Porter-Duff / Seidel / Helbing-Molnár / Eikonal / Riccati are textbook results
  restated here; the tests verify the specific numeric claims made above.
