"""Local studio workspace operations: ``init``, ``doctor`` and the Type-4 ``render`` CLI.

The Typer wrapper lives in :mod:`nexus_ai_agent.creative.studio_typer` so this
module stays importable without the CLI dependency. ``serve`` is a thin wrapper
over uvicorn and is registered there too.

``render`` is the command that joins the isolated subsystems into one engine: it
runs :func:`nexus_ai_agent.creative.studio_cas_vault_bridge.render_storyboard_with_segment_cas`
(Storyboard -> SegmentCAS shot cache -> FFmpeg -> Passport) and, when
``--vault-dir`` is given, immediately backs the result up to the encrypted
personal cloud vault with
:func:`~nexus_ai_agent.creative.studio_cas_vault_bridge.sync_and_verify_studio_bundle_in_vault`
and re-verifies the restored bytes before reporting success. The bridge is
imported lazily so ``init``/``doctor`` keep working without NumPy or the vault.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

#: Bounded read for ``--script-file`` so a huge file can never be slurped.
MAX_SCRIPT_BYTES = 1_048_576
#: Bounded read for ``--signing-key-file`` / ``--vault-secret-file``.
MAX_SECRET_BYTES = 4_096
#: Environment fallbacks for secrets, so they never have to appear on argv.
SIGNING_KEY_ENV = "NEXUS_STUDIO_SIGNING_KEY"
VAULT_SECRET_ENV = "NEXUS_STUDIO_VAULT_SECRET"
#: Environment fallbacks that name a *file* holding the secret (no argv, no env value).
SIGNING_KEY_FILE_ENV = "NAGAR_SIGNING_KEY_FILE"
VAULT_SECRET_FILE_ENV = "NAGAR_VAULT_SECRET_FILE"
#: Durable approval database looked up when ``--approval-db`` is not given.
DEFAULT_APPROVAL_DB = "approvals.sqlite3"
#: Where ``render`` keeps its shot cache when ``--cas-dir`` is not supplied.
DEFAULT_CAS_DIRNAME = ".segment-cas"

#: Directory layout created by ``studio init``, relative to the workspace root.
WORKSPACE_DIRS = ("projects", "assets", "artifacts", "queue")
_DIR_MODE = 0o700


class StudioCliError(RuntimeError):
    """A refusal while preparing the workspace (e.g. a symlink where a dir should be)."""


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...] = field(default_factory=tuple)

    @property
    def healthy(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)

    @property
    def exit_code(self) -> int:
        return 0 if self.healthy else 1


def studio_init(workspace: Path) -> list[Path]:
    """Create the workspace layout with owner-only directories. Idempotent.

    Refuses if a required path already exists as a symlink or a non-directory,
    so ``init`` never follows or repurposes something it did not create.
    """
    root = Path(workspace)
    created: list[Path] = []
    root.mkdir(parents=True, exist_ok=True)
    if stat.S_ISLNK(os.lstat(root).st_mode):
        raise StudioCliError("workspace root is a symlink")
    for name in WORKSPACE_DIRS:
        target = root / name
        if target.is_symlink():
            raise StudioCliError(f"{name} is a symlink; refusing to use it")
        if target.exists() and not target.is_dir():
            raise StudioCliError(f"{name} exists and is not a directory")
        if not target.exists():
            target.mkdir(mode=_DIR_MODE)
            created.append(target)
        os.chmod(target, _DIR_MODE)
    return created


def _binary_ok(binary: str) -> tuple[bool, str]:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [binary, "-version"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}"
    first = completed.stdout.decode("utf-8", errors="replace").splitlines()[:1]
    return completed.returncode == 0, first[0] if first else f"rc={completed.returncode}"


def _atomic_roundtrip(root: Path) -> tuple[bool, str]:
    """Write, fsync, replace and read back a probe file inside the workspace."""
    payload = b"nexus-studio-doctor"
    try:
        fd, tmp_name = tempfile.mkstemp(dir=root, prefix=".doctor.", suffix=".tmp")
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        final = root / ".doctor.probe"
        os.replace(tmp_name, final)
        ok = final.read_bytes() == payload
        final.unlink(missing_ok=True)
        return ok, "atomic write + replace + read-back"
    except OSError as exc:
        return False, type(exc).__name__


def _sqlite_roundtrip(root: Path) -> tuple[bool, str]:
    """Create, write and read a scratch SQLite database in the queue directory."""
    db = root / "queue" / ".doctor.sqlite3"
    try:
        conn = sqlite3.connect(db)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS doctor_probe (v TEXT NOT NULL)")
            conn.execute("DELETE FROM doctor_probe")
            conn.execute("INSERT INTO doctor_probe (v) VALUES ('ok')")
            conn.commit()
            row = conn.execute("SELECT v FROM doctor_probe").fetchone()
        finally:
            conn.close()
        return row == ("ok",), "sqlite write + read"
    except sqlite3.Error as exc:
        return False, type(exc).__name__
    finally:
        db.unlink(missing_ok=True)


def _compiler_ok() -> tuple[bool, str]:
    try:
        from nexus_ai_agent.creative.storyboard_compiler import (
            compile_storyboard,
            verify_manifest_integrity,
        )

        manifest = compile_storyboard("INT. ROOM - DAY\nA: Hello.\n")
        ok = verify_manifest_integrity(manifest)
        return bool(ok), f"compiled {manifest.total_shots} shot(s), integrity={ok}"
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        return False, type(exc).__name__


def studio_doctor(
    workspace: Path,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> DoctorReport:
    """Probe the real environment. Exit code 0 only when every check passes."""
    root = Path(workspace)
    checks: list[DoctorCheck] = []
    for name, binary in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        ok, detail = _binary_ok(binary)
        checks.append(DoctorCheck(name=f"{name}_runs", ok=ok, detail=detail))
    layout_ok = root.is_dir() and all(
        (root / d).is_dir() and not (root / d).is_symlink() for d in WORKSPACE_DIRS
    )
    checks.append(
        DoctorCheck(
            name="workspace_layout",
            ok=layout_ok,
            detail="run `studio init` first" if not layout_ok else "all directories present",
        )
    )
    if layout_ok:
        ok, detail = _atomic_roundtrip(root)
        checks.append(DoctorCheck(name="atomic_write", ok=ok, detail=detail))
        ok, detail = _sqlite_roundtrip(root)
        checks.append(DoctorCheck(name="queue_sqlite", ok=ok, detail=detail))
    else:
        checks.append(DoctorCheck(name="atomic_write", ok=False, detail="skipped: no layout"))
        checks.append(DoctorCheck(name="queue_sqlite", ok=False, detail="skipped: no layout"))
    ok, detail = _compiler_ok()
    checks.append(DoctorCheck(name="storyboard_compiler", ok=ok, detail=detail))
    return DoctorReport(checks=tuple(checks))


# --------------------------------------------------------------------------- #
# Type-4 CLI: parser, secret resolution, render command
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    """Build the studio CLI parser (``init``, ``doctor`` and ``render``)."""
    parser = argparse.ArgumentParser(
        prog="studio",
        description="Local NAGAR studio: workspace init, doctor and Type-4 render.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_cmd = sub.add_parser("init", help="create the workspace layout")
    init_cmd.add_argument("--workspace", required=True, type=Path)

    doctor_cmd = sub.add_parser("doctor", help="probe the real environment")
    doctor_cmd.add_argument("--workspace", required=True, type=Path)
    doctor_cmd.add_argument("--ffmpeg", default="ffmpeg")
    doctor_cmd.add_argument("--ffprobe", default="ffprobe")

    render_cmd = sub.add_parser(
        "render",
        help="incremental render: SegmentCAS shot cache -> FFmpeg -> passport -> vault",
    )
    source = render_cmd.add_mutually_exclusive_group()
    source.add_argument("--script", help="screenplay text given inline")
    source.add_argument("--script-file", type=Path, help="screenplay text read from this file")
    render_cmd.add_argument("--title", default="Untitled")
    render_cmd.add_argument("--job-id", default="job-1")
    render_cmd.add_argument("--project-id", default="studio-local")
    render_cmd.add_argument("--output-dir", required=True, type=Path)
    render_cmd.add_argument("--fps", type=int, default=12)
    render_cmd.add_argument("--width", type=int, default=320)
    render_cmd.add_argument("--height", type=int, default=180)
    render_cmd.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="SHOT_ID=PATH",
        help="use this PNG for one shot instead of the generated slate (repeatable)",
    )
    render_cmd.add_argument(
        "--cas-dir",
        type=Path,
        default=None,
        help="SegmentCAS root; defaults to <output-dir>/.segment-cas",
    )
    render_cmd.add_argument(
        "--vault-dir",
        type=Path,
        default=None,
        help="local personal-cloud vault directory for the encrypted backup",
    )
    render_cmd.add_argument("--vault-journal", type=Path, default=None)
    render_cmd.add_argument("--restore-dir", type=Path, default=None)
    render_cmd.add_argument("--device-id", default="studio-local-node-01")
    render_cmd.add_argument("--signing-key", default=None, help="passport HMAC key (>= 32 chars)")
    render_cmd.add_argument("--signing-key-file", type=Path, default=None)
    render_cmd.add_argument("--vault-secret", default=None)
    render_cmd.add_argument("--vault-secret-file", type=Path, default=None)
    render_cmd.add_argument("--approval-id", default=None)
    render_cmd.add_argument("--principal-id", default=None)
    render_cmd.add_argument(
        "--approval-db",
        type=Path,
        default=None,
        help=(
            "durable SQLite approval ledger to consume --approval-id from "
            f"(default: ./{DEFAULT_APPROVAL_DB} when it exists)"
        ),
    )
    render_cmd.add_argument("--ffmpeg", default="ffmpeg")
    render_cmd.add_argument("--ffprobe", default="ffprobe")
    return parser


def _read_bounded_text(path: Path, *, limit: int, label: str) -> str:
    """Read a text file through one ``O_NOFOLLOW`` descriptor, capped at ``limit``.

    One ``os.open`` means the bytes that are read are the bytes that were
    measured: there is no window in which the path can be repointed at a symlink
    or a FIFO between a check and a ``read_text``. The running total is checked
    on every chunk, so a file that grows while it is being read is refused
    instead of being buffered.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if Path(path).is_symlink():
            raise StudioCliError(f"{label} must be a regular, non-symlink file: {path}") from exc
        raise StudioCliError(f"{label} missing: {path} ({type(exc).__name__})") from exc
    chunks: list[bytes] = []
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise StudioCliError(f"{label} must be a regular, non-symlink file: {path}")
        if st.st_size <= 0 or st.st_size > limit:
            raise StudioCliError(f"{label} size out of range: {st.st_size}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1  # the file object owns the descriptor from here on
            while True:
                chunk = handle.read(8192)
                if not chunk:
                    break
                if sum(len(part) for part in chunks) + len(chunk) > limit:
                    raise StudioCliError(f"{label} grew past {limit} bytes: {path}")
                chunks.append(chunk)
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StudioCliError(f"{label} is not valid UTF-8: {path}") from exc


def _read_secret(
    *,
    inline: str | None,
    path: Path | None,
    env_name: str,
    file_env_name: str | None = None,
) -> str | None:
    """Resolve a secret from a file, then an explicit flag, then the environment.

    ``file_env_name`` names an environment variable holding the *path* of the
    file to read, which keeps the secret itself out of both argv (visible in
    ``ps``) and the environment block.
    """
    if path is None and file_env_name is not None:
        from_env_path = os.environ.get(file_env_name)
        if from_env_path and from_env_path.strip():
            path = Path(from_env_path.strip())
    if path is not None:
        secret = _read_bounded_text(path, limit=MAX_SECRET_BYTES, label="secret file").strip()
        if not secret or len(secret.encode("utf-8")) > MAX_SECRET_BYTES:
            raise StudioCliError(f"secret file is empty or oversized: {path}")
        return secret
    if inline:
        return inline
    from_env = os.environ.get(env_name)
    return from_env.strip() if from_env and from_env.strip() else None


def _read_script(args: argparse.Namespace) -> str:
    """Return the screenplay text from ``--script`` or ``--script-file``."""
    if args.script is not None:
        if not args.script.strip():
            raise StudioCliError("script text is empty")
        return args.script
    if args.script_file is None:
        raise StudioCliError("one of --script or --script-file is required")
    text = _read_bounded_text(args.script_file, limit=MAX_SCRIPT_BYTES, label="script file")
    if not text.strip():
        raise StudioCliError("script file is empty")
    return text


def _utc_now_iso() -> str:
    """Timestamp for a durable approval binding (UTC, second resolution, ``Z``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_approval_db(args: argparse.Namespace) -> Path | None:
    """Return the durable approval database to consume from, or ``None``.

    An explicit ``--approval-db`` always wins; otherwise the conventional
    ``approvals.sqlite3`` beside the render output (or in the working directory)
    is used when it exists, so the CLI works with a real ledger and no wrapper.
    """
    if args.approval_db is not None:
        return Path(args.approval_db)
    for candidate in (
        Path(args.output_dir) / DEFAULT_APPROVAL_DB,
        Path.cwd() / DEFAULT_APPROVAL_DB,
    ):
        if candidate.is_file():
            return candidate
    return None


def _open_durable_ledger(path: Path) -> Any:  # noqa: ANN401 - DurableApprovalLedger
    """Open the SQLite-backed approval ledger, refusing an unusable database."""
    from nexus_ai_agent.nagar.ibpc.approval import DurableApprovalLedger

    try:
        return DurableApprovalLedger(path, now=_utc_now_iso)
    except (OSError, sqlite3.Error) as exc:
        raise StudioCliError(f"approval database unusable: {path} ({type(exc).__name__})") from exc


def _parse_image_specs(specs: Sequence[str]) -> dict[str, Path]:
    """Parse repeated ``--image shot_id=path`` flags into a mapping."""
    images: dict[str, Path] = {}
    for spec in specs:
        shot_id, separator, raw_path = spec.partition("=")
        if not separator or not shot_id or not raw_path:
            raise StudioCliError(f"--image must be SHOT_ID=PATH, got {spec!r}")
        path = Path(raw_path)
        if not path.is_file() or path.is_symlink():
            raise StudioCliError(f"--image path is not a regular file: {path}")
        images[shot_id] = path
    return images


def _render_command(
    args: argparse.Namespace,
    *,
    ledger: Any,  # noqa: ANN401 - ApprovalLedger, imported lazily to keep this module light
    out: TextIO,
) -> int:
    """Run one incremental render, optionally backed up to the encrypted vault."""
    from nexus_ai_agent.creative.materializer import RenderProfile
    from nexus_ai_agent.creative.studio_cas_vault_bridge import (
        StudioBridgeError,
        render_storyboard_with_segment_cas,
        sync_and_verify_studio_bundle_in_vault,
    )

    script_text = _read_script(args)
    signing_key = _read_secret(
        inline=args.signing_key,
        path=args.signing_key_file,
        env_name=SIGNING_KEY_ENV,
        file_env_name=SIGNING_KEY_FILE_ENV,
    )
    if signing_key is None:
        raise StudioCliError(
            "a passport signing key is required "
            f"(--signing-key, --signing-key-file, {SIGNING_KEY_FILE_ENV} or {SIGNING_KEY_ENV})"
        )
    output_dir = Path(args.output_dir)
    cas_dir = Path(args.cas_dir) if args.cas_dir is not None else output_dir / DEFAULT_CAS_DIRNAME
    images = _parse_image_specs(args.image)

    vault_secret: str | None = None
    if args.vault_dir is not None:
        vault_secret = _read_secret(
            inline=args.vault_secret,
            path=args.vault_secret_file,
            env_name=VAULT_SECRET_ENV,
            file_env_name=VAULT_SECRET_FILE_ENV,
        )
        if vault_secret is None:
            raise StudioCliError(
                "--vault-dir needs a vault secret "
                f"(--vault-secret, --vault-secret-file, {VAULT_SECRET_FILE_ENV} "
                f"or {VAULT_SECRET_ENV})"
            )

    before_render = None
    owned_ledger: Any = None
    if args.approval_id is not None:
        if ledger is None:
            approval_db = _resolve_approval_db(args)
            if approval_db is None:
                raise StudioCliError(
                    "an approval ledger is required to consume --approval-id "
                    f"(--approval-db, or a {DEFAULT_APPROVAL_DB} beside the output)"
                )
            owned_ledger = _open_durable_ledger(approval_db)
            ledger = owned_ledger
        if not args.principal_id:
            raise StudioCliError("--approval-id requires --principal-id")
        from nexus_ai_agent.creative.storyboard_render import approval_identity_for
        from nexus_ai_agent.nagar.ibpc.approval import ApprovalError

        profile_hash = RenderProfile.from_mapping(
            {"width": args.width, "height": args.height, "fps": args.fps, "sample_rate": 16_000}
        ).profile_hash()
        import hashlib

        source_sha256 = hashlib.sha256(script_text.encode("utf-8")).hexdigest()

        def _consume(compiled: Any) -> None:  # noqa: ANN401
            try:
                ledger.consume(
                    args.approval_id,
                    identity=approval_identity_for(
                        project_id=args.project_id,
                        job_id=args.job_id,
                        principal_id=args.principal_id,
                        source_sha256=source_sha256,
                        script_sha256=compiled.script_sha256,
                        manifest_sha256=compiled.manifest_sha256,
                        render_profile_hash=profile_hash,
                    ),
                )
            except ApprovalError as exc:
                raise StudioCliError(f"approval_{exc.reason}") from exc

        before_render = _consume

    try:
        result = render_storyboard_with_segment_cas(
            script_text,
            args.title,
            output_dir,
            cas_dir,
            args.job_id,
            args.approval_id or "",
            args.principal_id or "",
            fps=args.fps,
            width=args.width,
            height=args.height,
            local_images=images or None,
            signing_key=signing_key,
            project_id=args.project_id,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
            before_render=before_render,
        )
    except StudioBridgeError as exc:
        payload: dict[str, Any] = {
            "status": "refused",
            "reason": exc.reason,
            "detail": exc.detail,
        }
        json.dump(payload, out, sort_keys=True)
        out.write("\n")
        return 1
    finally:
        if owned_ledger is not None:
            owned_ledger.close()

    payload = {
        "status": "rendered",
        "job_id": args.job_id,
        "project_id": args.project_id,
        "output_path": str(result.output_mp4_path),
        "output_sha256": result.verification.output_sha256,
        "output_size_bytes": result.verification.size_bytes,
        "probed_duration_s": result.verification.probed_duration_s,
        "verifier_status": result.verification.status,
        "passport_path": str(result.passport_path),
        "passport_sha256": result.passport["hmac_sha256"],
        "manifest_path": str(result.manifest_path),
        "receipt_path": str(result.receipt_path),
        "shot_count": len(result.artifact.shot_hashes),
        "shot_merkle_root": result.shot_merkle_root,
        "shot_inclusion_proof_count": len(result.passport["shot_inclusion_proofs"]),
        "cas_dir": str(cas_dir),
        "cas_hits": result.cas_hits,
        "cas_misses": result.cas_misses,
        "peak_memory_bytes": result.peak_memory_bytes,
    }

    if vault_secret is not None:
        restore_dir = Path(args.restore_dir) if args.restore_dir else output_dir / "_restored"
        journal = (
            Path(args.vault_journal) if args.vault_journal else output_dir / "vault_journal.jsonl"
        )
        try:
            sync = sync_and_verify_studio_bundle_in_vault(
                render_result=result,
                vault_storage_dir=args.vault_dir,
                vault_journal_db_path=journal,
                restore_dir=restore_dir,
                master_secret=vault_secret,
                project_id=args.project_id,
                signing_key=signing_key,
                device_id=args.device_id,
                ffprobe=args.ffprobe,
            )
        except (StudioBridgeError, RuntimeError) as exc:
            payload["status"] = "vault_failed"
            payload["vault_reason"] = getattr(exc, "reason", type(exc).__name__)
            json.dump(payload, out, sort_keys=True)
            out.write("\n")
            return 1
        payload.update(
            vault_snapshot_seq=sync.snapshot_seq,
            vault_manifest_id=sync.manifest_id,
            vault_pushed_chunks=sync.pushed_chunks,
            vault_deduplicated_chunks=sync.deduplicated_chunks,
            vault_restored_files_count=sync.restored_files_count,
            vault_restored_mp4_path=str(sync.restored_mp4_path),
            vault_restored_passport_path=str(sync.restored_passport_path),
            vault_restored_passport_status=sync.restored_passport_verification.passport.get(
                "verifier_status"
            ),
            vault_restored_artifact_status=sync.restored_artifact_verification.status,
            vault_restored_verified=True,
        )

    json.dump(payload, out, sort_keys=True)
    out.write("\n")
    return 0


def run_studio_cli(
    argv: Sequence[str] | None = None,
    *,
    ledger: Any = None,  # noqa: ANN401 - optional ApprovalLedger
    out: TextIO | None = None,
) -> int:
    """Entry point for the studio CLI; returns a process exit code."""
    stream = out if out is not None else sys.stdout
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse already printed the usage message
        return int(exc.code or 0)
    try:
        if args.command == "init":
            created = studio_init(Path(args.workspace))
            json.dump({"status": "initialized", "created": [str(p) for p in created]}, stream)
            stream.write("\n")
            return 0
        if args.command == "doctor":
            report = studio_doctor(Path(args.workspace), ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
            json.dump(
                {
                    "status": "healthy" if report.healthy else "unhealthy",
                    "checks": [
                        {"name": c.name, "ok": c.ok, "detail": c.detail} for c in report.checks
                    ],
                },
                stream,
            )
            stream.write("\n")
            return report.exit_code
        return _render_command(args, ledger=ledger, out=stream)
    except StudioCliError as exc:
        json.dump({"status": "refused", "reason": str(exc)}, stream, sort_keys=True)
        stream.write("\n")
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point (``python -m nexus_ai_agent.creative.studio_cli``)."""
    return run_studio_cli(argv)


if __name__ == "__main__":  # pragma: no cover - exercised through ``main`` in tests
    raise SystemExit(main())


__all__ = [
    "DEFAULT_APPROVAL_DB",
    "DEFAULT_CAS_DIRNAME",
    "MAX_SCRIPT_BYTES",
    "MAX_SECRET_BYTES",
    "SIGNING_KEY_ENV",
    "SIGNING_KEY_FILE_ENV",
    "VAULT_SECRET_ENV",
    "VAULT_SECRET_FILE_ENV",
    "WORKSPACE_DIRS",
    "DoctorCheck",
    "DoctorReport",
    "StudioCliError",
    "build_parser",
    "main",
    "run_studio_cli",
    "studio_doctor",
    "studio_init",
]
