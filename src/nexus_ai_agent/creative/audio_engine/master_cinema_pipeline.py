"""Deterministic in-memory six-stage cinema DAG with verified Merkle evidence.

PCM convention is mono dialogue/foley and channel-first mono/stereo music.
No files, global RNG, registry mutations or external services are involved.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np

from . import ambisonics_binaural_fdn as ambi
from . import av_subframe_foley_sync as sync
from . import cinema_kernels as kernels
from . import forensic_qc as qc
from . import loudness_bs1770 as loudness
from . import psychoacoustic_bark_mastering as bark
from . import spectral_voice_restoration as restoration
from . import spine_lineage as lineage
from . import stereo_hpss as hpss
from . import wiener_denoise as wiener


@dataclass(frozen=True)
class MasterCinemaResult:
    samples: np.ndarray
    lufs: float | None
    true_peak_dbtp: float | None
    status: str
    root: bytes
    stage_hashes: tuple[tuple[str, str], ...]
    proofs: tuple[tuple[bytes, ...], ...]


@dataclass(frozen=True)
class MasterCinemaAudioPipeline:
    sample_rate: int = 48000
    room_dims_m: tuple[float, float, float] = (6.0, 4.5, 2.8)
    absorption: float = 0.25

    def __post_init__(self) -> None:
        kernels._require_finite_scalar(self.sample_rate, name="sample_rate")
        kernels._require_finite_array(self.room_dims_m, name="room_dims_m")
        if not isinstance(self.sample_rate, int) or not 22050 <= self.sample_rate <= 192000:
            raise ValueError("sample_rate must be an integer in [22050, 192000]")
        if len(self.room_dims_m) != 3 or min(self.room_dims_m) <= 0:
            raise ValueError("room dimensions must be positive")
        kernels.eyring_t60(1.0, 1.0, self.absorption)

    def run(
        self,
        dialogue: np.ndarray,
        music: np.ndarray,
        foley: np.ndarray,
        visual_motion: np.ndarray,
        *,
        frame_rate: float = 24.0,
    ) -> MasterCinemaResult:
        sr = self.sample_rate
        stages: list[tuple[str, str]] = []

        def record(name: str, value: np.ndarray) -> None:
            kernels._require_finite_array(value, name=name)
            data = np.ascontiguousarray(value, dtype="<f8")
            digest = hashlib.sha256(str(data.shape).encode() + data.tobytes()).hexdigest()
            stages.append((name, digest))

        for name, data in (
            ("dialogue", dialogue),
            ("music", music),
            ("foley", foley),
            ("visual_motion", visual_motion),
        ):
            record(name, data)
        if dialogue.ndim != 1 or foley.ndim != 1 or dialogue.size < sr // 2:
            raise ValueError("dialogue/foley must be mono; at least half a second required")
        if foley.size != dialogue.size or music.shape[-1] != dialogue.size:
            raise ValueError("stems must have equal frame counts")
        restored = restoration.mmse_lsa_denoise(dialogue, sample_rate=sr).samples
        record("restored", restored)
        denoised = wiener.wiener_denoise(restored, sample_rate=sr).samples
        record("wiener", denoised)
        if music.ndim not in (1, 2) or (music.ndim == 2 and music.shape[0] != 2):
            raise ValueError("music must be mono or channel-first stereo")
        separated_channels = [
            hpss.hpss_separate(channel, sample_rate=sr)
            for channel in ((music,) if music.ndim == 1 else music)
        ]
        for separated in separated_channels:
            if separated.mask_sum_error > 1e-12 or separated.reconstruction_error > 1e-8:
                raise ValueError("HPSS partition/reconstruction failed")
        harmonic = np.vstack([item.harmonic for item in separated_channels])
        percussive = np.vstack([item.percussive for item in separated_channels])
        record("harmonic", harmonic)
        record("percussive", percussive)
        aligned = sync.synchronize_audio_to_visual(
            foley,
            visual_motion,
            sample_rate=sr,
            frame_rate=frame_rate,
        ).samples
        record("aligned", aligned)
        dims = self.room_dims_m
        volume = float(np.prod(dims))
        surface = 2 * (dims[0] * dims[1] + dims[1] * dims[2] + dims[0] * dims[2])
        rt = kernels.eyring_t60(volume, surface, self.absorption)
        # HOA3 direct field plus contractive binaural late-field FDN.
        encoded = ambi.encode_ambisonics(aligned, azimuth=0.0, order=3)
        record("hoa3", encoded)
        direct = ambi.binaural_decode(encoded, sample_rate=sr)
        room = ambi.FdnRoom(
            volume, surface, max(0.05, rt), max(0.05, rt * 0.8), max(0.05, rt * 0.6)
        )
        wet = ambi.fdn_reverb(aligned, sample_rate=sr, room=room).samples
        spatial = direct + 0.2 * wet[:, : direct.shape[-1]]
        record("binaural_room", spatial)
        music_sum = harmonic + percussive
        if music_sum.shape[0] == 1:
            music_sum = np.vstack((music_sum[0], music_sum[0]))
        master = bark.master_dialogue_music_sfx(
            np.vstack((denoised, denoised)),
            music_sum,
            spatial,
            sample_rate=sr,
        ).samples
        record("master", master)
        measurement = loudness.measure_bs1770_numpy(master, sample_rate=sr)
        peak = loudness.true_peak_dbtp(master)
        report = qc.analyze_forensic_audio_qc(master, sample_rate=sr)
        if report.status == "VIOLATED":
            raise ValueError(f"forensic QC VIOLATED: {report.codes}")
        parameters = json.dumps(
            {
                "sample_rate": sr,
                "room": dims,
                "absorption": self.absorption,
                "frame_rate": frame_rate,
            },
            sort_keys=True,
            allow_nan=False,
        ).encode()
        stages.append(("parameters", hashlib.sha256(parameters).hexdigest()))
        payloads = [json.dumps(item, separators=(",", ":")).encode() for item in stages]
        count = len(payloads)
        leaves = [lineage.merkle_leaf_hash(data, total_leaves=count) for data in payloads]
        root = lineage.merkle_root(leaves, strict=True)
        proofs = tuple(lineage.merkle_inclusion_proof(leaves, i) for i in range(count))
        for i, proof in enumerate(proofs):
            if not lineage.verify_merkle_inclusion(
                leaf=payloads[i],
                index=i,
                total_leaves=count,
                proof=proof,
                root=root,
                bind_tree_size=True,
            ):
                raise ValueError("lineage inclusion verification failed")
        master.setflags(write=False)
        return MasterCinemaResult(
            master, measurement.lufs, peak, report.status, root, tuple(stages), proofs
        )


def run_master_cinema_audio_pipeline(
    dialogue: np.ndarray,
    music: np.ndarray,
    foley: np.ndarray,
    visual_motion: np.ndarray,
    *,
    sample_rate: int = 48000,
    frame_rate: float = 24.0,
) -> MasterCinemaResult:
    return MasterCinemaAudioPipeline(sample_rate=sample_rate).run(
        dialogue,
        music,
        foley,
        visual_motion,
        frame_rate=frame_rate,
    )
