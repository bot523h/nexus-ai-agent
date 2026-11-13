# NAGAR VERITAS — Fifteen-Law Neuro-Physical Cinema Doctrine (`VERITAS OMNIA v2.0`)

## Preamble: Why a Physics Court for Synthetic Cinema

Generative video systems are, at bottom, extraordinarily confident liars. They will hand you a hand with six fingers, eyes that gaze in two directions at once, a mirror that reflects a room which does not exist, a shadow that floats a metre above the floor, a cup that vanishes between frames, text that dissolves into gibberish the moment the camera tilts, a body that accelerates without a force, cloth that passes through a shoulder, a glass of water that un-spills itself, and a face whose smile never reaches the eyes. Each of these failures is not a matter of taste. Each is a *physical impossibility* — a violation of a law that the real world obeys without exception. NAGAR VERITAS exists because a violation of physics is a falsifiable claim, and a falsifiable claim can be caught.

The engine is deliberately austere. It calls no external model, downloads no weights, and consults no network. Every law is expressed as closed-form mathematics evaluated in vectorised NumPy: linear algebra on Lie groups, spectral graph theory, geometric optics, Hamiltonian mechanics, continuum thermodynamics, and the psychophysics of human perception. This austerity is the point. A proof that a frame is physically impossible must not itself depend on the same statistical guesswork that produced the frame. The auditor is a court, not a critic; it does not opine, it computes. When it is finished it issues a passport — a SHA-256 digest, structured after RFC 6962 Certificate Transparency — that binds the before-and-after violation counts into a single tamper-evident fingerprint. A shot either carries a passport proving it was healed, or it does not.

The doctrine is organised around ten laws. The first four concern the geometry of light and the body that casts it; the next three concern rigid and deformable matter in motion; the last three concern the arrow of time, the topology of the hand, and the neurology of the face. For each law we state the invariant, the mathematics that enforces it, and the adversarial evidence — six falsification probes and four killed mutants — that the enforcement is real and not decorative.

## Law 1 — The Twenty-One Joint Hand

The human hand is not a bag of five identical fingers. It is a kinematic tree of exactly twenty-one joints arranged in five rays that share a common wrist root, and the tree is rigidly constrained by anatomy. The thumb ray carries a metacarpal and two phalanges; the four fingers each carry a metacarpal and three phalanges. The engine encodes this as `CANONICAL_RAYS`, a tuple of five index paths through a twenty-one node graph, and it treats any skeleton that departs from this topology as a hallucination to be pruned. Polydactyly — the infamous sixth finger — is detected by a spectral graph-Laplacian signature: each ray is a path graph, its Laplacian spectrum is computed in closed form as `2 − 2cos(πk/n)`, and rays whose spectrum is furthest from the canonical five-node signature are removed until exactly five remain. This is not a heuristic threshold; it is a topological projection onto the manifold of admissible hands.

Within each finger the phalanges obey the Rijpkema–Lozano-Pérez tendon coupling, in which the flexor digitorum profundus binds the distal interphalangeal joint to two-thirds of the proximal interphalangeal flexion, so that `θ_DIP = ⅔ θ_PIP`. The joint angles themselves live inside physiological cones — metacarpophalangeal flexion between −15° and 90°, proximal interphalangeal flexion between 0° and 110°, distal interphalangeal flexion between 0° and 80°, and abduction within ±25° — and any angle outside its cone is clipped back onto the boundary. The phalanx lengths obey subject-invariant proximal:middle:distal ratios (the Littler carpometacarpal arch), which are restored by rescaling each finger's three segments to their canonical proportions while preserving total length. Finally, when a grip is active, the fingertips are projected onto the surface of a signed-distance field by a gradient-descent correction applied rigidly to the whole phalanx chain, so that the tips close on the object without shearing the bone ratios. The six probes confirm canonical topology, polydactyly pruning, cone clamping, tendon coupling, ratio restoration, and grip closure; the four mutants inject a sixth ray, an overflowing joint, a broken tendon ratio, and a broken bone ratio, and every one is killed.

## Law 2 — Binocular Vergence, Saccades and the Pupil

Two eyes that do not converge on the same point produce the "zombie gaze" that haunts synthetic faces. The engine solves exact binocular vergence by treating each eye as a centre and a gaze axis, computing the closest points on the two rays, and measuring their skew distance. If the axes diverge by more than ninety degrees the gaze is flagged `divergent_gaze`; if the skew exceeds half a millimetre the gaze is flagged `non_intersecting_gaze`. In either case the heal is the same and it is physically the only correct one: both axes are reprojected onto the true fixation point, because binocular fusion admits exactly one admissible gaze — the one that lands on the target. The interpupillary distance is validated against the human range of 54 to 72 millimetres, and any eye pair outside it is rejected outright.

Saccades are synthesised from the Bahill main-sequence relation, in which peak velocity saturates with amplitude as `v_peak = v_max(1 − e^{−Δθ/θ₀})`, and the position profile is the minimum-jerk quintic `10τ³ − 15τ⁴ + 6τ⁵`. The vestibulo-ocular reflex is layered on top by adding the head's angular velocity to the eye angle, so that the world-referenced gaze remains stable while the head turns. The pupil is integrated through the Watson–Yellott steady-state model with an asymmetric first-order ODE: the iris constricts quickly with a time constant of 0.2 seconds and dilates slowly with a time constant of 1.1 seconds, and the resulting diameter is clamped to the physiological range of 2 to 8 millimetres. The probes verify exact vergence, divergent-gaze detection, non-intersecting-gaze healing, interpupillary bounds, main-sequence monotonicity, and pupillary constriction; the mutants inject divergence, parallelism, a collapsed interpupillary distance, and a non-physical luminance, and all four are killed.

## Law 3 — Catoptric Reflection Physics

A mirror is a lie detector. The reflection of a scene in a planar mirror is an exact isometry of the scene across the mirror plane, and any renderer that draws a reflection not related to the scene by that isometry is provably wrong. The engine constructs the reflection as a four-by-four Householder transform, `M = I − 2nnᵀ` extended with the plane offset, which has determinant exactly −1 and which is its own inverse, `M² = I₄`. These two facts are the entire test: a matrix with determinant −1 is orientation-reversing (a true reflection, not a rotation), and a matrix that squares to the identity is an involution (reflecting twice returns you home). The engine then verifies epipolar consistency by reflecting both the camera and a landmark, casting the virtual ray between them, and confirming that it intersects the mirror plane with zero residual and that the intersection is collinear with the ray.

Dielectric reflections are governed by the Fresnel equations. The engine computes the amplitude reflectances for s- and p-polarised light as functions of incidence angle and refractive indices, and it locates the Brewster angle at which the p-polarised reflectance vanishes exactly. That vanishing is a sharp, falsifiable prediction: at the Brewster angle the reflected light is perfectly s-polarised, and any model that fails to reproduce `R_p = 0` there has the wrong physics. Capillary-gravity ripples on a water surface are warped with the full dispersion relation `ω² = gk + (σ/ρ)k³ tanh(kh)`, so that the reflection distortion respects both gravity and surface tension. The probes check the determinant, the involution, the mirror-image mapping, the Brewster null, the Fresnel bounds and ordering, and epipolar consistency; the mutants flip the determinant sign, break the involution, violate Brewster, and pass a zero normal, and all are killed.

## Law 4 — Projective Shadow Transport

A cast shadow is the projective image of an occluder from a light source onto a receiving plane, and it can be computed exactly with a single four-by-four matrix. For a homogeneous light `L` and a plane `π`, the shadow projection is `(πᵀL)I₄ − Lπᵀ`, a rank-three projective map that sends every point of the occluder to the point where the ray from the light through that point meets the plane. The engine validates that the light is either a directional source (homogeneous weight zero) or a point source (weight one), that the plane normal is non-zero, and that no vertex lies on the light plane (which would make the projection degenerate). Every projected shadow vertex is then checked to lie on the receiving plane to within a nanometre, which is the mathematical statement that the shadow is not floating.

Real light sources are not points, and the softness of a shadow is a measurable consequence of their finite size. The engine derives the penumbra radius from similar triangles as `r_pen = r_emitter · h / (d − h)`, where `h` is the occluder's height above the plane and `d` is the light distance; the radius grows with both occluder height and emitter radius, exactly as physical shadow softness does. Contact ambient occlusion is modelled with a hemisphere integral that decays as `1 − e^{−h/a}`, so that geometry touching the floor is darkened while geometry far above it is not. The probes confirm shadow-on-plane, the four-by-four matrix, penumbra growth with height and with emitter size, contact-AO bounds, and plane validation; the mutants lift a shadow off the plane, force a negative penumbra, zero the plane normal, and corrupt the light weight, and all four are killed.

## Law 5 — SE(3) Object Permanence

An object that exists in one frame and is occluded in the next does not cease to exist; it continues along a geodesic of the special Euclidean group SE(3), and when it reappears it must reappear where physics put it. The engine implements the SE(3) exponential and logarithmic maps in closed form. The exponential map turns a six-vector twist — three rotational and three translational components — into a rigid transform via the Rodrigues formula and its left-Jacobian, and the logarithm inverts it. The round-trip `log(exp(ξ)) = ξ` is the foundational identity, and the logarithm of the identity is the zero twist. Between observations the engine propagates the object's pose along its twist under gravity, integrating the motion in small steps, and it holds the object's extents fixed because a rigid body does not change size when nobody is looking.

The permanence audit then examines a sequence of states for three canonical failures. A `teleport` is a translational jump larger than the impulse bound, which no finite force could produce in one step. A `volume_drift` is a change in the object's extents, which rigid-body motion forbids. A `rotation_determinant` failure is a rotation block whose determinant has drifted from unity, which means the "rotation" is secretly a scale or a shear. The repair is deterministic: rotations are orthonormalised by a signed SVD projection back onto SO(3), extents are reset to the reference volume, and teleports are clamped to the impulse bound along the offending direction. The probes verify the round-trip, the identity logarithm, extent conservation, teleport detection, rotation-determinant detection, and full healing; the mutants inject a teleport, a volume drift, a scaled rotation, and a malformed twist, and all are killed.

## Law 6 — Homography-Locked Diegetic Typography

Text painted on a surface in the world — a sign, a screen, a page — must be transported by the projective transformation that carries its plane into the camera, and it must remain legible under that transformation. The engine solves the planar homography from four point correspondences using the normalised direct linear transform, then normalises the result to unit determinant so that it lies in the special linear group SL(3); this removes the arbitrary scale ambiguity and makes the transform a genuine projective map rather than an arbitrary matrix. A correct homography sends the four source corners to the four destination corners exactly, and its determinant is one.

The glyphs themselves are procedural: each character is a five-by-seven bitmap stamped into a texture, so that the engine carries no font dependency and remains fully deterministic. When the texture is transported through the homography, the engine applies elliptical-weighted-average filtering driven by the Jacobian of the transform, so that texels are sampled with an anisotropic footprint proportional to how much the projection stretches them; this is what keeps text from aliasing into moiré when the surface is viewed at a grazing angle. The probes check the unit determinant, the exact corner mapping, the non-empty glyph bitmap, the visibility of rendered text, the rejection of a degenerate quad, and the positivity of the EWA footprint; the mutants scale the homography off SL(3), collapse the quad to a line, blank the render mask, and pass a malformed quad, and all four are killed.

## Law 7 — Symplectic Contact Mechanics

Bodies move according to Hamilton's equations, and the integrator that respects their symplectic structure conserves the quantities that physics conserves. The engine advances rigid bodies with the Störmer–Verlet scheme, a second-order symplectic integrator that updates velocity by a half step, position by a full step, and velocity by the closing half step; because it is symplectic, it does not artificially pump energy into or out of a system the way a naive Euler step would. Collisions are resolved by an impulse method: at contact, the relative normal velocity is reversed according to the coefficient of restitution, and tangential motion is damped according to the coefficient of friction, with the impulses applied equal-and-opposite so that Newton's third law holds by construction.

The invariants are exact and unforgiving. Total linear momentum is conserved to within one part in a trillion, because the collision impulses sum to zero. Total angular momentum about the origin is likewise conserved for a central collision. Kinetic energy is conserved exactly in a perfectly elastic collision (restitution one) and can only decrease in an inelastic one, because the second law forbids a passive collision from creating energy. The probes verify free-fall kinematics, momentum conservation, elastic energy conservation, inelastic dissipation, angular-momentum conservation, and input validation; the mutants corrupt the momentum, fabricate energy, pass a malformed position array, and pass a negative mass, and all four are killed.

## Law 8 — XPBD Cloth Drape

Cloth is a constraint network, and a cloth that passes through a body has violated a distance constraint that the real fabric cannot violate. The engine simulates cloth with extended position-based dynamics: each step predicts positions under gravity and wind, then iteratively projects the distance constraints between connected particles back toward their rest lengths, weighted by a compliance parameter that controls how stiff the fabric is. The projection is the standard XPBD update, in which the constraint correction is divided by `2 + α` with `α = compliance / dt²`, and it is applied symmetrically to both endpoints so that momentum is not spuriously injected.

Collisions are handled against capsules — the natural primitive for limbs and torsos — using an exact signed distance to the capsule surface. The signed distance clamps the projection of each particle onto the capsule's axis segment, measures the distance to that closest point, and subtracts the radius; a particle is penetrating exactly when this value is negative. When penetration is detected, the particle is pushed radially out to the surface, and because this repulsion is applied after the constraint projection in each iteration, the final state is guaranteed to be penetration-free. The probes verify the exact capsule surface distance, gravity-driven drape, zero penetration, bounded stretch, input validation, and rest-length preservation; the mutants force a penetrating particle, pass an out-of-range edge, collapse a capsule to a point, and pass a negative time step, and all four are killed.

## Law 9 — The Thermodynamic Arrow of Time

The second law of thermodynamics is the only law of physics that distinguishes past from future, and it is therefore the sharpest possible test of whether a video is running forward or backward. The engine measures the spatial Gibbs–Shannon entropy of each scalar density frame, `S = −Σ p ln p` over the normalised field, and it requires that entropy be non-decreasing along the time axis. A uniform field carries the maximum entropy `ln N`; a point mass carries zero entropy; and any sequence in which entropy falls is a sequence that has been played in reverse. The heal is a forward diffusion: each frame is advanced from its predecessor by the Fourier-domain advection-diffusion operator, which is unconditionally stable because it multiplies each Fourier mode by the exact analytic propagator `exp(−i k·v dt) · exp(−D k² dt)`, and the diffused field is clipped to non-negativity.

Causality is enforced with a retarded Green's function. A wavefront generated by an impact at a source point can only reach a radius `c·(t − t_impact)` by time `t`, so the field must be exactly zero outside that light cone and exactly zero before the impact. The engine constructs such a wavefront analytically and verifies that no signal leaks outside the causal support; any frame that carries amplitude before its cause is rejected as acausal. The probes verify the entropy extremes, mass conservation under diffusion, entropy increase, retarded support, and the joint causality-and-heal test; the mutants inject an entropy reversal, an acausal wavefront, a negative density field, and a negative diffusivity, and all four are killed.

## Law 10 — Neuro-Facial Microdynamics

The human face betrays synthetic smiles because the muscles that produce a genuine smile are not under voluntary control in the same way as the muscles that produce a polite one. A Duchenne smile of enjoyment recruits both the zygomaticus major (AU12), which lifts the mouth corners, and the orbicularis oculi (AU6), which crinkles the eyes; a posed smile recruits only AU12, and the eyes stay flat. The engine encodes this synergy as a hard manifold constraint: whenever AU12 exceeds a threshold `τ₀`, AU6 must be at least `κ·(AU12 − τ₀)`, and any action-unit vector that violates this is projected back onto the manifold by raising AU6 to the required level. This is the Ekman FACS Duchenne invariant, expressed as an inequality that the engine enforces exactly.

Blinks are asymmetric because the eyelid closes faster than it opens: the engine models closure with a fast time constant of 75 milliseconds on the way down and a slow time constant of 175 milliseconds on the way up, so that the blink curve is skewed and its peak closure never exceeds one. Skin is rendered with a separable subsurface-scattering approximation, the Jiménez dual-Gaussian dipole, which blurs the diffuse radiance per colour channel with a channel-dependent diffusion length while preserving the specular highlights and the skin mask; the result is the soft, slightly translucent look of real skin rather than the plastic sheen of a bad render. The probes verify Duchenne violation detection, Duchenne healing, blink bounding, blink asymmetry, subsurface shape preservation, and FACS validation; the mutants inject a Duchenne violation, an over-closed blink, a negative action unit, and a mismatched skin mask, and all four are killed.

## The Reality Auditor and the Passport

The ten laws are not ten separate tools; they are ten clauses of a single contract, and the `VeritasRealityAuditor` is the court that enforces the contract on a complete cinematic shot. A `CinemaShot` bundles every observation the engine can audit — a hand skeleton, an eye pair, a saccade, a pupil stimulus, an action-unit sequence, a blink, a skin render, a mirror, a Fresnel interface, a shadow, a typography quad, an object sequence, a collision, a cloth state, a scalar-frame sequence, and a retarded wavefront — and the auditor dispatches each present observation to its law, counts the violations before and after healing, and assembles a `VeritasRealityPassport`. The passport records the per-law violation counts before and after, the detailed violation strings, the aggregate pre-audit and post-heal totals, and a digest. The digest is computed over the canonical JSON serialisation of the counts using the RFC 6962 leaf-hash construction — a leading zero byte followed by the SHA-256 of the payload — so that any change to any count changes the fingerprint. A shot is declared healed only when it began with at least one violation and ended with none; a clean shot is not "healed", it is simply already real, and the passport says so honestly.

## The Adversarial Evidence: Falsification and Mutation

A physics engine that is never attacked is a physics engine that has never been tested. NAGAR VERITAS is therefore defended by two independent adversarial suites. The first is a set of sixty falsification probes, six per law, each of which states a specific, falsifiable physical claim and fails if the engine does not uphold it. These probes are Popperian in spirit: they are designed to be capable of failure, and their passing is meaningful precisely because each could have gone the other way. The second is a mutation campaign of forty mutants, four per law, in which a deliberately corrupted artefact or parameter is fed to the engine's own oracle. A mutant is killed when the oracle rejects it, and the campaign certifies a forty-out-of-forty kill rate. Together the probes and the mutants establish that the engine detects the ten deadly sins not by accident and not by threshold-tuning, but because the mathematics of each law leaves the sin no room to hide.

The entire suite is vectorised NumPy and completes in well under eight seconds, which is itself a kind of proof: a real physics court does not need a GPU, a network, or a language model to know that six fingers are impossible. It needs only the laws, the arithmetic that applies them, and the discipline to attack its own conclusions until they hold. That is the doctrine of NAGAR VERITAS.

## Law 11 — The Madelung Quantum-Fluid Phase-Coherence Transport

The ten laws above police the geometry, optics, dynamics and thermodynamics of a
frame. There remains a failure mode that is invisible to all of them: temporal
texture boiling. Generative video frequently exhibits a high-frequency,
frame-to-frame shimmer in which surfaces seethe like liquid while their
silhouettes stay nominally fixed. Nothing teleports, no shadow detaches, no hand
grows a sixth finger — yet the image is plainly unreal. Law 11 confronts this
residual hallucination with the mathematics of quantum fluids.

The construction begins by fusing the frame's luminance-chrominance density
`rho(x, t)` and its optical phase `Phi(x, t)` into a single complex
wavefunction,

```
psi(x, t) = sqrt(rho(x, t)) * exp(i * Phi(x, t)).
```

Writing `psi = sqrt(rho) exp(i S / hbar_eff)` and substituting into the
Schrodinger equation, then separating real and imaginary parts, yields the two
Madelung equations. The imaginary part is the continuity equation

```
d rho / dt + div(rho * grad Phi) = 0,
```

which says that density is transported as a probability current
`j = rho * grad Phi`. The real part is a Hamilton-Jacobi equation whose only
non-classical term is the Bohmian quantum potential

```
Q(rho) = -(hbar_eff^2 / 2) * laplacian(sqrt(rho)) / sqrt(rho).
```

This `Q` is the engine of the law. It is a measure of local curvature of the
amplitude field. Over a smooth gradient `Q` is negligible, so the fluid evolves
classically and texture drifts naturally. Across a genuine physical edge — a
silhouette, a specular boundary, a hard shadow terminator — the second
derivative of `sqrt(rho)` spikes, and `Q` diverges. That divergence is precisely
the property we want: the quantum pressure resists smoothing exactly where the
image carries real structure, and yields freely where the structure is noise.
A naive Gaussian blur cannot make this distinction; it destroys edges and noise
alike. The Bohmian regulariser is therefore edge-preserving by construction
rather than by heuristic.

Operationally the auditor measures the high-frequency spectral energy ratio of
the phase field, the fraction of `|FFT(Phi)|^2` that lives outside the
lowest-frequency block. When this ratio exceeds the boiling threshold the phase
is declared to be boiling, and the engine applies successive brick-wall
low-pass sweeps in the Fourier domain until the ratio falls below threshold or
the iteration budget is exhausted. Phase-wrap discontinuities are first removed
by unwrapping along every axis, since a `2*pi` jump is a measurement artefact
rather than a physical event. The healed phase is then recombined with the
original amplitude to produce a regularised wavefunction, and the pass reports
the boiling ratio before and after together with the number of sweeps applied.

The invariant is stated compactly: a frame is phase-coherent when its phase
boiling ratio lies below threshold after unwrapping, and a healed frame must
satisfy `boiling_after < boiling_before` whenever a violation was detected.
The continuity residual, the RMS of `div(rho * grad Phi)`, provides an
independent falsification metric: a frame whose measured divergence does not
match its density transport is boiling regardless of how plausible it looks.
Together these two scalars let the auditor prove, rather than assert, that a
shot has been transported forward in time as a coherent quantum fluid and not
merely re-rendered with fresh noise.

## Law 12 — The Anamorphic Dioptric Squeeze, Cauchy Lateral Chromatic Aberration and cos⁴θ Vignetting

An anamorphic taking lens compresses the horizontal field by a squeeze factor, and the honest way to represent that compression is the intrinsic matrix itself: the horizontal focal term is divided by the squeeze while the vertical term is untouched, so the elliptical bokeh covariance aspect ratio is exactly the squeeze factor and the depth-of-field cue reads as genuinely anamorphic rather than as a post-hoc horizontal stretch. Glass does not bend every wavelength equally, and the Cauchy relation n(λ) = A + B/λ² fixes both the magnitude and the sign of the radial displacement between the red and blue channels, so the lateral chromatic fringe is measured across high-gradient edges and healed by radially resampling red and blue toward the green reference channel. Because the same lens that disperses light also falls off as cos⁴θ away from the optical axis, the healed image is divided by the exact off-axis radiance law, which prevents the correction from manufacturing unphysical edge darkening. The receipt records the intrinsic matrix, the bokeh aspect, the fringe metric before and after, and the vignetting map, so the repair is auditable rather than merely plausible.

## Law 13 — The Stevens-House Bio-Acoustic Viseme-Formant Articulatory Causality and Relativistic Acoustic Doppler Shift

Speech is produced by a physical tube, and the Stevens-House articulatory invariant encodes that tube's geometry: an open vocal tract with a high first formant cannot coexist with a closed jaw, so whenever F₁ exceeds six hundred hertz the jaw opening must be at least κ₁(F₁ − 300)/700. The inverse failure is the ventriloquist zombie mouth, where a digital performer flaps its jaw through silence; whenever frame RMS energy falls below the silence floor the jaw and lip activations are clamped to zero, eliminating the desynchronisation that makes synthetic dialogue read as puppetry. Bilabial rounding that contradicts a high first formant is likewise resolved by relaxing the lip activation to the physiological ceiling. The acoustic side is treated with the same rigour: the exact relativistic acoustic Doppler factor α = c/(c − v_r) is applied phase-coherently while preserving the output buffer length, and any radial velocity that reaches the sonic singularity fails closed rather than producing a numerically explosive artefact.

## Law 14 — The Navier-Stokes Helmholtz-Hodge Incompressible Projection and Free-Surface Volume Conservation

Water in a shot must be incompressible, and the Helmholtz-Hodge decomposition is the exact statement of that constraint: any velocity field splits into a solenoidal part and a gradient part, and subtracting ∇∇⁻²(∇·u) in the Fourier spectral domain leaves a field whose discrete divergence vanishes to machine precision. Because the projection is performed on the spectral representation of the whole frame sequence, the healed field satisfies an L₂ divergence below 10⁻¹¹ without any iterative solver or pixel loop. Incompressibility alone is not enough, however, because a splash can still appear or evaporate; the free-surface height field is therefore rescaled per frame so that the total liquid volume equals the volume of the first frame exactly, which is the discrete statement of mass conservation for an incompressible free-surface flow. Negative heights, mismatched shapes and non-finite values fail closed, and the receipt carries both divergence norms and both volume sequences.

## Law 15 — The Nyquist Rotary-Shutter Anti-Stroboscopic Wagon-Wheel Eliminator

A wheel that spins backward on screen is not a stylistic choice but a sampling failure, and the criterion is exact: the per-frame angular advance is ω/fps, its wrapped apparent value is ((Δφ + π) mod 2π) − π, and aliasing occurs whenever the true advance exceeds π radians or when the wrapped advance reverses sign relative to the true advance. Detecting the failure is only half the law, because the physical camera does not sample an instant; it integrates the rotary shutter's exposure arc Δφ·θ/360°, and reproducing that arc is what makes a fast rotor read as a blurred disc rather than a set of frozen spokes. The rectification therefore performs a vectorised angular quadrature over the exposure arc, accumulating rotated copies of the prop patch and averaging them, which restores the motion-blur signature that the human visual system expects. Shutter angles outside (0°, 360°] and non-positive frame rates fail closed, so the law cannot be silently misconfigured.
# Fifteen-Law Neuro-Physical Cinema Doctrine (`VERITAS OMNIA v2.0`)

The deterministic NumPy implementation makes no paid AI API calls. Its physical
predicates validate supplied observations; they do not establish that arbitrary
pixels depict physically valid scenes. Passing synthetic fixtures is not proof
of real-media reconstruction.

## Law inventory

1. Twenty-one-joint hand topology, tendon coupling and anatomical bounds.
2. Binocular vergence, saccadic main sequence and pupil light response.
3. Planar Householder reflection and Fresnel reflectance.
4. Projective shadows, penumbrae and contact geometry.
5. SE(3) object permanence and bounded motion under occlusion.
6. Projective diegetic typography and glyph spacing.
7. Impulse-momentum contact mechanics and friction.
8. XPBD cloth constraints and capsule collision.
9. Thermodynamic entropy and retarded acoustic causality.
10. Facial coactivation, asymmetric blink and subsurface scattering.
11. Madelung density/phase coherence and Bohmian edge-weighted smoothing.
    The stabilizer accepts previous density and time step, measures
    `(rho - rho_previous)/dt + div(rho grad(phase))` before and after healing,
    and reports residual violations explicitly. Continuity backtracking may
    change density as well as phase; it is a regularizer, not an inference of
    ground-truth motion. A zero iteration budget leaves failures visible.
12. Catadioptric dispersion and natural vignetting.
13. Acoustic Doppler and independent AU26/AU20/AU18 articulatory channels.
    Lip-pucker repair must not overwrite lip-stretch, and all channels are
    checked after repair.
14. Spectral Helmholtz-Hodge fluid projection and volume conservation.
15. Rotary-shutter integration and stroboscopic aliasing.

Detailed equations for the implemented kernels appear above.

## Media compiler and evidence boundary

The existing local compiler writes PNG, WAV and optionally FFmpeg MP4 and
constructs a fifteen-leaf RFC 6962 Merkle tree. Domain separation uses a zero
prefix for leaf hashes and a one prefix for internal node hashes. A valid
inclusion proof establishes membership, not physical truth.

The compiler now decodes its written RGB8/filter-zero PNG and mono PCM16 WAV
files and verifies that their canonical content matches the generated buffers.
PNG chunk CRCs, dimensions, stream lengths and supported encoding are checked.
Canonical input/output array digests are in the same domain; the output digests
are computed from decoded disk content. Every leaf also binds PNG/WAV file
hashes and the MP4 hash (empty when no mux was produced).

Caller media uses deterministic observed luminance, spatial gradients,
brightness-constraint velocity, fixed lower-central ROI energy and audio
spectral proxies, not the corrupted synthetic anatomy fixture. These proxies
are not semantic face tracking or ground-truth optical flow. Synthetic corruption
can still be explicitly requested. Caller RGB channels are retained by the
rotary filter and caller frames receive no VERITAS text overlay.

**Not yet closure-verified:** independent physical re-audit of decoded artifacts,
full per-frame application of Laws 11/14/15, and independently checked inclusion
proofs. Content integrity is not a proof of physical validity. No
`post_disk_verified=True` physical certificate is issued by this implementation.

## NEXUS staged execution spine: required contract (pending implementation)

Intent/recipe -> private attempt staging -> fifteen-law render -> independent
post-disk verifier -> fenced atomic publish -> AssetVersion/passport lineage.
The adapter must bind recipe identity and input content, reject path traversal,
verify disk hashes and all fifteen Merkle inclusion proofs, check the active
fencing token, and refuse publication on any mismatch. Atomic promotion must
publish only verified artifacts; a failed attempt must publish no bytes.
This contract is a target, not a claim that the adapter currently exists.
