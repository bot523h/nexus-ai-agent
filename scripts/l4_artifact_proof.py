#!/usr/bin/env python3
"""Artifact-proof harness — the first real vertical slice (task-191).

REAL INPUT → DISCOVERY → INTENT → TYPED COMMAND → COMMAND BUS → REAL RENDER →
REAL ARTIFACT → INDEPENDENT VERIFICATION → HASH → REOPEN → PROVENANCE.

Everything on the execution path is the shipped code: the surface intent is
mapped by ``creative.render_jobs.SURFACE_TO_CANONICAL``, executed by the
production worker adapter ``creative_render_job`` (packs runtime registry →
``CommandBus.dispatch`` → Lane IR → ``render_lane`` → FFmpeg), and re-verified
by the queue's own verifier ``jobs.creative_verification.creative_render_verifier``.
Nothing is mocked.  The only instrumentation is a pass-through recorder on
``CommandBus.dispatch`` that keeps the *real* command, result and bus instance
the worker used (the worker's result dict does not carry the command id,
transaction id or state revision — a provenance gap this harness records
instead of hiding), so undo can be proven on the exact bus that executed.

Verification is independent of the producer in three ways:

1. **bytes** — ``hashlib`` over the file, compared with the claimed sha256;
2. **container** — a pure-Python ISO-BMFF box walk (no FFmpeg): ``ftyp`` brand,
   ``moov``/``mdat`` present, movie duration, per-track handler and size;
3. **decode** — a full decode of every video frame (``-f framemd5``) with the
   allow-listed binary: exit 0, frame count, per-frame digests.

Then the artifact is **reopened** (fresh handles; re-hash, re-parse, re-decode,
``probe_video``) and must match the first pass bit-for-bit.

What this harness deliberately does NOT claim:

* **Master** — ``delivery.render_master_4k`` writes no file; its record's
  ``content_sha256`` hashes a spec string.  The harness proves that absence.
* **L4** — the canonical ladder (PR#83 ``nagar/truth.py::LADDER``) needs a
  production-like measurement no source defines; a sandbox/CI run is not one.
  The emitted ``proof_registry_candidate`` is ``kind="artifact_evidence"``
  only, for PR#83's registry once it is on main.

Usage::

    python scripts/l4_artifact_proof.py [--workdir DIR] [--out proof.json]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "nagar.artifact_proof.v1"

#: Deterministic fixture: 3 s, 640x360 @ 25 fps test pattern + 440 Hz sine.
FIXTURE_SECONDS = 3
FIXTURE_SIZE = (640, 360)
FIXTURE_FPS = 25

#: The slice's intent: trim [0.5 s, 2.5 s) — expected output 2.0 s.  Output
#: size/fps are NOT the source's: the render lane normalises every artifact to
#: its ``LaneProfile`` (``creative/rendering/ir.py``; default 1280x720 @ 30), so
#: expectations are read from that shipped contract, never assumed.
TRIM_IN_S = 0.5
TRIM_OUT_S = 2.5
EXPECTED_SECONDS = TRIM_OUT_S - TRIM_IN_S
#: One frame of slack plus AAC priming on the container duration.
DURATION_TOLERANCE_S = 0.12
FRAME_TOLERANCE = 1


#: ``delivery.make_proxy_480p`` lane profile (``render_jobs._lane_ops``).
PROXY_PROFILE_SIZE = (854, 480)

_CONTAINER_BOXES = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf"}
_ISO_BRANDS = {"isom", "iso2", "iso4", "iso5", "iso6", "mp41", "mp42", "avc1", "M4V ", "qt  "}


# --------------------------------------------------------------------------
# Independent verifier 1: pure-Python ISO-BMFF walk (no FFmpeg involved)
# --------------------------------------------------------------------------


def _iter_boxes(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """Yield ``(type, payload_start, box_end)`` for the boxes in ``data[start:end]``."""
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos : pos + 8])
        header = 8
        if size == 1:
            if pos + 16 > end:
                raise ValueError("truncated 64-bit box header")
            size = struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise ValueError(f"box {kind!r} at {pos} overruns its parent ({size} bytes)")
        yield kind, pos + header, pos + size
        pos += size
    if pos != end:
        raise ValueError(f"{end - pos} trailing bytes do not form a box")


def parse_isobmff(path: Path) -> dict[str, Any]:
    """Structural facts of an MP4 read straight from its bytes."""
    data = path.read_bytes()
    top = [(kind.decode("latin-1"), s, e) for kind, s, e in _iter_boxes(data, 0, len(data))]
    kinds = [k for k, _, _ in top]
    if not kinds or kinds[0] != "ftyp":
        raise ValueError(f"first box is {kinds[:1]!r}, not 'ftyp' — not an ISO-BMFF file")
    _, fs, _ = top[0]
    major_brand = data[fs : fs + 4].decode("latin-1")
    facts: dict[str, Any] = {
        "top_level_boxes": kinds,
        "major_brand": major_brand,
        "movie_timescale": None,
        "movie_duration_s": None,
        "tracks": [],
    }
    moov = next(((s, e) for k, s, e in top if k == "moov"), None)
    if moov is None:
        raise ValueError("no 'moov' box — the file has no movie header")
    for kind, s, e in _iter_boxes(data, *moov):
        if kind == b"mvhd":
            version = data[s]
            if version == 1:
                timescale, duration = struct.unpack(">IQ", data[s + 20 : s + 32])
            else:
                timescale, duration = struct.unpack(">II", data[s + 12 : s + 20])
            facts["movie_timescale"] = timescale
            facts["movie_duration_s"] = duration / timescale if timescale else None
        elif kind == b"trak":
            facts["tracks"].append(_parse_trak(data, s, e))
    return facts


def _parse_trak(data: bytes, start: int, end: int) -> dict[str, Any]:
    track: dict[str, Any] = {"handler": None, "width": None, "height": None, "duration_s": None}
    stack = [(start, end)]
    while stack:
        s0, e0 = stack.pop()
        for kind, s, e in _iter_boxes(data, s0, e0):
            if kind == b"tkhd":
                version = data[s]
                # payload = version/flags(4) + times/ids/duration + reserved(8) +
                # layer/group/volume/reserved(8) + matrix(36) → width, height
                offset = s + (88 if version == 1 else 76)
                w, h = struct.unpack(">II", data[offset : offset + 8])
                track["width"], track["height"] = w >> 16, h >> 16
            elif kind == b"mdhd":
                version = data[s]
                if version == 1:
                    timescale, duration = struct.unpack(">IQ", data[s + 20 : s + 32])
                else:
                    timescale, duration = struct.unpack(">II", data[s + 12 : s + 20])
                track["duration_s"] = duration / timescale if timescale else None
            elif kind == b"hdlr":
                track["handler"] = data[s + 8 : s + 12].decode("latin-1")
            elif kind in _CONTAINER_BOXES:
                stack.append((s, e))
    return track


# --------------------------------------------------------------------------
# Independent verifier 2: full decode + bytes hash
# --------------------------------------------------------------------------


def sha256_bytes(path: Path) -> str:
    """Stdlib hash, deliberately not the runtime's ``sha256_file``."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def decode_video_frames(path: Path, binary: str) -> dict[str, Any]:
    """Decode EVERY video frame; a corrupt stream fails here, not in a header read."""
    proc = subprocess.run(  # noqa: S603 - argv list, allow-listed binary
        [binary, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path)]
        + ["-map", "0:v:0", "-f", "framemd5", "-"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    frames = [line for line in proc.stdout.splitlines() if line and not line.startswith("#")]
    return {
        "exit_code": proc.returncode,
        "stderr": proc.stderr.strip()[-400:],
        "video_frames": len(frames),
        "decoded_frames_digest": "sha256:"
        + hashlib.sha256(
            "\n".join(line.rsplit(",", 1)[-1].strip() for line in frames).encode()
        ).hexdigest(),
    }


# --------------------------------------------------------------------------
# The slice
# --------------------------------------------------------------------------


def make_fixture(path: Path, binary: str) -> None:
    """Deterministic real media (same binary + args ⇒ same bytes)."""
    w, h = FIXTURE_SIZE
    subprocess.run(  # noqa: S603 - argv list, allow-listed binary
        [binary, "-hide_banner", "-nostdin", "-loglevel", "error"]
        + [
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=duration={FIXTURE_SECONDS}:size={w}x{h}:rate={FIXTURE_FPS}",
        ]
        + ["-f", "lavfi", "-i", f"sine=frequency=440:duration={FIXTURE_SECONDS}"]
        + ["-pix_fmt", "yuv420p", "-threads", "1", "-map_metadata", "-1"]
        + ["-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact"]
        + ["-y", str(path)],
        check=True,
        capture_output=True,
        timeout=120,
    )


@dataclass
class _DispatchRecorder:
    """Pass-through recorder: the real bus runs; we only keep what it did."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    @contextlib.contextmanager
    def installed(self) -> Iterator[None]:
        from nexus_ai_agent.creative.studio.bus import CommandBus

        original = CommandBus.dispatch
        recorder = self

        def recording_dispatch(bus: Any, command: Any) -> Any:  # noqa: ANN401
            result = original(bus, command)
            recorder.calls.append({"bus": bus, "command": command, "result": result})
            return result

        CommandBus.dispatch = recording_dispatch  # type: ignore[method-assign]
        try:
            yield
        finally:
            CommandBus.dispatch = original  # type: ignore[method-assign]


@contextlib.contextmanager
def _creative_root(root: Path) -> Iterator[None]:
    from nexus_ai_agent.config import settings as settings_module

    previous = os.environ.get("CREATIVE_TEMP_DIR")
    os.environ["CREATIVE_TEMP_DIR"] = str(root)
    settings_module.get_settings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("CREATIVE_TEMP_DIR", None)
        else:
            os.environ["CREATIVE_TEMP_DIR"] = previous
        settings_module.get_settings.cache_clear()


def discovery_view(operation_id: str) -> dict[str, Any]:
    """The main-branch read view of the capability (Gate 2 ``describe``)."""
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
    from nexus_ai_agent.creative.studio.lifecycle import pack_lifecycle

    registry = build_runtime_registry()
    if operation_id not in registry.list_operations():
        raise AssertionError(f"{operation_id} is not discoverable in the runtime registry")
    d = registry.describe(operation_id)
    return {
        "operation_id": operation_id,
        "capability_id": d.capability_id,
        "capability_version": d.version,
        "available": d.available,
        "execution_modes": list(d.execution_modes),
        "required_packs": list(d.required_packs),
        "pack_states": {p: pack_lifecycle(p).state.value for p in d.required_packs},
        "operation_schema_version": d.operation_schema_version,
        "surface": "CapabilityRegistry.describe (main); nagar.discovery.v1 is PR#87, not on main",
    }


def verify_artifact_independently(
    path: Path, binary: str, *, claimed_sha256: str, expect: dict[str, Any]
) -> dict[str, Any]:
    from nexus_ai_agent.creative.slideshow.ffmpeg import probe_video

    checks: dict[str, bool] = {}
    exists = path.is_file()
    checks["exists"] = exists
    size = path.stat().st_size if exists else 0
    checks["non_zero"] = size > 0
    measured_sha = sha256_bytes(path) if exists else ""
    checks["sha256_matches_claim"] = measured_sha == claimed_sha256
    box = parse_isobmff(path)
    checks["iso_bmff_container"] = box["major_brand"] in _ISO_BRANDS and {"moov", "mdat"} <= set(
        box["top_level_boxes"]
    )
    handlers = sorted(t["handler"] for t in box["tracks"])
    checks["streams_video_and_audio"] = handlers == ["soun", "vide"]
    video_track = next(t for t in box["tracks"] if t["handler"] == "vide")
    checks["resolution"] = (video_track["width"], video_track["height"]) == tuple(expect["size"])
    duration = box["movie_duration_s"] or 0.0
    checks["duration"] = abs(duration - expect["seconds"]) <= DURATION_TOLERANCE_S
    decoded = decode_video_frames(path, binary)
    checks["full_decode_clean"] = decoded["exit_code"] == 0 and decoded["stderr"] == ""
    if expect.get("frames") is not None:
        checks["frame_count"] = abs(decoded["video_frames"] - expect["frames"]) <= FRAME_TOLERANCE
    probe = probe_video(path, binary=binary)
    checks["probe_agrees"] = (
        probe.width,
        probe.height,
        probe.has_audio,
    ) == (*expect["size"], True) and abs(probe.duration_us / 1e6 - duration) <= 0.05
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "size_bytes": size,
        "sha256": measured_sha,
        "container": box,
        "decode": decoded,
        "probe": {
            "duration_us": probe.duration_us,
            "width": probe.width,
            "height": probe.height,
            "has_audio": probe.has_audio,
        },
    }


def _render(
    workspace: Path, key: str, command: str, operation: str, args: list[str]
) -> dict[str, Any]:
    from nexus_ai_agent.creative.render_jobs import creative_render_job

    payload = {
        "command": command,
        "operation": operation,
        "args": args,
        "workspace_dir": str(workspace),
        "input_path": str(workspace / "input.mp4"),
        "user_id": 1,
        "chat_id": 1,
        "lang": "fa",
        "idempotency_key": key,
    }
    result = asyncio.run(creative_render_job(payload))
    return {"payload": payload, "result": result}


def master_reality_check() -> dict[str, Any]:
    """Dispatch ``delivery.render_master_4k`` for real and look for its artifact."""
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
    from nexus_ai_agent.creative.studio.bus import CommandBus
    from nexus_ai_agent.creative.studio.models import (
        AssetRecord,
        Timeline,
        TypedCommand,
        new_project,
    )

    project = new_project("master_probe", "master probe", Timeline(timeline_id="tl", duration_us=1))
    project = project.model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="src",
                    media_kind="video",
                    content_sha256="sha256:" + "a" * 64,
                    duration_us=1,
                    parent_asset_ids=(),
                    provenance={},
                )
            ]
        }
    )
    bus = CommandBus(state=project, registry=build_runtime_registry(), allow_experimental=True)
    result = bus.dispatch(
        TypedCommand(
            command_id="cmd_master_probe",
            operation="delivery.render_master_4k",
            input={"confirmed": True, "output_asset_id": "master_probe"},
            confirmed=True,
        )
    )
    record = next(a for a in bus.project.assets if a.asset_id == "master_probe")
    return {
        "operation": "delivery.render_master_4k",
        "dispatch_status": result.status,
        "record_claims_master": bool(record.provenance.get("is_master")),
        "record_claims_engine": record.provenance.get("render_engine"),
        "record_content_sha256": record.content_sha256,
        "output_keys": sorted(result.output),
        "artifact_path_in_output": any("path" in k for k in result.output),
        "verdict": "SIMULATED — state record only; no file is rendered, and content_sha256 "
        "hashes a render-spec string, not bytes",
    }


def run_slice(workdir: Path) -> dict[str, Any]:
    from nexus_ai_agent.creative.render_jobs import SURFACE_TO_CANONICAL
    from nexus_ai_agent.creative.rendering.ir import LaneProfile
    from nexus_ai_agent.creative.slideshow.ffmpeg import resolve_ffmpeg_bin
    from nexus_ai_agent.creative.studio.models import TypedCommand
    from nexus_ai_agent.jobs.creative_verification import creative_render_verifier

    binary = resolve_ffmpeg_bin()
    root = workdir / "creative_tmp"
    root.mkdir(parents=True, exist_ok=True)
    proof: dict[str, Any] = {"schema": SCHEMA, "ffmpeg_binary": Path(binary).name}

    with _creative_root(root):
        # 1. REAL INPUT (deterministic fixture, generated twice → same bytes?)
        workspace = root / "creative_l4proof"
        workspace.mkdir()
        fixture = workspace / "input.mp4"
        make_fixture(fixture, binary)
        twin = workdir / "fixture_twin.mp4"
        make_fixture(twin, binary)
        proof["input"] = {
            "sha256": sha256_bytes(fixture),
            "deterministic": sha256_bytes(fixture) == sha256_bytes(twin),
            "size_bytes": fixture.stat().st_size,
            "spec": f"testsrc2 {FIXTURE_SIZE[0]}x{FIXTURE_SIZE[1]}@{FIXTURE_FPS} + sine 440Hz, "
            f"{FIXTURE_SECONDS}s, bitexact",
        }

        # 2. INTENT (surface) → 3. DISCOVERY (runtime read view)
        intent = ("edit", "trim", [str(TRIM_IN_S), str(TRIM_OUT_S)])
        canonical = SURFACE_TO_CANONICAL[(intent[0], intent[1])]
        proof["intent"] = {"surface": f"/{intent[0]} {intent[1]} {' '.join(intent[2])}"}
        proof["discovery"] = discovery_view(canonical)

        # 4–8. TYPED COMMAND → COMMAND BUS → RUNTIME → RENDER → ARTIFACT
        recorder = _DispatchRecorder()
        with recorder.installed():
            run = _render(workspace, "l4proof", *intent)
        result = run["result"]
        if not result.get("success"):
            raise AssertionError(f"render failed: {result}")
        [call] = [c for c in recorder.calls if c["command"].operation == canonical]
        command, bus_result, bus = call["command"], call["result"], call["bus"]
        proof["command"] = {
            "command_id": command.command_id,
            "operation": command.operation,
            "protocol_version": command.protocol_version,
            "idempotency_key": command.idempotency_key,
            "execution_mode": command.execution_policy.mode,
            "input": command.input,
        }
        proof["bus_result"] = {
            "transaction_id": bus_result.transaction_id,
            "state_revision": bus_result.state_revision,
            "state_hash": bus_result.state_hash,
            "status": bus_result.status,
            "undo_available": bus_result.undo_available,
        }

        artifact = Path(result["artifact_path"])
        lane = LaneProfile()
        expect = {
            "size": (lane.width, lane.height),
            "seconds": EXPECTED_SECONDS,
            "frames": round(EXPECTED_SECONDS * lane.fps),
        }
        proof["expected"] = {**expect, "source": "creative.rendering.ir.LaneProfile()"}

        # 9. VERIFICATION — the queue's own verifier, then the independent one
        queue_outcome = creative_render_verifier(run["payload"], result)
        proof["queue_verifier"] = {"ok": queue_outcome.ok, "reason_code": queue_outcome.reason_code}
        first = verify_artifact_independently(
            artifact, binary, claimed_sha256=result["sha256"], expect=expect
        )

        # 10. REOPEN — fresh handles, every measurement again
        again = verify_artifact_independently(
            artifact, binary, claimed_sha256=result["sha256"], expect=expect
        )
        proof["reopen"] = {
            "identical": (
                first["sha256"],
                first["container"],
                first["decode"],
                first["probe"],
            )
            == (again["sha256"], again["container"], again["decode"], again["probe"]),
            "ok": again["ok"],
        }
        proof["artifact"] = {
            "path_name": artifact.name,
            "sha256": first["sha256"],
            "size_bytes": first["size_bytes"],
            "lane_ir_hash": result.get("spec_ir_hash"),
        }
        proof["verification"] = first

        # UNDO on the exact bus that executed the command
        undo = bus.dispatch(
            TypedCommand(
                command_id=f"{command.command_id}-undo",
                operation="system.undo",
                input={},
                target=command.target,
            )
        )
        proof["undo"] = {
            "undone_operation": undo.output.get("undone_operation"),
            "state_revision": undo.state_revision,
            "state_hash_after_undo": undo.state_hash,
            "artifact_untouched_by_undo": sha256_bytes(artifact) == first["sha256"],
            "note": "undo rewinds project state; published artifact bytes are not retracted",
        }

        # PREVIEW — the real 480p proxy through the same shipped chain
        pws = root / "creative_l4proof_preview"
        pws.mkdir()
        (pws / "input.mp4").write_bytes(fixture.read_bytes())
        prun = _render(pws, "l4proof-preview", "grade", "proxy", [])
        presult = prun["result"]
        preview: dict[str, Any] = {
            "operation": "delivery.make_proxy_480p",
            "success": presult.get("success"),
        }
        if presult.get("success"):
            pv = verify_artifact_independently(
                Path(presult["artifact_path"]),
                binary,
                claimed_sha256=presult["sha256"],
                expect={
                    "size": PROXY_PROFILE_SIZE,
                    "seconds": float(FIXTURE_SECONDS),
                    "frames": FIXTURE_SECONDS * LaneProfile().fps,
                },
            )
            preview.update(
                ok=pv["ok"],
                checks=pv["checks"],
                sha256=pv["sha256"],
                queue_verifier_ok=creative_render_verifier(prun["payload"], presult).ok,
            )
        proof["preview"] = preview

    # MASTER — reality check, never claimed
    proof["master"] = master_reality_check()

    verified = (
        proof["queue_verifier"]["ok"]
        and first["ok"]
        and proof["reopen"]["identical"]
        and proof["reopen"]["ok"]
    )
    proof["status"] = "ARTIFACT_PROOF_VERIFIED" if verified else "ARTIFACT_PROOF_FAILED"
    proof["l4"] = {
        "emitted": False,
        "reason": "canonical ladder (PR#83) requires a production-like measurement that no "
        "source defines; this run is a sandbox/CI harness, not production-like",
    }
    proof["proof_registry_candidate"] = {
        "operation_id": canonical,
        "kind": "artifact_evidence",
        "method": "scripts/l4_artifact_proof.py (shipped creative_render_job chain; queue "
        "verifier + ISO-BMFF walk + full decode + reopen)",
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_revision": _git_head(),
    }
    return proof


def _git_head() -> str:
    try:
        out = subprocess.run(  # noqa: S603, S607 - fixed argv
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parents[1],
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNKNOWN"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None, help="also write the JSON here")
    args = parser.parse_args(argv)
    if args.workdir is None:
        with tempfile.TemporaryDirectory(prefix="l4proof_") as tmp:
            proof = run_slice(Path(tmp))
    else:
        args.workdir.mkdir(parents=True, exist_ok=True)
        proof = run_slice(args.workdir)
    text = json.dumps(proof, indent=2, sort_keys=True, default=str)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if proof["status"] == "ARTIFACT_PROOF_VERIFIED" else 1


if __name__ == "__main__":
    sys.exit(main())
