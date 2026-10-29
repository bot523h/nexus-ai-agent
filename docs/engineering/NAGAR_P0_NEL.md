# Nagar Editing Language (NEL) v1

`OBSERVED_IN_CODE`: NEL v1 is implemented in `src/nexus_ai_agent/nagar/p0/nel.py` as a pure parser/compiler. It is intentionally narrower than a general-purpose language.

## Grammar

```ebnf
program       = "NEL 1", project, intent, select, trim, { preserve }, { evidence }, [ "PREVIEW" ] ;
project       = "PROJECT", identifier ;
intent        = "INTENT", quoted_text ;
select        = "SELECT semantic_segment(\"", label, "\")" ;
trim          = "TRIM duration_delta=", signed_seconds ;
preserve      = "PRESERVE speech_continuity=", ("true" | "false") ;
evidence      = "REQUIRE evidence(\"", evidence_id, "\")" ;
identifier    = letter, { letter | digit | "_" | "." | ":" | "-" } ;
label         = lowercase_letter, { lowercase_letter | digit | "_" } ;
signed_seconds = [ "-" ], number, "s" ;
```

The parser reports source line and column. Unknown statements, missing required statements, malformed durations, and invalid identifiers are errors. There is no fallback to raw commands.

## Compiler mapping

```text
NEL source
  -> NELAst
  -> TypedTrimIR(operation="timeline.trim", project_id, semantic_label,
                 duration_delta_us, preservation, evidence requirements)
  -> existing Semantic Time resolver
  -> existing capability registry / policy / authorization
  -> preview or canonical CommandBus commit
```

`IMPLEMENTED`: the runtime adapter refuses commit planning when the semantic segment is stale, ambiguous, below the confidence threshold, outside the actor’s project/tenant scope, zero-delta, or backed only by a model name rather than a verified `EvidenceRef`. A negative trim whose intent names a pause is an explicit `unsupported_intent`; preservation constraints are typed clarification until a verifier exists. The AST and IR are not authorization grants.

## Security properties

`TESTED` by the focused P0 tests once the repository dev dependencies are installed: the intended contracts reject shell-like statements, keep operation names allowlisted, use integer microseconds in IR, and produce stable canonical JSON for hashing and replay.

The current implementation does not accept paths, FFmpeg argv, imports, eval, dynamic operation names, actor IDs, tenant IDs, or policy overrides from NEL input. It also does not claim to generate NEL from intent, and it does not claim durable nonzero-revision mutation safety.
