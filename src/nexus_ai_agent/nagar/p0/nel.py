"""NEL v1: a deliberately small declarative editing language."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NEL_VERSION = 1


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
    match = re.fullmatch(r"(-?\d+(?:\.\d{1,9})?)s", raw)
    if not match:
        raise ValueError("duration must be signed seconds, e.g. -2.0s")
    try:
        value = Decimal(match.group(1)) * Decimal(1_000_000)
        return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise ValueError("duration is not a finite decimal") from exc


def parse(source: str) -> tuple[NELAst | None, tuple[NELDiagnostic, ...]]:
    lines = source.splitlines()
    values: dict[str, Any] = {"preserve": [], "requirements": []}
    errors: list[NELDiagnostic] = []
    seen: set[str] = set()

    def duplicate(key: str, line_no: int, raw: str) -> bool:
        if key in seen:
            errors.append(
                NELDiagnostic(
                    code="duplicate_statement",
                    message=f"duplicate or conflicting {key} statement",
                    line=line_no,
                    column=max(1, raw.find(key) + 1),
                )
            )
            return True
        seen.add(key)
        return False

    for line_no, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if line == "NEL 1":
                if not duplicate("NEL", line_no, raw):
                    values["version"] = 1
            elif m := re.fullmatch(r"PROJECT ([A-Za-z0-9_.:-]+)", line):
                if not duplicate("PROJECT", line_no, raw):
                    values["project"] = m.group(1)
            elif m := re.fullmatch(r'INTENT "([^\"]{1,128})"', line):
                if not duplicate("INTENT", line_no, raw):
                    values["intent"] = m.group(1)
            elif m := re.fullmatch(r'SELECT semantic_segment\("([a-z][a-z0-9_]{0,63})"\)', line):
                if not duplicate("SELECT", line_no, raw):
                    values["select"] = SemanticSelect(label=m.group(1))
            elif m := re.fullmatch(r"TRIM duration_delta=(-?\d+(?:\.\d{1,9})?)s", line):
                if not duplicate("TRIM", line_no, raw):
                    values["trim"] = TrimAction(duration_delta_us=_duration_us(m.group(1) + "s"))
            elif m := re.fullmatch(r"PRESERVE speech_continuity=(true|false)", line):
                if not duplicate("PRESERVE:speech_continuity", line_no, raw):
                    values["preserve"].append(
                        PreserveAction(field="speech_continuity", value=m.group(1) == "true")
                    )
            elif m := re.fullmatch(r'REQUIRE evidence\("([a-z][a-z0-9_.-]{0,63})"\)', line):
                if not duplicate(f"REQUIRE:{m.group(1)}", line_no, raw):
                    values["requirements"].append(EvidenceRequirement(evidence_type=m.group(1)))
            elif line == "PREVIEW":
                if not duplicate("PREVIEW", line_no, raw):
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
    for name in ("version", "project", "intent", "select", "trim"):
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
    return CompileResult(ast=ast, ir=ir, diagnostics=(), canonical=canonicalize(ast))


def canonicalize(ast: NELAst) -> str:
    body = ast.model_dump(mode="json")
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(ast: NELAst) -> str:
    return "sha256:" + hashlib.sha256(canonicalize(ast).encode()).hexdigest()
