#!/usr/bin/env python3
"""Mutation gate for the retired STOP-B video-edit endpoint.

Run with ``python scripts/security_mutations_stop_b.py``. A green exit proves
that restoring status, job creation, or body parsing trips the intended guard.
"""

from __future__ import annotations

from security_mutation_support import Mutation, SourceEdit, run_mutation_suite

TESTS = (
    "tests/unit/test_security_hardening.py::test_valid_hmac_cannot_reactivate_retired_video_edit",
    "tests/unit/test_creative_studio.py::test_video_edit_retirement_has_no_runtime_side_effects",
    "tests/architecture/test_legacy_creative_boundary.py::test_video_edit_post_is_a_410_without_processing_or_auth_gates",
)
APP = "src/nexus_ai_agent/api/app.py"

MUTATIONS = (
    Mutation(
        "reactivate_retired_post",
        (
            SourceEdit(
                APP,
                "async def create_video_edit_job() -> JSONResponse:\n"
                "    return JSONResponse(\n        status_code=410,",
                "async def create_video_edit_job() -> JSONResponse:\n"
                "    return JSONResponse(\n        status_code=200,",
            ),
        ),
        (TESTS[0],),
        (
            "tests/unit/test_security_hardening.py::test_valid_hmac_cannot_reactivate_retired_video_edit",
        ),
        "the route must remain HTTP 410 even with a valid HMAC",
    ),
    Mutation(
        "reintroduce_legacy_job_creation",
        (
            SourceEdit(
                APP,
                "async def create_video_edit_job() -> JSONResponse:\n    return JSONResponse(",
                "async def create_video_edit_job() -> JSONResponse:\n"
                '    await get_creative_registry().create_job("video_edit", {})\n'
                "    return JSONResponse(",
            ),
        ),
        (TESTS[1],),
        (
            "tests/unit/test_creative_studio.py::test_video_edit_retirement_has_no_runtime_side_effects",
        ),
        "the retired route must create no registry job",
    ),
    Mutation(
        "reintroduce_request_body_parsing",
        (
            SourceEdit(
                APP,
                "async def create_video_edit_job() -> JSONResponse:\n    return JSONResponse(",
                "async def create_video_edit_job(request: Request) -> JSONResponse:\n"
                "    await request.body()\n"
                "    return JSONResponse(",
            ),
        ),
        (TESTS[2],),
        (
            "tests/architecture/test_legacy_creative_boundary.py::test_video_edit_post_is_a_410_without_processing_or_auth_gates",
        ),
        "the inert route must not accept or parse attacker-controlled request data",
    ),
)


if __name__ == "__main__":
    raise SystemExit(run_mutation_suite("STOP-B", TESTS, MUTATIONS))
