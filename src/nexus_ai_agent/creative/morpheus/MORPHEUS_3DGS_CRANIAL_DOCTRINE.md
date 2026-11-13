# MORPHEUS 3DGS CRANIAL DOCTRINE (Waves 1–8)

**NAGAR Creative OS — Deterministic Character Continuity**

Branch: `arena/morpheus-3dgs-cranial-rig`
Package: `src/nexus_ai_agent/creative/morpheus/`
Budget: **$0** external paid AI APIs.

## Waves 1–4 (Foundation)
- Orthogonalized 3DMM: `||U_id^T U_exp_tilde||_F < 1e-13`
- Umeyama rigid anchor `det(R)=+1`
- Dual-Quaternion Skinning: 175° twist radius error ≈ 0; LBS collapse > 85%
- EWA anisotropic 3DGS + Cook-Torrance GGX + Order-2 SH
- Staged spine + RFC 6962 Merkle (`0x00` leaf / `0x01` node)

## Wave 5 — Corotational FEM Soft Tissue
- Deformation gradient `F = Ds Dm^{-1}`, polar `F = R S`, corotational strain `S - I`
- Neo-Hookean PK1 with near-incompressible Lame (ν ∈ [0.45, 0.495])
- SVD singular-value clamping projects inverted tets onto `J ∈ [0.95, 1.05]`
- Kelvin-Voigt / Rayleigh damped Newmark; passive energy dissipation under zero load

## Wave 6 — Cosserat Hair + Marschner BRDF
- Discrete Cosserat rods, Bishop parallel transport, twist angles
- Inextensibility residual `< 1e-12`, scalp SDF push-out (0 penetrations)
- Marschner R / TT / TRT lobes with cuticle tilt α → longitudinal peak separation

## Wave 7 — Thin-Film Iridescence + Charlie Cloth
- Airy multi-beam interference at 650/532/460 nm
- Estevez-Kulla Charlie inverted-Gaussian microfiber sheen
- Material kinds: `standard_ggx | marschner_hair | charlie_cloth | thin_film_iridescent`

## Wave 8 — Episodic Character Capsule Vault
- Content-addressed JCS capsule (3DMM, bone lengths, tet volume, strand lengths, melanin, 3DGS)
- Cross-episode verifier: cranial / bone / strand / volume drift `< 1e-12`
- Merkle-sealed passport

## Verification
90 Popperian probes (P01–P90) + 80/80 mutation kills.
