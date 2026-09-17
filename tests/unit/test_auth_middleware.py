"""Deny-by-default tests for AuthMiddleware (Phase 0)."""

from __future__ import annotations

from nexus_ai_agent.bot.middleware import AuthMiddleware


def test_list_populated_allows_members_and_owner() -> None:
    auth = AuthMiddleware([1, 2, 3], owner_telegram_id=99)
    assert auth.is_allowed(1) is True
    assert auth.is_allowed(3) is True
    assert auth.is_allowed(99) is True  # owner, even though not in the list
    assert auth.is_allowed(4) is False


def test_empty_list_with_owner_allows_only_owner() -> None:
    auth = AuthMiddleware([], owner_telegram_id=99)
    assert auth.is_allowed(99) is True
    assert auth.is_allowed(1) is False
    assert auth.is_allowed(0) is False


def test_empty_list_without_owner_denies_everyone() -> None:
    auth = AuthMiddleware([])
    assert auth.is_allowed(1) is False
    assert auth.is_allowed(99) is False
    assert auth.is_allowed(0) is False


def test_unconfigured_owner_never_matches() -> None:
    # owner_telegram_id == 0 means "not configured"; a real user can never
    # match it.
    auth = AuthMiddleware([7])
    assert auth.is_allowed(7) is True
    assert auth.is_allowed(0) is False
