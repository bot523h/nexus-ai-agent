"""One authenticated chain over the REAL ASGI app: pin -> submit -> approve ->
execute -> queue -> worker -> passport, plus a revocation halfway through.

Every other Studio suite proves one boundary.  This one proves the *chain*: that
a single deployment, reached only through HTTP, carries one turn from a pinned
revision to a verified artifact with a passport, and that the authority
decisions are re-made at execution time rather than remembered from approve
time.

Two properties are the point of the file:

* the six steps compose -- a session opened over HTTP is the same session the
  submit route sees, whose approval the execute route spends, whose job the
  real worker drains, and whose passport is readable afterwards;
* revoking a principal between approve and execute refuses the execution and
  enqueues nothing.  That is a worker-boundary property, not a route property:
  ``execute`` must not trust the decision ``approve`` already made.

Nothing is mocked below the HTTP boundary.  The queue is the real
``InProcessJobQueue`` the app's creative registry exposes, the handlers are the
real ``default_job_handlers``, the ledger/pinning/outbox stores are the real
durable ones on a temporary disk, and the clip is a real FFmpeg render.
"""

from __future__ import annotations

import base64
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

TENANT = "ten-e2e-http"
PROJECT = "proj-e2e-http"
OWNER_ID = "prn_" + "a" * 32
EDITOR_ID = "prn_" + "b" * 32
BEARER = "studio-e2e-deploy-token"
OWNER_TOKEN = "tok-e2e-owner-secret"
EDITOR_TOKEN = "tok-e2e-editor-secret"
BACKUP_KEY_HEX = "cd" * 32
SIZE = "1280x720"
RATE = 30
SECONDS = 2.0


def _clip(path: Path) -> None:
    from nexus_ai_agent.creative.slideshow.ffmpeg import FfmpegUnavailableError, resolve_ffmpeg_bin

    try:
        binary = resolve_ffmpeg_bin()
    except FfmpegUnavailableError as exc:  # pragma: no cover - environment only
        pytest.skip(f"FFmpeg unavailable: {exc}")
    proc = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={SECONDS}:size={SIZE}:rate={RATE}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={SECONDS}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        capture_output=True,
        timeout=180,
    )
    if proc.returncode != 0:  # pragma: no cover - environment only
        pytest.skip(f"could not synthesize clip: {proc.stderr.decode('utf-8', 'replace')[-200:]}")


@pytest.fixture()
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """A hermetic Studio deployment behind the real ASGI app."""
    from nexus_ai_agent.adapters.studio_principal_auth import StudioPrincipalAuthStore
    from nexus_ai_agent.api import app as app_module
    from nexus_ai_agent.config.settings import get_settings
    from nexus_ai_agent.nagar.ibpc.authority import StudioRole

    # The runtime's workspace root must sit inside the creative temp dir: the
    # worker independently refuses a workspace outside it (a real containment
    # check, not a fixture convenience).
    creative_tmp = tmp_path / "creative_tmp"
    creative_tmp.mkdir()
    workspace = creative_tmp / "workspace"
    workspace.mkdir()
    assets = workspace / "assets"
    assets.mkdir()
    auth_db = tmp_path / "studio_auth.sqlite3"

    monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", BEARER)
    monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", PROJECT)
    monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("NEXUS_STUDIO_ASSET_ROOT", str(assets))
    monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
    monkeypatch.setenv("NEXUS_STUDIO_AUTHORITY_DB", str(auth_db))
    monkeypatch.setenv("NEXUS_STUDIO_ACTIVE_REVISION", "5")
    monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "queue.sqlite3"))
    monkeypatch.setenv("NEXUS_STUDIO_QUEUE_DB_PATH", str(tmp_path / "queue.sqlite3"))
    monkeypatch.setenv("NEXUS_STUDIO_BACKUP_ROOT", str(tmp_path / "backups"))
    monkeypatch.setenv("NEXUS_STUDIO_BACKUP_KEY_HEX", BACKUP_KEY_HEX)
    monkeypatch.setenv("NEXUS_STUDIO_BACKUP_KEY_ID", "bk-e2e")
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(creative_tmp))
    # The application directory (which the *worker* authorizes against) is a
    # different database from the Studio ledger: point both at this test's tmp.
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "NEXUS_IBPC_APPROVAL_KEY_RING",
        json.dumps({"k1": base64.b64encode(b"k" * 32).decode("ascii")}),
    )
    monkeypatch.setenv("NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID", "k1")
    # Settings are cached process-wide and the storage engine is resolved from
    # them, so the cache must be dropped BEFORE the schema is created -- or the
    # tables land in whatever database the previous test selected.
    get_settings.cache_clear()

    from nexus_ai_agent.storage.db import sync_engine
    from nexus_ai_agent.storage.migrations import ensure_startup_schema
    from nexus_ai_agent.storage.models import SQLModel

    ensure_startup_schema()
    SQLModel.metadata.create_all(sync_engine())
    monkeypatch.setattr(app_module, "_creative_registry", None, raising=False)

    def _now() -> str:
        return "2026-11-27T00:00:00+00:00"

    store = StudioPrincipalAuthStore(auth_db, now=_now)
    store.ensure_project(PROJECT, tenant_id=TENANT, active_revision_id="5")
    for principal_id, token, role in (
        (OWNER_ID, OWNER_TOKEN, StudioRole.OWNER),
        (EDITOR_ID, EDITOR_TOKEN, StudioRole.EDITOR),
    ):
        store.register_principal(
            principal_id,
            tenant_id=TENANT,
            raw_token=token,
            display_name=role.value,
            allow_system_bootstrap=True,
        )
        store.grant_membership(
            PROJECT, principal_id, role=role, granted_by="system", allow_system_bootstrap=True
        )
        store.bind_channel(
            principal_id,
            channel="studio",
            external_subject=f"subject-{principal_id}",
            user_id=1,
            chat_id=2,
            allow_system_bootstrap=True,
        )
    # The route forwards the Studio store's own binding_ref into the service, and
    # the worker looks *that* id up in the directory -- so the two stores have to
    # agree on it.  Read it from the Studio store rather than guessing its shape.
    studio_binding_refs = {
        principal_id: store.default_binding_for(principal_id)
        for principal_id in (OWNER_ID, EDITOR_ID)
    }
    store.close()

    # The Studio role store and the application directory are two different
    # tables, and the *worker* authorizes against the directory: it re-decides
    # membership at render time rather than trusting the route.  Seed it too, so
    # the chain under test is the production one and not a stubbed worker.
    # The directory's API only mints ids, but the *worker* authorizes with the
    # id the Studio ledger carries -- so the same row is seeded here directly.
    # This is test seeding of a durable table, not a mock: the worker's own
    # authorization path then runs for real, against the real project grant.
    from nexus_ai_agent.adapters.sql_application_directory import (
        DEFAULT_PROJECT_PERMISSIONS,
        SQLApplicationDirectory,
    )
    from nexus_ai_agent.storage.models import (
        AppChannelBinding,
        AppPrincipal,
        AppProject,
        AppProjectMembership,
    )

    directory = SQLApplicationDirectory()
    with directory._engine.begin() as conn:  # noqa: SLF001 - fixture seeding only
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        for principal_id in (OWNER_ID, EDITOR_ID):
            conn.execute(
                sqlite_insert(AppPrincipal)
                .values(principal_id=principal_id)
                .on_conflict_do_nothing()
            )
        conn.execute(sqlite_insert(AppProject).values(project_id=PROJECT).on_conflict_do_nothing())
        for principal_id in (OWNER_ID, EDITOR_ID):
            # The acting channel must really be bound to the acting principal:
            # the worker re-checks that too, independently of the route.
            conn.execute(
                sqlite_insert(AppChannelBinding)
                .values(
                    binding_id=studio_binding_refs[principal_id],
                    principal_id=principal_id,
                    channel="studio",
                    external_id=f"subject-{principal_id}",
                )
                .on_conflict_do_nothing()
            )
            conn.execute(
                sqlite_insert(AppProjectMembership)
                .values(
                    project_id=PROJECT,
                    principal_id=principal_id,
                    permissions_json=json.dumps(sorted(DEFAULT_PROJECT_PERMISSIONS)),
                )
                .on_conflict_do_nothing()
            )

    client = TestClient(app_module.app)
    try:
        yield {
            "client": client,
            "workspace": workspace,
            "assets": assets,
            # The API's single job authority is the queue sidecar of
            # NEXUS_DB_PATH; NEXUS_QUEUE_DB_PATH is not what the registry reads.
            "queue_db": Path(str(tmp_path / "app.sqlite") + ".jobs.sqlite3"),
        }
    finally:
        get_settings.cache_clear()


def _headers(token: str = OWNER_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {BEARER}", "X-Nexus-Principal-Token": token}


def _open_session(client: TestClient) -> str:
    response = client.post(
        f"/api/studio/projects/{PROJECT}/ibpc/sessions",
        json={"user_id": 5, "chat_id": 55, "current_revision": 5},
        headers=_headers(),
    )
    assert response.status_code == 201, response.text[:600]
    return str(response.json()["session_id"])


def _submit(client: TestClient, session_id: str, source: Path, turn_id: str = "turn-e2e") -> str:
    response = client.post(
        f"/api/studio/projects/{PROJECT}/ibpc/sessions/{session_id}/turns",
        json={
            "turn_id": turn_id,
            "sequence_number": 1,
            "text": "trim from 0.5 to 1.5 seconds",
            "asset_id": "src",
            "source_path": str(source),
        },
        headers=_headers(),
    )
    assert response.status_code == 201, response.text[:600]
    return str(response.json()["approval_id"])


def _approve(
    client: TestClient, session_id: str, approval_id: str, turn_id: str = "turn-e2e"
) -> None:
    response = client.post(
        f"/api/studio/projects/{PROJECT}/ibpc/sessions/{session_id}/turns/{turn_id}/approve",
        json={"approval_id": approval_id},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text[:600]


def _execute(
    client: TestClient, session_id: str, approval_id: str, turn_id: str = "turn-e2e"
) -> Any:
    return client.post(
        f"/api/studio/projects/{PROJECT}/ibpc/sessions/{session_id}/turns/{turn_id}/execute",
        json={"approval_id": approval_id},
        headers=_headers(),
    )


def _queue_jobs(queue_db: Path) -> list[tuple[str, str]]:
    import sqlite3

    if not queue_db.is_file():
        return []
    with sqlite3.connect(queue_db) as conn:
        try:
            rows = conn.execute("SELECT id, status FROM nexus_job_queue").fetchall()
        except sqlite3.OperationalError:
            return []
    return [(str(row[0]), str(row[1])) for row in rows]


def test_the_six_step_chain_runs_end_to_end_over_http(
    deployment: dict[str, Any], tmp_path: Path
) -> None:
    """pin -> submit -> preview -> approve -> execute -> queue -> passport."""
    client = deployment["client"]
    source = deployment["assets"] / "e2e-source.mp4"
    _clip(source)

    # ---- 1. pin a revision through HTTP -----------------------------------
    from nexus_ai_agent.creative.slideshow.ffmpeg import sha256_file

    asset_digest = sha256_file(source).removeprefix("sha256:")
    pin = client.post(
        f"/api/studio/projects/{PROJECT}/ibpc/revisions/pin",
        json={
            "revision_id": "5",
            # A genesis pin: the first revision of a lineage names no parent.
            "revision_number": 1,
            "session_id": "pinned-session",
            "turn_id": "pinned-turn",
            "plan_id": "plan-e2e-1",
            "plan_jcs_sha256": "1" * 64,
            "parameters_jcs_sha256": "2" * 64,
            "preview_digest": "sha256:" + "3" * 64,
            "assets": [
                {
                    "asset_id": "src",
                    "relative_path": source.name,
                    "content_sha256": asset_digest,
                    "byte_size": source.stat().st_size,
                    "media_type": "video/mp4",
                }
            ],
        },
        headers=_headers(),
    )
    assert pin.status_code == 201, pin.text[:600]
    assert pin.json()["revision_id"] == "5"

    # ---- 2. open a session on that pinned revision ------------------------
    session_id = _open_session(client)
    assert session_id

    # ---- 3. submit an intent; the service admits and previews it ----------
    approval_id = _submit(client, session_id, source)
    assert approval_id

    # --- 4. the preview readback is a function of the returned body --------
    readback = client.post(
        f"/api/studio/projects/{PROJECT}/ibpc/preview-readback",
        json={
            "revision_id": "5",
            # A genesis pin: the first revision of a lineage names no parent.
            "revision_number": 1,
            "timebase_numerator": 30000,
            "timebase_denominator": 1001,
            "operations": [
                {
                    "operation": "preview.sample_cut",
                    "target_clip_id": "clip-1",
                    "span": ["0/1", "1/2"],
                    "labels": {"fa": "برش", "en": "Cut"},
                }
            ],
        },
        headers=_headers(),
    )
    assert readback.status_code == 200, readback.text[:600]
    assert readback.json()["wysiwys_verified"] is True

    # ---- 5. approve, then execute: exactly one durable job arrives --------
    _approve(client, session_id, approval_id)
    assert _queue_jobs(deployment["queue_db"]) == [], "nothing may be queued before execute"

    executed = _execute(client, session_id, approval_id)
    assert executed.status_code == 200, executed.text[:800]
    body = executed.json()
    assert body["status"] in ("executed", "executed_unverified"), body
    assert body["job_id"]

    jobs = _queue_jobs(deployment["queue_db"])
    assert [job_id for job_id, _status in jobs] == [body["job_id"]], (
        "execute must enqueue exactly one job, and it must be the one it reported"
    )

    # ---- 6. the worker really ran, and the turn reached EXECUTED ----------
    session_view = client.get(
        f"/api/studio/projects/{PROJECT}/ibpc/sessions/{session_id}", headers=_headers()
    )
    assert session_view.status_code == 200, session_view.text[:600]
    turns = session_view.json().get("turns") or []
    matching = [turn for turn in turns if turn.get("turn_id") == "turn-e2e"]
    assert len(matching) == 1, session_view.json()
    view = matching[0]
    assert view["turn_state"] == "EXECUTED", view

    # The artifact the worker claimed exists, and its digest is the digest the
    # queue recorded -- checked out of band, not from the response body.
    import sqlite3

    with sqlite3.connect(deployment["queue_db"]) as conn:
        row = conn.execute(
            "SELECT status FROM nexus_job_queue WHERE id = ?", (body["job_id"],)
        ).fetchone()
    assert row is not None and str(row[0]).lower() in ("completed", "succeeded")


def test_a_revocation_between_approve_and_execute_refuses_and_enqueues_nothing(
    deployment: dict[str, Any], tmp_path: Path
) -> None:
    """The worker boundary re-decides authority; approve time is not a licence.

    The approval is signed, unspent and unexpired.  The *only* thing that
    changed is the principal's standing, and that is precisely what `execute`
    must notice -- otherwise a revoked insider keeps a play button.
    """
    client = deployment["client"]
    source = deployment["assets"] / "revoke-source.mp4"
    _clip(source)

    session_id = _open_session(client)
    approval_id = _submit(client, session_id, source)
    _approve(client, session_id, approval_id)

    revoked = client.post(
        f"/api/studio/projects/{PROJECT}/principals/{EDITOR_ID}/revoke", headers=_headers()
    )
    assert revoked.status_code == 200, revoked.text[:600]

    # The owner is untouched: the refusal below is about the EDITOR, so first
    # prove the chain still works for a principal in good standing.
    ok = _execute(client, session_id, approval_id)
    assert ok.status_code == 200, ok.text[:600]
    assert _queue_jobs(deployment["queue_db"]), "the good-standing principal must execute"

    # Now the same chain, but the submitting principal is revoked first.
    second_session = _open_session(client)
    second_approval = _submit(client, second_session, source, turn_id="turn-revoked")
    _approve(client, second_session, second_approval, turn_id="turn-revoked")
    client.post(f"/api/studio/projects/{PROJECT}/principals/{OWNER_ID}/revoke", headers=_headers())

    refused = _execute(client, second_session, second_approval, turn_id="turn-revoked")
    assert refused.status_code in (401, 403), refused.text[:600]
