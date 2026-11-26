"""AST mutation campaign for Wave 3 IBPC durability, canonical binding, privacy, and RBAC.

Every mutant uses a scoped ``ast.NodeTransformer`` inside an isolated copy of
``src/nexus_ai_agent`` in ``tmp_path`` (never mutating the working tree), runs
with ``PYTHONDONTWRITEBYTECODE=1``, compiles cleanly before execution, and is
evaluated with a strict kill oracle:

1. ``returncode == 1`` (exact behavioral test failure, never 2/3/4/5);
2. ``FAILED`` appears in stdout/stderr;
3. zero ``SyntaxError``, ``IndentationError``, ``ImportError``,
   ``ModuleNotFoundError``, ``INTERNALERROR``, or ``no tests ran``; and
4. at least one expected invariant failure token is present.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

# Keep NumPy's native extension resident while mutant subprocesses import the
# copied package under coverage instrumentation.
_NUMPY_EXTENSION_KEEPALIVE = np

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_PACKAGE = REPO_ROOT / "src" / "nexus_ai_agent"

_FORBIDDEN_CRASH_TOKENS: tuple[str, ...] = (
    "SyntaxError",
    "IndentationError",
    "ImportError",
    "ModuleNotFoundError",
    "INTERNALERROR",
    "no tests ran",
)


class _ScopedAstMutator(ast.NodeTransformer):
    """Apply exactly one AST transformation inside a named function/method."""

    def __init__(
        self,
        func_name: str,
        predicate: Callable[[ast.AST], bool],
        transform: Callable[[ast.AST], ast.AST],
    ) -> None:
        self._func_name = func_name
        self._predicate = predicate
        self._transform = transform
        self._in_scope = False
        self.applied = 0

    def _visit_scoped(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST:
        if node.name == self._func_name:
            prev = self._in_scope
            self._in_scope = True
            try:
                return self.generic_visit(node)
            finally:
                self._in_scope = prev
        return self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return self._visit_scoped(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return self._visit_scoped(node)

    def generic_visit(self, node: ast.AST) -> ast.AST:
        if self._in_scope and self.applied == 0 and self._predicate(node):
            self.applied += 1
            return self._transform(node)
        return super().generic_visit(node)


def _is_if_with_test_substr(node: ast.AST, substr: str) -> bool:
    return isinstance(node, ast.If) and substr in ast.unparse(node.test)


def _bypass_if(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.If)
    return ast.copy_location(
        ast.If(test=ast.Constant(value=False), body=node.body, orelse=node.orelse),
        node,
    )


def _force_if_true(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.If)
    return ast.copy_location(
        ast.If(test=ast.Constant(value=True), body=node.body, orelse=node.orelse),
        node,
    )


def _replace_dict_entry_with_empty(node: ast.AST, key_name: str) -> ast.AST:
    assert isinstance(node, ast.Dict)
    new_values = []
    for k, v in zip(node.keys, node.values, strict=True):
        if isinstance(k, ast.Constant) and k.value == key_name:
            new_values.append(ast.Constant(value=""))
        else:
            new_values.append(v)
    return ast.copy_location(ast.Dict(keys=node.keys, values=new_values), node)


def _is_expr_call_substr(node: ast.AST, substr: str) -> bool:
    return isinstance(node, ast.Expr) and substr in ast.unparse(node.value)


def _replace_with_pass(node: ast.AST) -> ast.AST:
    return ast.copy_location(ast.Pass(), node)


@dataclass(frozen=True)
class Wave3MutantSpec:
    name: str
    relative_path: Path
    func_name: str
    predicate: Callable[[ast.AST], bool]
    transform: Callable[[ast.AST], ast.AST]
    target_test: str
    expected_tokens: tuple[str, ...]


WAVE3_MUTANTS: tuple[Wave3MutantSpec, ...] = (
    Wave3MutantSpec(
        name="M01_session_epoch_fencing",
        relative_path=Path("nagar/ibpc/session.py"),
        func_name="update_session_cas",
        predicate=lambda n: _is_if_with_test_substr(n, "cur.rowcount != 1"),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3EdgeCasesAndCoverage::test_wave3_ibpc_voice_turn_and_edge_branches"
        ),
        expected_tokens=("SessionEpochConflictError", "DID NOT RAISE"),
    ),
    Wave3MutantSpec(
        name="M02_session_monotonic_turn_order",
        relative_path=Path("nagar/ibpc/session.py"),
        func_name="insert_turn_atomic",
        predicate=lambda n: _is_if_with_test_substr(n, "turn.sequence_number <= db_max_seq"),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3EdgeCasesAndCoverage::test_wave3_ibpc_voice_turn_and_edge_branches"
        ),
        expected_tokens=("StaleTurnError", "DID NOT RAISE"),
    ),
    Wave3MutantSpec(
        name="M03_session_idempotency_conflict",
        relative_path=Path("nagar/ibpc/session.py"),
        func_name="insert_turn_atomic",
        predicate=lambda n: _is_if_with_test_substr(
            n, "existing.payload_digest != turn.payload_digest"
        ),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3EdgeCasesAndCoverage::test_wave3_ibpc_voice_turn_and_edge_branches"
        ),
        expected_tokens=("IdempotencyConflictError", "DID NOT RAISE"),
    ),
    Wave3MutantSpec(
        name="M04_canonical_approval_hash_drops_expiry",
        relative_path=Path("nagar/ibpc/models.py"),
        func_name="canonical_approval_payload",
        predicate=lambda n: (
            isinstance(n, ast.Dict)
            and any(isinstance(k, ast.Constant) and k.value == "expires_at_utc" for k in n.keys)
        ),
        transform=lambda n: _replace_dict_entry_with_empty(n, "expires_at_utc"),
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestSlice32DurableApprovalBinding::test_canonical_binding_hash_expiry_and_tamper_rejection"
        ),
        expected_tokens=("AssertionError", "assert"),
    ),
    Wave3MutantSpec(
        name="M05_approval_expiry_gate",
        relative_path=Path("nagar/ibpc/approval.py"),
        func_name="_check_expiry",
        predicate=lambda n: _is_if_with_test_substr(n, "now_dt >= exp_dt"),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestSlice32DurableApprovalBinding::test_canonical_binding_hash_expiry_and_tamper_rejection"
        ),
        expected_tokens=("ApprovalError", "DID NOT RAISE"),
    ),
    Wave3MutantSpec(
        name="M06_approval_canonical_hash_check",
        relative_path=Path("nagar/ibpc/approval.py"),
        func_name="_check_identity",
        predicate=lambda n: (
            isinstance(n, ast.If)
            and "binding.approval_hash" in ast.unparse(n.test)
            and "canonical_binding_digest" in ast.unparse(n.test)
        ),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestSlice32DurableApprovalBinding::test_canonical_binding_hash_expiry_and_tamper_rejection"
        ),
        expected_tokens=("ApprovalError", "DID NOT RAISE"),
    ),
    Wave3MutantSpec(
        name="M07_privacy_local_vault_egress_guard",
        relative_path=Path("nagar/ibpc/privacy.py"),
        func_name="enforce_processing_consent",
        predicate=lambda n: _is_if_with_test_substr(
            n, "EXTERNAL_PROVIDER_DISPATCH.value and mode is PrivacyMode.LOCAL_VAULT"
        ),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestSlice33DurablePrivacyConsent::test_durable_grant_immediate_revoke_expiry_and_append_only_audit"
        ),
        expected_tokens=("PrivacyViolation", "DID NOT RAISE"),
    ),
    Wave3MutantSpec(
        name="M08_auth_insufficient_role_gate",
        relative_path=Path("adapters/studio_principal_auth.py"),
        func_name="authorize_action",
        predicate=lambda n: _is_if_with_test_substr(n, "action not in ROLE_ACTIONS[role]"),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestSlice34StudioPrincipalAuthAndAPI::test_rbac_matrix_and_revocation_fail_closed"
        ),
        expected_tokens=("StudioAuthorizationError", "DID NOT RAISE"),
    ),
    Wave3MutantSpec(
        name="M09_service_execute_rbac_gate",
        relative_path=Path("nagar/ibpc/service.py"),
        func_name="execute",
        predicate=lambda n: _is_expr_call_substr(n, "self._authorize_studio_action"),
        transform=_replace_with_pass,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3EdgeCasesAndCoverage::test_wave3_ibpc_voice_turn_and_edge_branches"
        ),
        expected_tokens=("StudioAuthorizationError", "DID NOT RAISE"),
    ),
    Wave3MutantSpec(
        name="M10_studio_ibpc_project_id_validation",
        relative_path=Path("api/studio_ibpc.py"),
        func_name="_authorize",
        predicate=lambda n: _is_if_with_test_substr(n, "_ID_RE.fullmatch(project_id)"),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3EdgeCasesAndCoverage::test_studio_principal_auth_and_http_error_paths"
        ),
        expected_tokens=("AssertionError", "400"),
    ),
    Wave3MutantSpec(
        name="M11_approval_key_provider_check",
        relative_path=Path("nagar/ibpc/approval.py"),
        func_name="consume",
        predicate=lambda n: _is_if_with_test_substr(n, "binding.key_id"),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3SecurityClosureAndCoverage::test_missing_approval_key_fails_closed_no_fallback_and_key_rotation"
        ),
        expected_tokens=("AssertionError", "ApprovalError", "DID NOT RAISE"),
    ),
    Wave3MutantSpec(
        name="M12_privacy_external_provider_exact_match",
        relative_path=Path("nagar/ibpc/privacy.py"),
        func_name="check_consent_status",
        predicate=lambda n: _is_if_with_test_substr(
            n, "processing_scope == ProcessingScope.EXTERNAL_PROVIDER_DISPATCH.value"
        ),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3SecurityClosureAndCoverage::test_external_provider_consent_requires_exact_provider_id"
        ),
        expected_tokens=("PrivacyViolation", "DID NOT RAISE", "AssertionError"),
    ),
    Wave3MutantSpec(
        name="M13_privacy_revoke_project_guard",
        relative_path=Path("nagar/ibpc/privacy.py"),
        func_name="revoke_consent",
        predicate=lambda n: _is_if_with_test_substr(
            n, "project_id is not None and existing.project_id != project_id"
        ),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3SecurityClosureAndCoverage::test_cross_project_consent_revoke_refused"
        ),
        expected_tokens=("PrivacyViolation", "DID NOT RAISE"),
    ),
    Wave3MutantSpec(
        name="M14_service_untrusted_source_path_symlink",
        relative_path=Path("nagar/ibpc/service.py"),
        func_name="_validate_source_path",
        predicate=lambda n: _is_if_with_test_substr(n, "stat.S_ISLNK(st.st_mode)"),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3SecurityClosureAndCoverage::test_symlink_and_outside_asset_root_source_path_refused"
        ),
        expected_tokens=("AuthorizationError", "DID NOT RAISE", "AssertionError"),
    ),
    Wave3MutantSpec(
        name="M15_service_untrusted_source_path_outside_root",
        relative_path=Path("nagar/ibpc/service.py"),
        func_name="_validate_source_path",
        predicate=lambda n: _is_if_with_test_substr(
            n, "not resolved_src.is_relative_to(resolved_root)"
        ),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3SecurityClosureAndCoverage::test_symlink_and_outside_asset_root_source_path_refused"
        ),
        expected_tokens=("AuthorizationError", "DID NOT RAISE", "AssertionError"),
    ),
    Wave3MutantSpec(
        name="M16_service_authoritative_revision_mismatch",
        relative_path=Path("nagar/ibpc/service.py"),
        func_name="_validate_revision_binding",
        predicate=lambda n: _is_if_with_test_substr(n, "base_revision_id is not None"),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3SecurityClosureAndCoverage::test_stale_and_spoofed_base_revision_id_refused"
        ),
        expected_tokens=("StaleRevisionError", "DID NOT RAISE", "AssertionError"),
    ),
    Wave3MutantSpec(
        name="M17_service_session_owner_binding",
        relative_path=Path("nagar/ibpc/service.py"),
        func_name="_validate_session_binding",
        predicate=lambda n: _is_if_with_test_substr(n, "require_session_owner"),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3SecurityClosureAndCoverage::test_cross_project_and_cross_principal_turn_submission_refused"
        ),
        expected_tokens=("AuthorizationError", "DID NOT RAISE", "AssertionError"),
    ),
    Wave3MutantSpec(
        name="M18_auth_same_tenant_enforcement",
        relative_path=Path("adapters/studio_principal_auth.py"),
        func_name="assert_same_tenant",
        predicate=lambda n: _is_if_with_test_substr(n, "p_tenant != tenant_id"),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3SecurityClosureAndCoverage::test_cross_tenant_membership_grant_and_session_spoof_denied"
        ),
        expected_tokens=("AuthorizationError", "DID NOT RAISE", "AssertionError"),
    ),
    Wave3MutantSpec(
        name="M19_studio_rbac_fail_closed",
        relative_path=Path("api/studio.py"),
        func_name="_enforce_optional_principal_rbac",
        predicate=lambda n: _is_if_with_test_substr(
            n, "not principal_token and require_flag != '1' and (not auth_configured)"
        ),
        transform=_force_if_true,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3SecurityClosureAndCoverage::test_studio_rbac_fail_closed_by_default_when_auth_configured"
        ),
        expected_tokens=("AssertionError", "401"),
    ),
    Wave3MutantSpec(
        name="M20_studio_ibpc_cross_tenant_binding_guard",
        relative_path=Path("api/studio_ibpc.py"),
        func_name="bind_studio_principal_channel",
        predicate=lambda n: _is_if_with_test_substr(n, "target_tenant != actor_tenant"),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3SecurityClosureAndCoverage::test_membership_and_principal_lifecycle_apis_and_admin_audit"
        ),
        expected_tokens=("AssertionError", "403"),
    ),
    Wave3MutantSpec(
        name="M21_register_principal_insert_only_conflict",
        relative_path=Path("adapters/studio_principal_auth.py"),
        func_name="register_principal",
        predicate=lambda n: _is_if_with_test_substr(n, "existing is not None"),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3FinalSecurityClosure::"
            "test_p1_1_register_principal_rejects_duplicate_and_token_hijack_via_store_and_api"
        ),
        expected_tokens=("AuthorizationError", "DID NOT RAISE", "AssertionError", "IntegrityError"),
    ),
    Wave3MutantSpec(
        name="M22_bind_channel_insert_only_conflict",
        relative_path=Path("adapters/studio_principal_auth.py"),
        func_name="bind_channel",
        predicate=lambda n: _is_if_with_test_substr(n, "existing is not None"),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3FinalSecurityClosure::"
            "test_p1_2_bind_channel_rejects_collision_hijack_and_generates_server_binding_ref"
        ),
        expected_tokens=("AuthorizationError", "DID NOT RAISE", "AssertionError", "IntegrityError"),
    ),
    Wave3MutantSpec(
        name="M23_privacy_consent_tenant_isolation",
        relative_path=Path("nagar/ibpc/privacy.py"),
        func_name="check_consent_status",
        predicate=lambda n: _is_if_with_test_substr(n, "not tenant_matching"),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3FinalSecurityClosure::"
            "test_p2_2_privacy_consent_enforces_tenant_isolation_and_legacy_backfill"
        ),
        expected_tokens=("PrivacyViolation", "DID NOT RAISE", "AssertionError"),
    ),
    Wave3MutantSpec(
        name="M24_production_rbac_opt_out_forbidden",
        relative_path=Path("api/studio.py"),
        func_name="_enforce_optional_principal_rbac",
        predicate=lambda n: _is_if_with_test_substr(n, "require_flag == '0' or opt_out"),
        transform=_bypass_if,
        target_test=(
            "tests/unit/test_wave3_ibpc_durability.py::"
            "TestWave3FinalSecurityClosure::"
            "test_p2_3_owner_scoped_to_project_and_production_forbids_rbac_opt_out"
        ),
        expected_tokens=("AssertionError", "403"),
    ),
)


@pytest.mark.parametrize("spec", WAVE3_MUTANTS, ids=lambda s: s.name)
def test_wave3_ast_mutant_is_validly_killed(tmp_path: Path, spec: Wave3MutantSpec) -> None:
    copied_pkg = tmp_path / "src" / "nexus_ai_agent"
    shutil.copytree(SRC_PACKAGE, copied_pkg, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO_ROOT / "migrations", tmp_path / "migrations")
    shutil.copy2(REPO_ROOT / "alembic.ini", tmp_path / "alembic.ini")

    target_file = copied_pkg / spec.relative_path
    original_src = target_file.read_text(encoding="utf-8")
    tree = ast.parse(original_src, filename=str(target_file))
    mutator = _ScopedAstMutator(spec.func_name, spec.predicate, spec.transform)
    mutated_tree = mutator.visit(tree)
    ast.fix_missing_locations(mutated_tree)
    assert mutator.applied == 1, (
        f"mutant {spec.name} applied {mutator.applied} times (expected exactly 1)"
    )

    mutated_src = ast.unparse(mutated_tree) + "\n"
    compile(mutated_src, str(target_file), "exec")
    target_file.write_text(mutated_src, encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(tmp_path / "src") + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", spec.target_test],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = proc.stdout + "\n" + proc.stderr
    for crash_tok in _FORBIDDEN_CRASH_TOKENS:
        assert crash_tok not in output, (
            f"invalid mutant {spec.name} ({crash_tok}):\n{output[-2500:]}"
        )
    assert proc.returncode == 1, (
        f"mutant {spec.name} did not exit 1 (rc={proc.returncode}):\n{output[-2500:]}"
    )
    assert "FAILED" in output, f"mutant {spec.name} missing FAILED marker:\n{output[-2500:]}"
    assert any(tok in output for tok in spec.expected_tokens), (
        f"mutant {spec.name} failed without expected invariant token "
        f"{spec.expected_tokens}:\n{output[-2500:]}"
    )
