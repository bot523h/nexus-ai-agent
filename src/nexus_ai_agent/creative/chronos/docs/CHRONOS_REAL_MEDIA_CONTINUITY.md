# Chronos Real-Media Continuity in the NEXUS Execution Spine

## Purpose and evidence boundary

Wave 17 connects the existing Chronos continuity renderers to a local, fenced
NEXUS publication lifecycle without adding image, audio, acoustic, or motion
physics. It composes the existing real-media persistence, multicamera mastering,
RFC 6962 Merkle, and CIEDE2000 implementations. The evidence is for the supplied
or reproducibly generated buffers and the files read back from the measured
workspace; it is not a claim about perceptual quality or production footage in
general.

The only supported engine names are `chronos.real_media_continuity` and
`chronos.omniverse_mastering`. The spine bridge registers
`chronos.staged_spine_master`. The latter selects the existing multicamera
mastering pipeline; it does not invoke an external Omniverse service or enable
egress.

## Existing NEXUS boundaries reused

- `WorkspaceFilesystem` is the filesystem authority. It provides descriptor-
  relative, no-follow reads, writes, hashing, directory creation, quarantine,
  and same-workspace atomic rename. The adapter uses the requested private
  layout `<workspace>/.staging/<attempt_id>_<fencing_token>/` and atomically
  renames that complete directory to `published/<project>/<revision>/<version>`.
- `ExecutionIdentity` validates the local attempt identity. An injected
  `active_fencing_token(project_id, attempt_id)` reader remains the authority;
  the adapter never manufactures or stores a fence. It checks the token before
  rendering and again at the publish boundary.
- `AttemptStaging` was audited but not used for this route: its public layout is
  job/attempt nested and its publish verb promotes an individual regular file.
  Chronos needs the existing `.staging/<attempt>_<fence>` contract and one
  atomic promotion of the complete verified directory. The adapter therefore
  composes `WorkspaceFilesystem`'s existing descriptor-relative directory,
  no-follow, rename, and quarantine operations; it adds no alternate filesystem
  boundary.
- The adapter does not create a second filesystem-policy layer, job queue,
  SQL database, or causal journal. If a canonical `CommandBus` is injected, the
  adapter first dispatches the confirmed, local-only
  `chronos.staged_spine_master` operation with actor/project authorization and
  current revision/hash preconditions; a denial occurs before any stage exists.
  `ChronosAssetVersionRecord` and `ChronosArtifactPassport` are immutable
  measured lineage projections persisted together through the injected
  `ChronosAssetVersionStore`/`ArtifactPassportSink` transaction contract. The
  default `FileChronosLedger` stores both in one atomically replaced JCS ledger
  under `.chronos-ledger/`; `InMemoryChronosLedger` is provided for isolated
  tests. A real repository adapter can implement the same pair-commit contract.
  This execution ledger does not impersonate the SQLModel Asset Library row or
  the causal-journal-backed `provenance.passport.ArtifactPassport`; those
  authorities can be connected through the injected transactional sink.

## Recipe identity

`ChronosRecipeIdentity.recipe_digest` is SHA-256 over RFC 8785 JCS UTF-8 bytes.
The object binds project and parent revision, attempt and fencing token, ordered
frame/landmark/PCM buffer digests, operation name, normalized render parameters,
calibration inputs, room and camera controls, and engine version
`chronos-omniverse-1.0`. The local JCS serializer rejects non-finite values,
non-string object keys, lone surrogates, and integers outside the interoperable
safe range. Buffer hashes normalize numeric arrays to contiguous little-endian
float64; shape and calibration values are also bound into the normalized recipe
parameters.

## Stage, render, verify, publish

1. **Private stage.** Project and attempt identifiers are conservative single
   path components; the parent revision is checked as a non-path identity; the
   fencing token must be a positive, safe integer. Existing attempt stage paths
   are refused rather than reused. All render output is confined below the
   private attempt directory.
2. **Render.** The real-media engine writes PNG frames, mono/stereo 48 kHz WAV,
   an RFC 6962 continuity manifest, and optional FFV1/PCM NUT muxes. When
   `render_mp4=true`, FFmpeg additionally writes an MP4 per shot and the
   manifest's RFC 6962 root and inclusion proofs are recomputed to bind those
   bytes. Omniverse mastering composes the existing calibrated camera,
   color/focus, rolling-shutter, acoustic, and kinetic-cut pipeline before it
   enters the same persistence path.
3. **Independent post-disk verifier.** The verifier reads the manifest, spine
   receipt, PNGs, WAVs, and any muxes back from disk. It rehashes every expected
   file; decodes PNG pixels and PCM; checks WAV channel count, rate, sample
   frames, and exact rational 48 kHz horizon; measures the calibrated patch's
   worst-pixel CIEDE2000 from decoded frame bytes; recomputes the RFC 6962 root
   from disk receipts and verifies every proof; and independently reruns the
   existing epipolar and acoustic continuity audits for the mastering route.
   A missing, empty, unexpected, unsafe, tampered, or malformed artifact is a
   typed refusal. Failed attempts are moved into the private quarantine tree;
   no published destination is created or changed.
4. **Fenced atomic publish.** After verification, the active token is read again
   immediately before publication. A stale attempt is quarantined. The whole
   verified attempt directory is promoted with one same-filesystem atomic
   directory rename; an existing immutable asset-version destination is never
   overwritten.
5. **Lineage.** The returned immutable AssetVersion record and artifact
   passport bind project/parent/produced revision, attempt/fence, recipe, Merkle
   root, input and output content digests, per-file SHA-256s, `REAL-DISK` tier,
   UTC verification time, sample drift, maximum ΔE00, epipolar residual, and
   RT60 spread. The passport digest is a JCS/SHA-256 digest over the record's
   evidence fields (excluding its own digest and local absolute path).

## Verification limits

The active-fence callback is an injected interface to the caller's actual
NEXUS authority. This local adapter cannot make a filesystem rename and a
separate database transaction one atomic transaction; queue completion and
canonical Asset Library/provenance persistence must still be performed by their
own authorities. The returned lineage values are not a substitute for the
existing causal journal-backed `ArtifactPassport`. Optional MP4 uses FFmpeg's
`mpeg4` video and AAC audio mux; the exact sample-clock guarantee is checked
against the separately persisted WAV, not codec-decoded MP4 audio.
