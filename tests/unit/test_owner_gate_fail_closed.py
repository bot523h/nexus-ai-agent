"""The owner identity gate must fail closed when no owner is configured (task-253).

Live defect on ``main@15ef5a9``: ``features/owner_control.py::is_owner``
compares the candidate against ``settings.owner_telegram_id`` — whose
*declared default* is ``0`` — so the "owner not configured" sentinel
authenticates Telegram user id ``0``.  Every owner-gated call site in the bot
fabricates exactly that id for an update without a user::

    if not is_owner(update.effective_user.id if update.effective_user else 0):
        return

and the global deny-by-default guard *deliberately* lets such updates through
(``bot/access_guard.py::AccessGuardHandler.check_update``: ``if user is None:
return False`` — "every real command handler degrades safely without a user").
Composed, an update with no ``effective_user`` (e.g. an anonymous admin post in
a supergroup, where Telegram sets ``sender_chat`` and omits ``from``) reaches
owner-only handlers and, with the owner unset, passes their gate.

Levels of proof:
1. the sentinel is the declared settings default (the precondition);
2. ``is_owner`` over the sentinel — configured, unset, and via the real
   settings/env path;
3. the ``owner_only`` decorator over an update without an effective user;
4. the composition with the **real** ``AccessGuardHandler`` from
   ``build_access_guard`` (the guard passes the update in, the gate must stop
   it);
5. no regression: a genuinely configured owner still passes every gate.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nexus_ai_agent.bot.access_guard import AccessGuardHandler, build_access_guard
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.features import owner_control
from nexus_ai_agent.features.owner_control import OwnerControl, is_owner, owner_only

#: The id the repo's owner-gated call sites fabricate when an update carries no
#: ``effective_user`` (``bot/handlers.py``: ``… else 0``).
FABRICATED_NO_USER_ID = 0
OWNER = 4242
STRANGER = 99


def _patch_owner(monkeypatch: pytest.MonkeyPatch, owner_id: int) -> Settings:
    """Point ``owner_control`` at a deterministic settings object.

    ``owner_control`` imports ``get_settings`` into its own namespace and caches
    the id in a module global, so both must be controlled for a unit-level test.
    ``_env_file=None`` keeps any workspace ``.env`` out of the fixture.
    """
    settings = Settings(_env_file=None).model_copy(update={"owner_telegram_id": owner_id})
    monkeypatch.setattr(owner_control, "get_settings", lambda: settings)
    monkeypatch.setattr(owner_control, "_owner_id", None, raising=False)
    return settings


def _recorder() -> tuple[Any, list[Any]]:
    """An async ``send_message`` stand-in plus the calls it received."""
    calls: list[Any] = []

    async def record(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    return record, calls


def _update_without_user() -> SimpleNamespace:
    """A userless update — no ``effective_user``, like an anonymous admin post."""
    send, _calls = _recorder()
    return SimpleNamespace(
        effective_user=None,
        effective_chat=SimpleNamespace(send_message=send),
        message=SimpleNamespace(text="/approve 1"),
        edited_message=None,
        callback_query=None,
    )


# --------------------------------------------------------------------- #
# 1. the precondition: the sentinel really is the declared default
# --------------------------------------------------------------------- #


def test_the_unset_owner_sentinel_is_the_declared_settings_default() -> None:
    """``0`` is not a Telegram user id — it is the "no owner configured" default."""
    default = Settings.model_fields["owner_telegram_id"].default
    assert default == 0, "this fix is scoped to the 0 sentinel"
    assert Settings(_env_file=None).owner_telegram_id == 0


# --------------------------------------------------------------------- #
# 2. is_owner over the sentinel
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("candidate", [0, 1, -1, OWNER, 2**63 - 1])
def test_no_owner_configured_owns_nobody(monkeypatch: pytest.MonkeyPatch, candidate: int) -> None:
    """With the owner unset, no candidate id may pass — least of all the fabricated 0."""
    _patch_owner(monkeypatch, owner_id=0)
    assert is_owner(candidate) is False


def test_unset_owner_is_fail_closed_through_the_real_settings_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same proof through the module's own cache and the real env plumbing."""
    monkeypatch.setenv("NEXUS_OWNER_TELEGRAM_ID", "0")
    settings_module.get_settings.cache_clear()
    monkeypatch.setattr(owner_control, "_owner_id", None, raising=False)
    try:
        assert is_owner(FABRICATED_NO_USER_ID) is False
    finally:
        monkeypatch.delenv("NEXUS_OWNER_TELEGRAM_ID", raising=False)
        settings_module.get_settings.cache_clear()
        monkeypatch.setattr(owner_control, "_owner_id", None, raising=False)


def test_protected_command_is_fail_closed_without_an_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_owner(monkeypatch, owner_id=0)
    assert OwnerControl.protected_command(FABRICATED_NO_USER_ID) is False


# --------------------------------------------------------------------- #
# 3. the owner_only decorator over an update without an effective user
# --------------------------------------------------------------------- #


async def test_owner_only_refuses_an_update_without_an_effective_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decorator must not promote "no user" into the owner identity."""
    _patch_owner(monkeypatch, owner_id=0)
    executed: list[str] = []

    @owner_only
    async def handler(update: Any) -> str:
        executed.append("ran")
        return "owner action"

    result = await handler(_update_without_user())

    assert executed == [], "an update without a user reached an owner-only handler"
    assert result is None


async def test_owner_only_refuses_a_user_object_without_an_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed user object is not an identity either."""
    _patch_owner(monkeypatch, owner_id=0)
    executed: list[str] = []

    @owner_only
    async def handler(update: Any) -> str:
        executed.append("ran")
        return "owner action"

    update = SimpleNamespace(effective_user=SimpleNamespace(), effective_chat=None)
    assert await handler(update) is None
    assert executed == []


async def test_owner_only_never_consults_the_gate_for_a_missing_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second fence: the missing-user decision is the decorator's own.

    Even if the authority were to answer "owner" for the ``0`` sentinel (the
    historical regression this lane fixes), the decorator must still refuse:
    only a *present* user identity may be evaluated at all.
    """
    _patch_owner(monkeypatch, owner_id=0)
    monkeypatch.setattr(owner_control, "is_owner", lambda user_id: True)  # broken authority
    executed: list[str] = []

    @owner_only
    async def handler(update: Any) -> str:
        executed.append("ran")
        return "owner action"

    assert await handler(_update_without_user()) is None
    assert executed == []


async def test_owner_only_denial_survives_a_malformed_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A userless update without ``effective_chat`` must be refused, never raise."""
    _patch_owner(monkeypatch, owner_id=0)
    executed: list[str] = []

    @owner_only
    async def handler(update: Any) -> str:
        executed.append("ran")
        return "owner action"

    assert await handler(SimpleNamespace()) is None
    assert executed == []


async def test_owner_only_refuses_a_stranger(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_owner(monkeypatch, owner_id=OWNER)
    executed: list[str] = []
    send, replies = _recorder()

    @owner_only
    async def handler(update: Any) -> str:
        executed.append("ran")
        return "owner action"

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=STRANGER),
        effective_chat=SimpleNamespace(send_message=send),
    )
    result = await handler(update)

    assert result is None
    assert executed == []
    assert len(replies) == 1, "the stranger is told the command was refused"


# --------------------------------------------------------------------- #
# 4. the composition with the real access guard
# --------------------------------------------------------------------- #


def test_the_guard_really_does_pass_a_userless_update_through() -> None:
    """The premise of the escalation, asserted on the real guard (not a replica)."""
    settings = Settings(_env_file=None).model_copy(
        update={"owner_telegram_id": 0, "allowed_user_ids": []}
    )
    guard = build_access_guard(settings)
    assert isinstance(guard, AccessGuardHandler)
    assert guard.check_update(_update_without_user()) is False


async def test_guard_pass_through_plus_owner_gate_executes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Group −1 lets the userless update in; the owner gate must be the stop.

    This is the production shape: guard → command handler → owner gate.  With
    the owner unset the gate answered "you are the owner" for the fabricated id
    ``0``, so the owner-only action ran.
    """
    settings = Settings(_env_file=None).model_copy(
        update={"owner_telegram_id": 0, "allowed_user_ids": []}
    )
    _patch_owner(monkeypatch, owner_id=0)
    guard = build_access_guard(settings)
    update = _update_without_user()

    passed_the_guard = guard.check_update(update) is False
    assert passed_the_guard, "the guard's documented no-user pass-through changed"

    executed: list[str] = []

    @owner_only
    async def approve_cmd(update: Any) -> str:
        executed.append("approved a pending self-update")
        return "ok"

    assert await approve_cmd(update) is None
    assert executed == [], "authority escalation: an owner-only action executed"


# --------------------------------------------------------------------- #
# 5. no regression for a genuinely configured owner
# --------------------------------------------------------------------- #


async def test_configured_owner_still_passes_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_owner(monkeypatch, owner_id=OWNER)
    executed: list[str] = []

    @owner_only
    async def handler(update: Any) -> str:
        executed.append("ran")
        return "owner action"

    update = SimpleNamespace(effective_user=SimpleNamespace(id=OWNER), effective_chat=None)
    assert await handler(update) == "owner action"
    assert executed == ["ran"]


def test_configured_owner_is_recognized_and_strangers_are_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_owner(monkeypatch, owner_id=OWNER)
    assert is_owner(OWNER) is True
    assert is_owner(STRANGER) is False
    assert is_owner(FABRICATED_NO_USER_ID) is False


@pytest.mark.parametrize("wrong_type", [None, "4242", b"4242", 4242.0, [], {}])
def test_wrong_typed_candidates_are_refused(
    monkeypatch: pytest.MonkeyPatch, wrong_type: Any
) -> None:
    """Nothing that is not the owner's int id may pass the gate."""
    _patch_owner(monkeypatch, owner_id=OWNER)
    assert is_owner(wrong_type) is False


def test_a_boolean_is_never_an_owner_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """``True == 1``: a bool reaching the gate must not impersonate owner id 1."""
    _patch_owner(monkeypatch, owner_id=1)
    assert is_owner(1) is True, "the real id 1 is still the configured owner"
    assert is_owner(True) is False
    assert is_owner(False) is False


async def test_owner_only_refuses_a_userless_callback_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other PTB update kind: a callback whose user is missing is refused."""
    _patch_owner(monkeypatch, owner_id=0)
    executed: list[str] = []

    @owner_only
    async def handler(update: Any) -> str:
        executed.append("ran")
        return "owner action"

    update = SimpleNamespace(
        effective_user=None,
        effective_chat=None,
        callback_query=SimpleNamespace(data="approve:1"),
    )
    assert await handler(update) is None
    assert executed == []


def test_owner_only_keeps_the_wrapped_handler_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The decorator stays transparent (name/docstring) for PTB registration."""
    _patch_owner(monkeypatch, owner_id=OWNER)

    @owner_only
    async def handler(update: Any) -> str:
        """Handler docstring."""
        return "ok"

    assert handler.__name__ == "handler"
    assert handler.__doc__ == "Handler docstring."
