"""Tests for the ATLAS studio render adapter (Item 4).

These exercise the **real** studio authorization seam
(:mod:`nexus_ai_agent.creative.studio.authorization`): a real
``ProjectAccess`` grant, real ``ActorIdentity`` principals and the real
``AuthorizationError`` from ``studio.models``. No mock stands in for the
authorization decision.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.creative.atlas import atlas_spine_governance as gov
from nexus_ai_agent.creative.atlas import studio_render_adapter as adapter
from nexus_ai_agent.creative.atlas import titan_feature_film_compiler as tf
from nexus_ai_agent.creative.episode_causality_lab import (
    SCHEMA_VERSION,
    Character,
    Episode,
    PlanRequest,
    Scene,
    ShotCandidate,
    SourceRef,
    StoryBeat,
    StoryDocument,
    TimeOfDay,
    TriValue,
)
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.models import ActorIdentity
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

_SRC = SourceRef(path="ep1/sc1", line=1)


def _film() -> tf.CompiledFilm:
    """A tiny deterministic film fixture (same shape as the governance helper)."""
    doc = StoryDocument(
        schema_version=SCHEMA_VERSION,
        characters=(Character(id="alice"),),
        episodes=(Episode(id="ep1", order_index=0),),
        scenes=(
            Scene(
                id="sc1",
                episode_id="ep1",
                location_id="loc1",
                order_index=0,
                time_of_day=TimeOfDay.DAY,
            ),
        ),
        beats=(StoryBeat(id="b1", episode_id="ep1", order_index=0),),
    )
    shot = ShotCandidate(
        id="s1",
        asset_ref="asset://s1",
        covers_beats=("b1",),
        frames=24,
        evidence_status=TriValue.TRUE,
        evidence_ref="evidence://s1",
        source=_SRC,
    )
    req = PlanRequest(beats=("b1",), candidates=(shot,), frame_budget=100)
    return tf.compile_feature_film(doc, plan_request=req, scene_flags=frozenset({"crowd"}))


def _actor() -> ActorIdentity:
    return ActorIdentity(kind="service", actor_id="studio-renderer")


def _grant(permissions: frozenset[str]) -> ProjectAccess:
    return ProjectAccess(actor=_actor(), project_id="proj-1", permissions=permissions)


def test_adapter_authorizes_and_runs_the_staged_spine(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    ad = adapter.StudioRenderAdapter(
        _grant(frozenset({"project:write"})), _actor(), project_id="proj-1"
    )
    ledger = ad.render_spine(_film(), fs)
    assert "authority_mode=AUTHORIZED" in ledger.notes


def test_adapter_refuses_when_actor_is_not_a_project_member(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    # Grant is bound to a *different* project, so the studio seam denies proj-1.
    wrong = ProjectAccess(
        actor=_actor(), project_id="proj-OTHER", permissions=frozenset({"project:write"})
    )
    ad = adapter.StudioRenderAdapter(wrong, _actor(), project_id="proj-1")
    with pytest.raises(adapter.StudioRenderError) as exc:
        ad.render_spine(_film(), fs)
    assert exc.value.code == "STUDIO_DENIED"
    assert not fs.exists("titan_spine")  # fail-closed: nothing staged


def test_adapter_refuses_without_the_required_permission(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    read_only = _grant(frozenset({"project:read"}))
    ad = adapter.StudioRenderAdapter(read_only, _actor(), project_id="proj-1")
    with pytest.raises(adapter.StudioRenderError) as exc:
        ad.render_spine(_film(), fs)
    assert exc.value.code == "STUDIO_DENIED"


def test_adapter_refuses_actor_label_mismatch(tmp_path: Path) -> None:
    ad = adapter.StudioRenderAdapter(
        _grant(frozenset({"project:write"})), _actor(), project_id="proj-1"
    )
    port = adapter.studio_authorizer(_grant(frozenset({"project:write"})), _actor())
    with pytest.raises(adapter.StudioRenderError) as exc:
        port.authorize(operation="op", actor="someone-else", project_id="proj-1", payload={})
    assert exc.value.code == "ACTOR_MISMATCH"
    # The bound adapter still authorizes its own trusted label.
    assert ad.authorizer is not None


def test_adapter_permits_predicate_pins_the_exact_project() -> None:
    port = adapter.studio_authorizer(_grant(frozenset({"project:write"})), _actor())
    decision = port.authorize(operation="op", actor="atlas", project_id="proj-1", payload={})
    assert decision.authorized is True
    assert decision.permits is not None
    assert decision.permits("proj-1", "rev-1", "op", "attempt-1", 1) is True
    assert decision.permits("proj-2", "rev-1", "op", "attempt-1", 1) is False
    assert decision.permits("proj-1", "rev-1", "other-op", "attempt-1", 1) is False


def test_adapter_custom_permission_requirement_is_honored() -> None:
    port = adapter.studio_authorizer(
        _grant(frozenset({"creative:render"})),
        _actor(),
        required_permissions=("creative:render",),
    )
    decision = port.authorize(operation="op", actor="atlas", project_id="proj-1", payload={})
    assert decision.authorized is True


def test_adapter_render_spine_commits_lineage(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    store = gov.AtlasLineageStore(fs)
    ad = adapter.StudioRenderAdapter(
        _grant(frozenset({"project:write"})), _actor(), project_id="proj-1"
    )
    ledger = ad.render_spine(
        _film(),
        fs,
        lineage=store,
        fencing_token=1,
        attempt_id="attempt-1",
    )
    assert "authority_mode=AUTHORIZED" in ledger.notes
    assert store.highest_token == 1
    assert len(store.records()["asset_versions"]) == 1
    assert len(store.records()["artifact_passports"]) == 1


def test_adapter_render_master_runs_under_studio_authority(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.atlas import showrunner_bible as sb

    fs = WorkspaceFilesystem(tmp_path)
    ad = adapter.StudioRenderAdapter(
        _grant(frozenset({"project:write"})), _actor(), project_id="proj-1"
    )
    bible = sb.ShowrunnerBible()
    bible.add_character(sb.Character(cid="c1", name="Ada"))
    bible.add_prop(sb.Prop(pid="p1", name="revolver"))
    bible.add_episode(sb.Episode(number=1, title="Pilot"))
    result = ad.render_master(bible, fs)
    assert result is not None
    # The master ledger records the continuity stage under studio authority.
    assert any(stage.name == "continuity" for stage in result.stages)
