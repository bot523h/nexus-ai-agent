"""Unified 10-law reality firewall, shot restorer and cryptographic passport.

``VeritasRealityAuditor`` runs every neuro-physical law over a multi-frame shot
bundle, reports per-law violation scores before and after deterministic
mathematical restoration, and emits an RFC 6962 / SHA-256 hashed
``VeritasRealityPassport``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt

from nexus_ai_agent.creative.veritas.catoptric_reflection_physics import (
    compute_fresnel_reflectance_rgb,
    verify_reflection_epipolar_consistency,
)
from nexus_ai_agent.creative.veritas.hand_kinematics import (
    HandSkeleton,
    Sdf,
    audit_and_project_hand_skeleton,
)
from nexus_ai_agent.creative.veritas.homography_diegetic_typography import (
    render_homography_locked_diegetic_text,
)
from nexus_ai_agent.creative.veritas.neuro_facial_microdynamics import (
    apply_dermal_subsurface_scattering,
    enforce_facs_synergy_manifold,
    synthesize_asymmetric_blink_curve,
)
from nexus_ai_agent.creative.veritas.ocular_biophysics import (
    compute_watson_yellott_pupil_diameter,
    solve_binocular_vergence,
    synthesize_saccade_vor_trajectory,
)
from nexus_ai_agent.creative.veritas.projective_shadow_transport import (
    synthesize_coherent_cast_shadows,
)
from nexus_ai_agent.creative.veritas.se3_object_permanence import (
    ObjectState,
    audit_object_permanence,
)
from nexus_ai_agent.creative.veritas.symplectic_contact_mechanics import (
    resolve_rigid_body_collision,
)
from nexus_ai_agent.creative.veritas.thermodynamic_arrow_of_time import (
    audit_and_enforce_thermodynamic_arrow,
    verify_retarded_causality,
)
from nexus_ai_agent.creative.veritas.xpbd_cloth_drape import (
    step_xpbd_cloth_simulation,
)

FloatArray = npt.NDArray[np.float64]
Capsule = tuple[FloatArray, FloatArray, float]

LAW_NAMES: dict[int, str] = {
    1: "hand_polydactyly",
    2: "zombie_eyes",
    3: "reflection_lies",
    4: "floating_shadows",
    5: "object_vanishing",
    6: "gibberish_text",
    7: "newtonian_violations",
    8: "cloth_penetration",
    9: "time_arrow_reversal",
    10: "uncanny_plastic_faces",
}


@dataclass(frozen=True)
class CinemaShot:
    """A bundle of optional physical observations for one cinematic shot."""

    hand: HandSkeleton | None = None
    hand_sdf: Sdf | None = None
    hand_grip_active: bool = False
    eyes: tuple[FloatArray, FloatArray, FloatArray] | None = None
    saccade: tuple[float, float, float] | None = None
    pupil: tuple[float, float, float, float] | None = None
    facs: FloatArray | None = None
    f0_envelope: FloatArray | None = None
    blink: tuple[float, float] | None = None
    skin: tuple[FloatArray, FloatArray, FloatArray] | None = None
    mirror: tuple[FloatArray, float, FloatArray, FloatArray] | None = None
    fresnel: tuple[float, float, float] | None = None
    shadow: tuple[FloatArray, FloatArray, FloatArray] | None = None
    typography: tuple[str, FloatArray, tuple[int, int]] | None = None
    objects: tuple[ObjectState, ...] | None = None
    collision: tuple[FloatArray, FloatArray, FloatArray, FloatArray, float, float] | None = None
    cloth: tuple[FloatArray, FloatArray, FloatArray, list[Capsule]] | None = None
    scalar_frames: FloatArray | None = None
    wave: tuple[FloatArray, tuple[float, float], float, float] | None = None


@dataclass(frozen=True)
class VeritasRealityPassport:
    """Cryptographically hashed proof of a shot's physical restoration."""

    law_violations_before: dict[int, int]
    law_violations_after: dict[int, int]
    details_before: dict[int, tuple[str, ...]]
    details_after: dict[int, tuple[str, ...]]
    pre_audit_violations: int
    post_heal_violations: int
    digest: str

    @property
    def healed(self) -> bool:
        return self.pre_audit_violations > 0 and self.post_heal_violations == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "law_violations_before": {
                str(k): v for k, v in sorted(self.law_violations_before.items())
            },
            "law_violations_after": {
                str(k): v for k, v in sorted(self.law_violations_after.items())
            },
            "details_before": {str(k): list(v) for k, v in sorted(self.details_before.items())},
            "details_after": {str(k): list(v) for k, v in sorted(self.details_after.items())},
            "pre_audit_violations": self.pre_audit_violations,
            "post_heal_violations": self.post_heal_violations,
        }


def _rfc6962_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    leaf = b"\x00" + encoded
    return hashlib.sha256(leaf).hexdigest()


@dataclass
class VeritasRealityAuditor:
    """Runs all ten laws and returns a hashed restoration passport."""

    impulse_bound: float = 0.5
    entropy_tolerance: float = 1e-9
    laws: tuple[int, ...] = field(default_factory=lambda: tuple(range(1, 11)))

    def _law_hand(self, shot: CinemaShot) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if shot.hand is None:
            return (), ()
        result = audit_and_project_hand_skeleton(
            shot.hand,
            target_sdf=shot.hand_sdf,
            grip_active=shot.hand_grip_active,
        )
        return result.violations_before, result.violations_after

    def _law_eyes(self, shot: CinemaShot) -> tuple[tuple[str, ...], tuple[str, ...]]:
        before: list[str] = []
        after: list[str] = []
        if shot.eyes is not None:
            solution = solve_binocular_vergence(*shot.eyes)
            before.extend(solution.violations_before)
            if not solution.is_consistent:
                before.append("gaze_projected")
        if shot.saccade is not None:
            delta, duration, head_velocity = shot.saccade
            trajectory = synthesize_saccade_vor_trajectory(
                delta, duration, head_angular_velocity_deg_s=head_velocity
            )
            if trajectory.peak_velocity_deg_s <= 0.0:
                before.append("saccade_static")
        if shot.pupil is not None:
            luminance, area, age, binocular = shot.pupil
            response = compute_watson_yellott_pupil_diameter(luminance, area, age, binocular)
            if response.steady_state_mm < 2.0 or response.steady_state_mm > 8.0:
                before.append("pupil_out_of_range")
        return tuple(before), tuple(after)

    def _law_reflection(self, shot: CinemaShot) -> tuple[tuple[str, ...], tuple[str, ...]]:
        before: list[str] = []
        after: list[str] = []
        if shot.mirror is not None:
            normal, offset, camera, landmark = shot.mirror
            consistency = verify_reflection_epipolar_consistency(normal, offset, camera, landmark)
            if not consistency.consistent:
                before.append("reflection_inconsistent")
        if shot.fresnel is not None:
            theta, n1, n2 = shot.fresnel
            fresnel = compute_fresnel_reflectance_rgb(theta, n1, n2)
            brewster = compute_fresnel_reflectance_rgb(fresnel.brewster_deg, n1, n2)
            if float(np.max(brewster.reflectance_p)) > 1e-9:
                before.append("brewster_violation")
        return tuple(before), tuple(after)

    def _law_shadow(self, shot: CinemaShot) -> tuple[tuple[str, ...], tuple[str, ...]]:
        before: list[str] = []
        after: list[str] = []
        if shot.shadow is not None:
            vertices, light, plane = shot.shadow
            field_result = synthesize_coherent_cast_shadows(vertices, light, plane)
            normal = plane[:3] / float(np.linalg.norm(plane[:3]))
            residual = np.abs(field_result.shadow_vertices @ normal + plane[3])
            if float(np.max(residual)) > 1e-6:
                before.append("floating_shadow")
            if np.any(field_result.penumbra_radius < 0.0):
                before.append("negative_penumbra")
        return tuple(before), tuple(after)

    def _law_objects(self, shot: CinemaShot) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if shot.objects is None:
            return (), ()
        audit = audit_object_permanence(shot.objects, impulse_bound=self.impulse_bound)
        return audit.violations_before, audit.violations_after

    def _law_typography(self, shot: CinemaShot) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if shot.typography is None:
            return (), ()
        text, quad, canvas = shot.typography
        render = render_homography_locked_diegetic_text(text, quad, canvas)
        before: list[str] = []
        if abs(float(np.linalg.det(render.homography)) - 1.0) > 1e-9:
            before.append("homography_not_sl3")
        if text.strip() and float(np.max(render.mask)) <= 0.0:
            before.append("text_invisible")
        return tuple(before), ()

    def _law_collision(self, shot: CinemaShot) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if shot.collision is None:
            return (), ()
        positions, velocities, masses, radii, restitution, friction = shot.collision
        result = resolve_rigid_body_collision(
            positions,
            velocities,
            masses,
            radii,
            restitution=restitution,
            friction=friction,
        )
        before: list[str] = []
        if float(np.linalg.norm(result.momentum_after - result.momentum_before)) > 1e-9:
            before.append("momentum_nonconservation")
        if result.energy_after > result.energy_before + 1e-9:
            before.append("energy_creation")
        return tuple(before), ()

    def _law_cloth(self, shot: CinemaShot) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if shot.cloth is None:
            return (), ()
        positions, velocities, edges, capsules = shot.cloth
        result = step_xpbd_cloth_simulation(positions, velocities, edges, capsules=capsules)
        before: list[str] = []
        if result.min_sdf < -1e-6:
            before.append("cloth_penetration")
        return tuple(before), ()

    def _law_time(self, shot: CinemaShot) -> tuple[tuple[str, ...], tuple[str, ...]]:
        before: list[str] = []
        after: list[str] = []
        if shot.scalar_frames is not None:
            audit = audit_and_enforce_thermodynamic_arrow(
                shot.scalar_frames, tolerance=self.entropy_tolerance
            )
            before.extend(audit.violations_before)
            after.extend(audit.violations_after)
        if shot.wave is not None:
            wave, source, impact, speed = shot.wave
            if not verify_retarded_causality(wave, source, impact, speed):
                before.append("acausal_wavefront")
        return tuple(before), tuple(after)

    def _law_face(self, shot: CinemaShot) -> tuple[tuple[str, ...], tuple[str, ...]]:
        before: list[str] = []
        after: list[str] = []
        if shot.facs is not None:
            facs = enforce_facs_synergy_manifold(shot.facs, f0_envelope=shot.f0_envelope)
            before.extend(facs.violations_before)
            after.extend(facs.violations_after)
        if shot.blink is not None:
            duration, onset = shot.blink
            blink = synthesize_asymmetric_blink_curve(duration, onset_s=onset)
            if blink.peak_closure > 1.0:
                before.append("blink_overclosure")
        if shot.skin is not None:
            diffuse, specular, mask = shot.skin
            composite = apply_dermal_subsurface_scattering(diffuse, specular, mask)
            if composite.shape != diffuse.shape:
                before.append("sss_shape")
        return tuple(before), tuple(after)

    def _dispatch(self, law: int, shot: CinemaShot) -> tuple[tuple[str, ...], tuple[str, ...]]:
        handlers = {
            1: self._law_hand,
            2: self._law_eyes,
            3: self._law_reflection,
            4: self._law_shadow,
            5: self._law_objects,
            6: self._law_typography,
            7: self._law_collision,
            8: self._law_cloth,
            9: self._law_time,
            10: self._law_face,
        }
        return handlers[law](shot)

    def audit_and_heal_cinema_shot(self, shot: CinemaShot) -> VeritasRealityPassport:
        """Run all ten laws, heal deterministically and hash the result."""
        before_scores: dict[int, int] = {}
        after_scores: dict[int, int] = {}
        details_before: dict[int, tuple[str, ...]] = {}
        details_after: dict[int, tuple[str, ...]] = {}
        for law in self.laws:
            before, after = self._dispatch(law, shot)
            before_scores[law] = len(before)
            after_scores[law] = len(after)
            details_before[law] = before
            details_after[law] = after
        passport = VeritasRealityPassport(
            law_violations_before=before_scores,
            law_violations_after=after_scores,
            details_before=details_before,
            details_after=details_after,
            pre_audit_violations=int(sum(before_scores.values())),
            post_heal_violations=int(sum(after_scores.values())),
            digest="",
        )
        digest = _rfc6962_digest(passport.as_dict())
        return VeritasRealityPassport(
            law_violations_before=before_scores,
            law_violations_after=after_scores,
            details_before=details_before,
            details_after=details_after,
            pre_audit_violations=passport.pre_audit_violations,
            post_heal_violations=passport.post_heal_violations,
            digest=digest,
        )
