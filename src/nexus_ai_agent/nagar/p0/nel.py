"""NEL v1: a deliberately small declarative editing language.

No shell, eval, imports, arbitrary paths, or free-form FFmpeg arguments are accepted.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NEL_VERSION: Literal[1] = 1


class NELDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: str
    message: str
    line: int = Field(ge=1)
    column: int = Field(ge=1)


class SemanticSelect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    label: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class TrimAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    duration_delta_us: int


class PreserveAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    field: Literal["speech_continuity"]
    value: bool


class EvidenceRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")


class NELAst(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = NEL_VERSION
    project: str
    intent: str
    select: SemanticSelect
    trim: TrimAction
    preserve: tuple[PreserveAction, ...] = ()
    requirements: tuple[EvidenceRequirement, ...] = ()
    preview: bool = False


class TypedTrimIR(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ir_version: Literal["nagar.ir.v1"] = "nagar.ir.v1"
    operation: Literal["timeline.trim"] = "timeline.trim"
    project_id: str
    semantic_label: str
    duration_delta_us: int
    preserve_speech_continuity: bool = False
    required_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompileResult:
    ast: NELAst | None
    ir: TypedTrimIR | None
    diagnostics: tuple[NELDiagnostic, ...]
    canonical: str | None = None


def _duration_us(raw: str) -> int:
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)s", raw)
    if not match:
        raise ValueError("duration must be signed seconds, e.g. -2.0s")
    return round(float(match.group(1)) * 1_000_000)


def parse(source: str) -> tuple[NELAst | None, tuple[NELDiagnostic, ...]]:
    lines = source.splitlines()
    values: dict[str, Any] = {"preserve": [], "requirements": []}
    errors: list[NELDiagnostic] = []
    for line_no, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if line == "NEL 1":
                values["version"] = 1
            elif m := re.fullmatch(r"PROJECT ([A-Za-z0-9_.:-]+)", line):
                values["project"] = m.group(1)
            elif m := re.fullmatch(r'INTENT "([^"]{1,128})"', line):
                values["intent"] = m.group(1)
            elif m := re.fullmatch(r'SELECT semantic_segment\("([a-z][a-z0-9_]{0,63})"\)', line):
                values["select"] = SemanticSelect(label=m.group(1))
            elif m := re.fullmatch(r"TRIM duration_delta=(-?\d+(?:\.\d+)?)s", line):
                values["trim"] = TrimAction(duration_delta_us=_duration_us(m.group(1) + "s"))
            elif m := re.fullmatch(r"PRESERVE speech_continuity=(true|false)", line):
                values["preserve"].append(
                    PreserveAction(field="speech_continuity", value=m.group(1) == "true")
                )
            elif m := re.fullmatch(r'REQUIRE evidence\("([a-z][a-z0-9_.-]{0,63})"\)', line):
                values["requirements"].append(EvidenceRequirement(evidence_type=m.group(1)))
            elif line == "PREVIEW":
                values["preview"] = True
            else:
                raise ValueError("unknown statement")
        except (ValueError, TypeError) as exc:
            errors.append(
                NELDiagnostic(
                    code="syntax_error",
                    message=str(exc),
                    line=line_no,
                    column=max(1, raw.find(line) + 1),
                )
            )
    required = ("version", "project", "intent", "select", "trim")
    for name in required:
        if name not in values:
            errors.append(
                NELDiagnostic(code="missing_statement", message=f"missing {name}", line=1, column=1)
            )
    if errors:
        return None, tuple(errors)
    try:
        return NELAst.model_validate(values), ()
    except ValueError as exc:
        return None, (NELDiagnostic(code="schema_error", message=str(exc), line=1, column=1),)


def compile_nel(source: str) -> CompileResult:
    ast, diagnostics = parse(source)
    if diagnostics or ast is None:
        return CompileResult(ast=None, ir=None, diagnostics=diagnostics)
    preserve = {item.field: item.value for item in ast.preserve}
    ir = TypedTrimIR(
        project_id=ast.project,
        semantic_label=ast.select.label,
        duration_delta_us=ast.trim.duration_delta_us,
        preserve_speech_continuity=bool(preserve.get("speech_continuity", False)),
        required_evidence=tuple(item.evidence_type for item in ast.requirements),
    )
    canonical = canonicalize(ast)
    return CompileResult(ast=ast, ir=ir, diagnostics=(), canonical=canonical)


def canonicalize(ast: NELAst) -> str:
    body = ast.model_dump(mode="json")
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(ast: NELAst) -> str:
    return "sha256:" + hashlib.sha256(canonicalize(ast).encode()).hexdigest()
